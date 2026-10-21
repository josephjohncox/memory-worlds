"""Coordinated whole-authority snapshots and fresh-target recovery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from contextlib import nullcontext
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import BigInteger, Float, Integer, String, Text, func, select

from .errors import Conflict
from .errors import MemoryError as InvalidRequest
from .graph_snapshots import FalkorSnapshots
from .models import Identifier, Model, Principal
from .persistence import CONTROL_METADATA, OUTBOX_METADATA, SOURCE_METADATA
from .portability import _json, _load_json
from .recovery_archive import AuthorityManifest, read_backup, write_backup

MAX_ROW_BYTES = 16 * 1024 * 1024
_REGISTRIES = {"control": CONTROL_METADATA, "source": SOURCE_METADATA, "outbox": OUTBOX_METADATA}


class TableSnapshot(Model):
    kind: Literal["control", "source", "outbox"]
    table: str = Field(min_length=1, max_length=100)
    member: str = Field(min_length=1, max_length=500)
    rows: int = Field(ge=0, strict=True)
    world_id: Identifier | None = None
    scope_id: UUID | None = None


def _schema_digest() -> str:
    fields = {
        kind: {
            table.name: [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                    "foreign_keys": sorted(key.target_fullname for key in column.foreign_keys),
                }
                for column in table.columns
            ]
            for table in registry.sorted_tables
        }
        for kind, registry in _REGISTRIES.items()
    }
    return hashlib.sha256(_json(fields)).hexdigest()


def _session(store, description: TableSnapshot):
    if description.kind == "control":
        return store._control()
    if description.kind == "source":
        return store._world_db(description.world_id, description.scope_id)
    return store.persistence.outbox()


def _table_inventory(store) -> list[TableSnapshot]:
    result = []
    for kind in ("control", "outbox"):
        for table in _REGISTRIES[kind].sorted_tables:
            result.append(
                TableSnapshot(
                    kind=kind,
                    table=table.name,
                    member=f"tables/{kind}/{table.name}.jsonl",
                    rows=0,
                )
            )
    with store._control() as db:
        table = store._table(db, "scopes")
        scopes = db.execute(select(table.c.world_id, table.c.id).order_by(table.c.id)).all()
    for world_id, scope_id in scopes:
        for table in SOURCE_METADATA.sorted_tables:
            result.append(
                TableSnapshot(
                    kind="source",
                    table=table.name,
                    world_id=world_id,
                    scope_id=UUID(scope_id),
                    rows=0,
                    member=f"tables/source/{world_id}/{scope_id}/{table.name}.jsonl",
                )
            )
    return result


def _dump_tables(store, target: Path) -> tuple[list[dict], dict[str, Path]]:
    inventory, members = [], {}
    for description in _table_inventory(store):
        destination = target / description.member
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        count = 0
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output, _session(store, description) as db:
            table = store._table(db, description.table)
            statement = select(table).order_by(*table.primary_key.columns)
            for row in db.execute(statement).mappings().yield_per(100):
                encoded = _json(dict(row)) + b"\n"
                if len(encoded) > MAX_ROW_BYTES:
                    raise InvalidRequest("Authority row exceeds its recovery record limit")
                output.write(encoded)
                count += 1
            output.flush()
            os.fsync(output.fileno())
        inventory.append(description.model_copy(update={"rows": count}).model_dump(mode="json"))
        members[description.member] = destination
    return inventory, members


def backup_authority(
    store,
    principal: Principal,
    destination: Path,
    key_file: Path,
    *,
    graph: FalkorSnapshots | None = None,
) -> AuthorityManifest:
    """Take one maintenance cut across all journals and selected graph namespaces."""
    with store._control() as db:
        store._admin(db, principal)
    with store.persistence.maintenance_guard():
        # Recheck after waiting for in-flight writes and before reading secrets.
        with store._control() as db:
            store._admin(db, principal)
        with tempfile.TemporaryDirectory(prefix="memory-worlds-authority-cut-") as temporary:
            target = Path(temporary)
            tables, members = _dump_tables(store, target)
            graph_metadata = None
            if graph is not None:
                with store._control() as db:
                    catalog = store._table(db, "projection_generations")
                    generations = [dict(row) for row in db.execute(select(catalog)).mappings()]
                graph_metadata = graph.capture(store.projection_worlds(), generations, target)
                for entry in graph_metadata["graphs"]:
                    if entry.get("present"):
                        members[entry["member"]] = target / entry["member"]
            return write_backup(
                destination,
                key_file,
                members,
                authority_id=store.authority_id,
                source_backend="postgres" if store.persistence.is_postgres else "sqlite",
                graph_mode="native" if graph is not None else "rebuild",
                metadata={
                    "schema_sha256": _schema_digest(),
                    "tables": tables,
                    "graph": graph_metadata,
                },
            )


def _rows(path: Path):
    with path.open("rb") as source:
        while line := source.readline(MAX_ROW_BYTES + 1):
            if len(line) > MAX_ROW_BYTES or not line.endswith(b"\n"):
                raise InvalidRequest("Invalid authority recovery record size or framing")
            value = _load_json(line, "authority table")
            if not isinstance(value, dict):
                raise InvalidRequest("Authority recovery rows must be objects")
            yield value


def _validate_row(table, row):
    if set(row) != set(table.columns.keys()):
        raise InvalidRequest("Authority recovery row differs from the current schema")
    for column in table.columns:
        value = row[column.name]
        if value is None:
            if not column.nullable:
                raise InvalidRequest("Authority recovery row contains an invalid null")
        elif isinstance(column.type, (Integer, BigInteger)):
            if type(value) is not int:
                raise InvalidRequest("Authority recovery row contains an invalid integer")
        elif isinstance(column.type, Float):
            if type(value) not in (float, int) or not math.isfinite(value):
                raise InvalidRequest("Authority recovery row contains an invalid number")
        elif isinstance(column.type, (Text, String)):
            if not isinstance(value, str):
                raise InvalidRequest("Authority recovery row contains invalid text")
        else:
            raise InvalidRequest("Authority recovery encountered an unregistered column type")


def _validate_inventory(manifest: AuthorityManifest, target: Path) -> list[TableSnapshot]:
    if manifest.metadata.get("schema_sha256") != _schema_digest():
        raise InvalidRequest("Authority backup does not match the current schema")
    raw = manifest.metadata.get("tables")
    if not isinstance(raw, list):
        raise InvalidRequest("Authority backup lacks its table inventory")
    descriptions = [TableSnapshot.model_validate(item) for item in raw]
    files = {entry.name for entry in manifest.members}
    seen, scopes = set(), set()
    for description in descriptions:
        registry = _REGISTRIES[description.kind]
        if description.table not in registry.tables:
            raise InvalidRequest("Authority backup contains an unknown table")
        identity = description.kind, description.world_id, description.scope_id, description.table
        if identity in seen or description.member not in files:
            raise InvalidRequest("Authority table inventory is duplicated or incomplete")
        seen.add(identity)
        if description.kind == "source":
            if description.world_id is None or description.scope_id is None:
                raise InvalidRequest("Source recovery requires a canonical world and scope")
            scopes.add((description.world_id, description.scope_id))
            expected = (
                f"tables/source/{description.world_id}/{description.scope_id}/"
                f"{description.table}.jsonl"
            )
        else:
            if description.world_id is not None or description.scope_id is not None:
                raise InvalidRequest("Control/outbox recovery cannot select source scopes")
            expected = f"tables/{description.kind}/{description.table}.jsonl"
        if description.member != expected:
            raise InvalidRequest("Authority table member identity mismatch")
        table = registry.tables[description.table]
        count = 0
        for row in _rows(target / description.member):
            _validate_row(table, row)
            count += 1
        if count != description.rows:
            raise InvalidRequest("Authority table row count mismatch")
    required = {
        (kind, None, None, table)
        for kind in ("control", "outbox")
        for table in _REGISTRIES[kind].tables
    }
    required.update(
        ("source", world, scope, table)
        for world, scope in scopes
        for table in SOURCE_METADATA.tables
    )
    if seen != required:
        raise InvalidRequest("Authority backup omits current tables")
    scope_table = next(
        item for item in descriptions if item.kind == "control" and item.table == "scopes"
    )
    declared_scopes = {
        (row["world_id"], UUID(row["id"])) for row in _rows(target / scope_table.member)
    }
    if scopes != declared_scopes:
        raise InvalidRequest("Authority backup omits private or shared scope journals")
    metadata_table = next(
        item for item in descriptions if item.kind == "control" and item.table == "metadata"
    )
    authority_rows = [
        row for row in _rows(target / metadata_table.member) if row["key"] == "authority_id"
    ]
    if authority_rows != [{"key": "authority_id", "value": manifest.authority_id.hex}]:
        raise InvalidRequest("Authority backup identity differs from its manifest")
    return descriptions


def _empty_target(store):
    for description in _table_inventory(store):
        if description.kind == "control" and description.table == "metadata":
            with store._control() as db:
                table = store._table(db, "metadata")
                rows = [dict(row) for row in db.execute(select(table)).mappings()]
                if rows != [{"key": "authority_id", "value": store.authority_id}]:
                    raise Conflict("Whole-authority recovery requires a fresh empty target")
            continue
        with _session(store, description) as db:
            table = store._table(db, description.table)
            if db.execute(select(func.count()).select_from(table)).scalar_one():
                raise Conflict("Whole-authority recovery requires a fresh empty target")


def _restore_tables(store, descriptions: list[TableSnapshot], source: Path, native_graph: bool):
    lookup = {(item.kind, item.world_id, item.scope_id, item.table): item for item in descriptions}
    control = [
        lookup[("control", None, None, table.name)] for table in CONTROL_METADATA.sorted_tables
    ]
    others = [item for item in descriptions if item.kind != "control"]
    # Sources were emitted in current FK order; outbox tables have no foreign parents.
    for description in control + others:
        if description.kind == "source":
            store.persistence.initialize_source(description.world_id, description.scope_id)
        with _session(store, description) as db:
            table = store._table(db, description.table)
            db.execute(table.delete())
            batch, size = [], 0
            for original in _rows(source / description.member):
                row = dict(original)
                if description.table == "worker_leases":
                    row.update(owner=None, expires_at=0)
                if description.table in {"projection_jobs", "deliveries"}:
                    row.update(lease_owner=None, lease_fence=None, lease_expires_at=None)
                if description.table == "deliveries" and row["state"] == "processing":
                    row.update(state="pending", next_attempt=0)
                if not native_graph:
                    if description.table == "projection":
                        row.update(generation=None, state="disabled", configuration="{}")
                    elif description.table == "projection_jobs":
                        row.update(state="pending", generation=None, error=None)
                    elif description.table == "episodes":
                        consent = (
                            json.loads(row["payload"])["project"]
                            or row["projection_state"] != "disabled"
                        )
                        row["projection_state"] = "pending" if consent else "disabled"
                    elif description.table == "projection_generations" and row["state"] != "purged":
                        row["state"] = "retired"
                encoded_size = len(_json(row))
                if batch and size + encoded_size > 4 * 1024 * 1024:
                    db.execute(table.insert(), batch)
                    batch, size = [], 0
                batch.append(row)
                size += encoded_size
            if batch:
                db.execute(table.insert(), batch)
            store.persistence.reset_sequences(db, table)


def restore_authority(
    archive: Path,
    key_file: Path,
    data_dir: Path,
    *,
    database_url: str | None = None,
    graph: FalkorSnapshots | None = None,
) -> dict:
    """Authenticate all records before restoring a fresh authority and isolated graph."""
    from .store import MemoryStore

    with read_backup(archive, key_file) as (manifest, source):
        descriptions = _validate_inventory(manifest, source)
        if manifest.graph_mode == "native" and graph is None:
            raise InvalidRequest("Native recovery requires an explicitly configured isolated graph")
        if manifest.graph_mode == "rebuild" and graph is not None:
            raise InvalidRequest("Source-only backups require graph re-extraction after recovery")
        data_dir = Path(data_dir)
        if not database_url and (data_dir.exists() or data_dir.is_symlink()):
            raise Conflict("SQLite authority recovery requires a new destination directory")
        data_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        context = (
            tempfile.TemporaryDirectory(prefix=".authority-restore-", dir=data_dir.parent)
            if not database_url
            else nullcontext(str(data_dir))
        )
        created, committed = [], False
        with context as staged:
            staging = Path(staged)
            store = MemoryStore(
                staging, database_url=database_url, authority_id=str(manifest.authority_id)
            )
            try:
                with store.persistence.maintenance_guard():
                    _empty_target(store)
                    # On PostgreSQL this shared control transaction also contains
                    # every source/outbox session and transactional schema creation.
                    with store._control() as db, store._transaction(db):
                        _restore_tables(
                            store, descriptions, source, manifest.graph_mode == "native"
                        )
                        if graph is not None:
                            catalog = store._table(db, "projection_generations")
                            generations = [
                                dict(row) for row in db.execute(select(catalog)).mappings()
                            ]
                            created = graph.restore(
                                store.projection_worlds(),
                                generations,
                                manifest.metadata["graph"],
                                source,
                            )
                    if not database_url:
                        store.persistence.close()
                        os.rename(staging, data_dir)
                    committed = True
            finally:
                store.persistence.close()
                if not committed and created and graph is not None:
                    graph.rollback(created)
        return {
            "authority_id": manifest.authority_id.hex,
            "backup_id": str(manifest.backup_id),
            "source_backend": "postgres" if database_url else "sqlite",
            "graph_mode": manifest.graph_mode,
            "tables": len(descriptions),
            "rows": sum(item.rows for item in descriptions),
            "restored_graphs": len(created),
        }
