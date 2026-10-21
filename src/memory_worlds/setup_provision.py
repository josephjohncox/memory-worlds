"""Stopped-authority setup with durable secrets and transactional resource identities.

The private journal records intent before files or SQL credentials are created.
It is not a second source schema: the authority metadata owns the canonical IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import sysconfig
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from .errors import Conflict, Forbidden
from .errors import MemoryError as InvalidRequest
from .locking import DaemonLock
from .models import CreateToken, CreateWorld, Role, WorldKind
from .scopes import ALL_OPERATIONS
from .setup_config import SetupConfig
from .setup_files import (
    check_path,
    ensure_private_dir,
    load_state,
    private_read,
    read_json,
    setup_lock,
    write_private,
)
from .store import MemoryStore

_MAXIMUM = 1024 * 1024
_TOKEN = re.compile(r"mw_([0-9a-f]{32})\.[A-Za-z0-9_-]{43}")


def _json(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exists(path: Path) -> bool:
    return os.path.lexists(path)


def _identity(config: SetupConfig) -> dict:
    return {
        "instance": config.instance,
        "mode": config.mode,
        "config_digest": config.digest,
        "user_id": config.owner,
        "data_dir": str(config.data_dir),
        "state_dir": str(config.state_dir),
    }


def _matching(config: SetupConfig, value: dict):
    if any(
        value.get(key) != expected
        for key, expected in _identity(config).items()
        if key != "config_digest"
    ):
        raise Conflict("Setup configuration or authority paths changed; review the existing setup")


def _journal(config: SetupConfig, value: dict):
    path = config.path("provision.json")
    previous = _hash(private_read(path, maximum=_MAXIMUM, managed=True)) if _exists(path) else None
    write_private(path, _json(value), expected_hash=previous)


def _read_journal(config: SetupConfig) -> dict:
    value = read_json(config.path("provision.json"))
    _matching(config, value)
    try:
        UUID(value["authority_id"])
        if value["stage"] not in {"prepared", "provisioned", "reconfiguring", "complete"}:
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{64}", value["config_digest"]):
            raise ValueError
        kinds = {"personal", "work"} if config.mode == "hybrid" else {"personal"}
        if not isinstance(value["resources"], dict) or set(value["resources"]) != kinds:
            raise ValueError
        for kind, resource in value["resources"].items():
            if set(resource) != {"world_id", "scope_id", "credential_id", "agent_id", "token_file"}:
                raise ValueError
            if any(
                not re.fullmatch(r"[0-9a-f]{32}", resource[key])
                for key in ("world_id", "credential_id")
            ):
                raise ValueError
            for key in ("scope_id", "agent_id"):
                if str(UUID(resource[key])) != resource[key]:
                    raise ValueError
            if resource["token_file"] != str(config.path(f"{kind}.token")):
                raise ValueError
        if not re.fullmatch(r"[0-9a-f]{32}", value["operator_credential_id"]):
            raise ValueError
        if not isinstance(value["adopted"], bool) or not isinstance(
            value["operator_token_file"], str
        ):
            raise ValueError
        if not value["adopted"] and value["operator_token_file"] != str(
            config.path("operator.token")
        ):
            raise ValueError
        secret_names = {"graph.token", *(f"{kind}.token" for kind in kinds)}
        if not value["adopted"]:
            secret_names.add("operator.token")
        outputs = {
            "compose.graph.yaml",
            "mounts.json",
            "pi-personal.json",
            "pi-work.json",
            "pi-corporate.json",
            "mcp-personal.json",
            "mcp-work.json",
        }
        for field, names in (
            ("secrets", secret_names),
            ("files", outputs),
            ("file_intents", outputs),
        ):
            if not isinstance(value[field], dict):
                raise ValueError
            for raw_path, expected in value[field].items():
                path = Path(raw_path)
                if path.parent != config.state_dir or path.name not in names:
                    raise ValueError
                digests = (
                    [expected]
                    if field != "file_intents"
                    else [expected["before"], expected["after"]]
                )
                if any(
                    item is not None and not re.fullmatch(r"[0-9a-f]{64}", item) for item in digests
                ):
                    raise ValueError
        configuration = value["configuration"]
        encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
        if _hash(encoded) != value["config_digest"]:
            raise ValueError
        if not isinstance(value.get("prior_record", {}), dict):
            raise ValueError
        if (config.mode == "hybrid" and not isinstance(value.get("corporate"), dict)) or (
            config.mode == "local" and value.get("corporate") is not None
        ):
            raise ValueError
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value["compose_project"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Conflict(
            "Setup operation journal does not satisfy the current setup contract"
        ) from None
    return value


def _control_metadata(data_dir: Path) -> dict | None:
    """Inspect current SQLite ownership without constructing an initializing store."""
    _check_control_files(data_dir)
    path = data_dir / "control.sqlite3"
    if not _exists(path):
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024 * 1024:
        raise Conflict("Existing authority control storage is not a bounded regular file")
    try:
        with _readonly_control(data_dir) as db:
            rows = db.execute("SELECT key,value FROM metadata").fetchall()
        result = dict(rows)
        if sum(len(str(k)) + len(str(v)) for k, v in result.items()) > _MAXIMUM:
            raise Conflict("Authority setup metadata exceeds its size limit")
        return result
    except sqlite3.Error:
        raise Conflict("Existing authority cannot be adopted as current SQLite storage") from None


def _check_control_files(data_dir: Path) -> None:
    """Reject aliased SQLite storage before opening SQLite or creating its schema."""
    for name in ("control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm"):
        path = check_path(data_dir / name)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Conflict("Authority control storage must be regular files with one hard link")


@contextmanager
def _readonly_control(data_dir: Path):
    # With no WAL, immutable mode prevents even a read-only SQLite connection
    # creating new -wal/-shm files. Apply holds the stopped-daemon lock. A
    # remaining WAL must instead be read by SQLite's normal recovery protocol.
    _check_control_files(data_dir)
    path = data_dir / "control.sqlite3"
    suffix = "?mode=ro" if _exists(Path(str(path) + "-wal")) else "?mode=ro&immutable=1"
    db = sqlite3.connect(path.as_uri() + suffix, uri=True)
    try:
        yield db
    finally:
        db.close()


def _corporate(config: SetupConfig) -> dict | None:
    if config.mode != "hybrid":
        return None
    settings = config.corporate
    if settings is None:
        raise InvalidRequest("Hybrid setup requires a corporate connection")
    path = config.path("corporate.json")
    if not _exists(path):
        raise Conflict("Connect the corporate user before applying hybrid setup")
    bound = read_json(path)
    expected_path = config.path("corporate.token")
    selected = settings.world_ids
    if (
        not isinstance(bound, dict)
        or bound.get("url") != settings.url
        or bound.get("token_file") != str(expected_path)
        or not isinstance(bound.get("world_ids"), list)
        or any(not isinstance(item, str) for item in bound["world_ids"])
        or len(bound["world_ids"]) != len(set(bound["world_ids"]))
        or sorted(bound["world_ids"]) != sorted(selected)
        or any(
            not isinstance(bound.get(key), str) or not bound[key]
            for key in ("authority_id", "user_id", "credential_id", "token_sha256")
        )
        or _hash(private_read(expected_path, maximum=16_386, managed=True)) != bound["token_sha256"]
    ):
        raise Conflict(
            "Corporate connection does not match the selected setup; reconnect explicitly"
        )
    return {
        key: bound[key] for key in ("url", "authority_id", "user_id", "world_ids", "token_file")
    }


def _resource(config: SetupConfig, kind: str) -> dict:
    return {
        "world_id": uuid4().hex,
        "scope_id": str(uuid4()),
        "credential_id": uuid4().hex,
        "agent_id": str(uuid4()),
        "token_file": str(config.path(f"{kind}.token")),
    }


def _new_journal(config: SetupConfig, corporate: dict | None, metadata, adopt: Path | None):
    present = metadata is not None and metadata.get("bootstrapped") == "1"
    if present and adopt is None:
        raise Conflict("Existing authority requires explicit adoption and its administrator token")
    if adopt is not None and not present:
        raise Conflict("Adoption requires an already initialized local authority")
    authority_id = metadata["authority_id"] if present else uuid4().hex
    resources = {"personal": _resource(config, "personal")}
    if config.mode == "hybrid":
        resources["work"] = _resource(config, "work")
    result = {
        **_identity(config),
        "authority_id": authority_id,
        "adopted": present,
        "operator_token_file": str(adopt if present else config.path("operator.token")),
        "operator_credential_id": None if present else uuid4().hex,
        "resources": resources,
        "corporate": corporate,
        "secrets": {},
        "files": {},
        "file_intents": {},
        "stage": "prepared",
        "configuration": config.model_dump(mode="json"),
    }
    project_id = _hash(f"{config.instance}:{authority_id}".encode())[:20]
    result["compose_project"] = f"memory-worlds-{config.instance[:24]}-{project_id}"
    return result


def _secret(config: SetupConfig, journal: dict, name: str, *, committed: bool) -> bytes:
    path = config.path(name)
    key = str(path)
    expected = journal["secrets"].get(key)
    if _exists(path):
        data = private_read(path, maximum=256, managed=True)
        if expected is None or _hash(data) != expected:
            raise Conflict("Setup secret changed or is not owned by this setup")
        return data
    if committed:
        raise Conflict("A provisioned setup secret is missing; refusing credential replacement")
    if name == "graph.token":
        data = (secrets.token_hex(32) + "\n").encode()
    else:
        credential_id = (
            journal["operator_credential_id"]
            if name == "operator.token"
            else journal["resources"][name.removesuffix(".token")]["credential_id"]
        )
        data = f"mw_{credential_id}.{secrets.token_urlsafe(32)}\n".encode()
    journal["secrets"][key] = _hash(data)
    _journal(config, journal)  # Intent is durable before exclusive file creation.
    write_private(path, data)
    return data


def _token(data: bytes) -> str:
    try:
        value = data.decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise Conflict("Setup credential is invalid") from None
    if not _TOKEN.fullmatch(value):
        raise Conflict("Setup credential is invalid")
    return value


def _record(journal: dict) -> dict:
    return {
        key: journal[key]
        for key in (
            "instance",
            "mode",
            "config_digest",
            "user_id",
            "data_dir",
            "state_dir",
            "authority_id",
            "adopted",
            "operator_token_file",
            "operator_credential_id",
            "resources",
            "corporate",
            "secrets",
            "compose_project",
        )
    }


def _provision_database(store: MemoryStore, config: SetupConfig, journal: dict, tokens: dict):
    """One control transaction owns bootstrap, agents, worlds, scopes and the receipt."""
    with store._control() as db, store._transaction(db):
        table = store._table(db, "metadata")
        personal = journal["resources"]["personal"]
        if journal["adopted"]:
            principal = store.authenticate(tokens["operator"])
            store._admin(db, principal)
            journal["operator_credential_id"] = principal.credential_id
            personal_world = store._new_world(
                db,
                CreateWorld(name=f"Personal ({config.instance})"),
                config.owner,
                prepared_id=personal["world_id"],
            )
        else:
            personal_world, _ = store._bootstrap(
                db,
                config.owner,
                prepared_token=tokens["operator"],
                prepared_world_id=personal["world_id"],
            )
        for kind, resource in journal["resources"].items():
            world = (
                personal_world
                if kind == "personal"
                else store._new_world(
                    db,
                    CreateWorld(name=f"Work ({config.instance})", kind=WorldKind.PRIVATE_WORK),
                    config.owner,
                    prepared_id=resource["world_id"],
                )
            )
            store._token(
                db,
                CreateToken(
                    user_id=config.owner,
                    agent_id=resource["agent_id"],
                    grants={world.id: Role.WRITER},
                ),
                prepared_token=tokens[kind],
            )
            now = datetime.now(UTC).isoformat()
            identities = store._table(db, "named_identities")
            db.execute(
                identities.insert().values(
                    id=resource["agent_id"],
                    kind="agent",
                    owner_id=config.owner,
                    name=f"{kind.title()} agent ({config.instance})",
                    created_at=now,
                )
            )
            scopes = store._table(db, "scopes")
            db.execute(
                scopes.insert().values(
                    id=resource["scope_id"],
                    world_id=world.id,
                    kind="agent-persistent",
                    name=f"{kind.title()} notes",
                    alias="notes",
                    created_at=now,
                    creator_credential_id=resource["credential_id"],
                    agent_id=resource["agent_id"],
                )
            )
            grants = store._table(db, "scope_grants")
            db.execute(
                grants.insert(),
                [
                    {
                        "credential_id": resource["credential_id"],
                        "scope_id": resource["scope_id"],
                        "operation": operation,
                    }
                    for operation in sorted(ALL_OPERATIONS)
                ],
            )
            store._initialize_world(world.id, resource["scope_id"])
        db.execute(
            table.insert().values(
                key=f"setup:{config.instance}", value=_json(_record(journal)).decode()
            )
        )


def _verify_database(store: MemoryStore, journal: dict, tokens: dict):
    """Detect live grant drift; setup never repairs it by issuing another credential."""
    with store._control() as db:
        operator = store.authenticate(tokens["operator"])
        store._admin(db, operator)
        if operator.credential_id != journal["operator_credential_id"]:
            raise Conflict("Setup administrator binding changed")
        for kind, resource in journal["resources"].items():
            principal = store.authenticate(tokens[kind])
            if (
                principal.admin
                or principal.restricted
                or principal.user_id != journal["user_id"]
                or principal.credential_id != resource["credential_id"]
                or principal.agent_id != resource["agent_id"]
            ):
                raise Conflict("Setup agent identity changed")
            world = store.get_world(principal, resource["world_id"])
            expected_kind = WorldKind.PERSONAL if kind == "personal" else WorldKind.PRIVATE_WORK
            if world.kind != expected_kind or world.owner_id != journal["user_id"]:
                raise Conflict("Setup world audience changed")
            granted = store._table(db, "grants")
            actual = db.execute(
                select(granted.c.world_id, granted.c.role).where(
                    granted.c.credential_id == principal.credential_id
                )
            ).all()
            if actual != [(resource["world_id"], "writer")]:
                raise Conflict("Setup credential world grants changed")
            scopes = store._table(db, "scopes")
            scope = (
                db.execute(select(scopes).where(scopes.c.id == resource["scope_id"]))
                .mappings()
                .one_or_none()
            )
            if scope is None or any(
                scope[field] != expected
                for field, expected in {
                    "world_id": world.id,
                    "kind": "agent-persistent",
                    "creator_credential_id": principal.credential_id,
                    "agent_id": resource["agent_id"],
                }.items()
            ):
                raise Conflict("Setup private scope binding changed")
            grants = store._table(db, "scope_grants")
            owned = db.execute(
                select(grants.c.credential_id, grants.c.scope_id, grants.c.operation).where(
                    (grants.c.scope_id == resource["scope_id"])
                    | (grants.c.credential_id == principal.credential_id)
                )
            ).all()
            managed = {row for row in owned if row.scope_id == resource["scope_id"]}
            if managed != {
                (principal.credential_id, resource["scope_id"], operation)
                for operation in ALL_OPERATIONS
            }:
                raise Conflict("Setup private scope audience changed")
            extra_ids = {row.scope_id for row in owned if row.scope_id != resource["scope_id"]}
            if extra_ids:
                extra = (
                    db.execute(select(scopes).where(scopes.c.id.in_(extra_ids))).mappings().all()
                )
                if len(extra) != len(extra_ids) or any(
                    row["world_id"] != world.id
                    or row["creator_credential_id"] != principal.credential_id
                    or row["kind"] not in {"agent-persistent", "invocation-private", "run-shared"}
                    for row in extra
                ):
                    raise Conflict("Setup credential acquired access to an external private scope")
                for row in extra:
                    required = {
                        "agent-persistent": "agent_id",
                        "invocation-private": "invocation_id",
                        "run-shared": "run_id",
                    }[row["kind"]]
                    try:
                        UUID(row[required])
                        for field in ("agent_id", "invocation_id", "run_id"):
                            if row[field] is not None:
                                UUID(row[field])
                    except (ValueError, TypeError, AttributeError):
                        raise Conflict(
                            "An agent-created setup scope has an invalid identity"
                        ) from None
            identities = store._table(db, "named_identities")
            identity = (
                db.execute(select(identities).where(identities.c.id == resource["agent_id"]))
                .mappings()
                .one_or_none()
            )
            if (
                identity is None
                or identity["kind"] != "agent"
                or identity["owner_id"] != principal.user_id
            ):
                raise Conflict("Setup registered agent identity changed")


def _profile(url: str, token_file: str, *, resource=None, mounts=None) -> dict:
    result = {
        "url": url,
        "tokenFile": token_file,
        "readScopes": [],
        "readMountIds": mounts or [],
        "automaticCapture": False,
        "projectCaptured": True,
        "retrieval": {"enabled": False, "mode": "graph", "limit": 5, "maxChars": 8000},
    }
    if resource is not None:
        result["agent"] = resource["agent_id"]
        result["readScopes"] = [
            {"world_id": resource["world_id"], "scope_id": resource["scope_id"]}
        ]
    return result


def _generated(config: SetupConfig, state: dict) -> dict[str, bytes]:
    from .setup_runtime import graph_compose_bytes

    result = {"compose.graph.yaml": graph_compose_bytes(config, state)}
    mounts = {"version": 1, "mounts": []}
    for kind, resource in state["resources"].items():
        selected = ["corporate"] if kind == "work" and state["corporate"]["world_ids"] else []
        profile = _profile(config.url, resource["token_file"], resource=resource, mounts=selected)
        if kind == "personal":
            profile["writeScope"] = profile["readScopes"][0]
        if config.pi_profiles:
            result[f"pi-{kind}.json"] = _json(profile)
        if config.mcp_profiles:
            result[f"mcp-{kind}.json"] = _json(
                {
                    "command": str(Path(sysconfig.get_path("scripts")) / "memory-worlds"),
                    "args": ["--url", config.url, "--token-file", resource["token_file"], "mcp"],
                }
            )
    if state["corporate"] is not None:
        corporate = state["corporate"]
        if corporate["world_ids"]:
            mounts["mounts"].append(
                {
                    "id": "corporate",
                    "name": "Corporate shared knowledge",
                    "url": corporate["url"],
                    "authority_id": corporate["authority_id"],
                    "world_ids": corporate["world_ids"],
                    "token_file": corporate["token_file"],
                    "user_grants": {},
                    "credential_grants": {state["resources"]["work"]["credential_id"]: "reader"},
                    "publish": False,
                }
            )
        if config.pi_profiles:
            result["pi-corporate.json"] = _json(_profile(corporate["url"], corporate["token_file"]))
    result["mounts.json"] = _json(mounts)
    return result


def _write_owned(config: SetupConfig, journal: dict, name: str, data: bytes):
    path = config.path(name)
    key, desired = str(path), _hash(data)
    before = journal["files"].get(key)
    pending = journal["file_intents"].get(key)
    if _exists(path):
        current = _hash(private_read(path, maximum=_MAXIMUM, managed=True))
        if pending and current == pending["after"] == desired:
            journal["files"][key] = desired
            del journal["file_intents"][key]
            _journal(config, journal)
            return
        if before is None or current != before:
            raise Conflict("Generated setup output changed or belongs to another operation")
        if current == desired:
            return
    elif before is not None:
        raise Conflict("An owned setup output is missing; review before recreating it")
    journal["file_intents"][key] = {"before": before, "after": desired}
    _journal(config, journal)
    write_private(path, data, expected_hash=before)
    journal["files"][key] = desired
    del journal["file_intents"][key]
    _journal(config, journal)


def _check_files(journal: dict):
    for key, expected in {**journal["secrets"], **journal["files"]}.items():
        path = Path(key)
        if not _exists(path):
            raise Conflict("A managed setup file is missing; review before reapplying")
        actual = _hash(private_read(path, managed=True))
        pending = journal["file_intents"].get(key)
        if actual != expected and not (pending and actual == pending["after"]):
            raise Conflict("A managed setup file changed outside setup")


def _state_matches(previous: dict | None, journal: dict):
    if not previous:
        return
    records = [_record(journal)]
    if journal.get("prior_record") is not None:
        records.append(journal["prior_record"])
    if not any(
        all(previous.get(key) == value for key, value in record.items()) for record in records
    ):
        raise Conflict("Setup state and authority operation journal disagree")


def _finish_state(config: SetupConfig, journal: dict) -> dict:
    state = {
        **_record(journal),
        "files": {**journal["secrets"], **journal["files"]},
        "stage": "complete",
    }
    path = config.path("state.json")
    current = _hash(private_read(path, managed=True)) if _exists(path) else None
    data = _json(state)
    journal["state_intent"] = {"before": current, "after": _hash(data)}
    _journal(config, journal)
    write_private(path, data, expected_hash=current)
    journal["state_hash"] = _hash(data)
    journal.pop("state_intent", None)
    journal.pop("prior_record", None)
    journal["stage"] = "complete"
    _journal(config, journal)
    return state


def _apply_stopped(config: SetupConfig, corporate, previous, adopt_admin_file):
    journal_path = config.path("provision.json")
    metadata = _control_metadata(config.data_dir)
    ownership = metadata.get(f"setup:{config.instance}") if metadata else None
    owned = None
    if ownership is not None:
        try:
            owned = json.loads(ownership)
        except (ValueError, TypeError):
            raise Conflict("Setup ownership metadata is invalid") from None
        if not isinstance(owned, dict):
            raise Conflict("Setup ownership metadata is invalid")
    if _exists(journal_path):
        journal = _read_journal(config)
        _matching(config, journal)
        if metadata and metadata.get("authority_id") != journal["authority_id"]:
            raise Conflict("Setup points to a different local authority")
        if ownership is None and (
            previous or journal.get("stage") in {"provisioned", "complete", "reconfiguring"}
        ):
            raise Conflict("Provisioned authority ownership is missing; refusing reinitialization")
        _state_matches(previous, journal)
        if previous:
            actual = _hash(private_read(config.path("state.json"), managed=True))
            pending = journal.get("state_intent")
            expected = journal.get("state_hash")
            if actual != expected and not (
                pending and actual in {pending["before"], pending["after"]}
            ):
                raise Conflict("Setup state file changed outside its recorded operation")
    else:
        if ownership is not None or previous:
            raise Conflict("Setup operation journal is missing; refusing to infer ownership")
        journal = _new_journal(config, corporate, metadata, adopt_admin_file)
        if journal["adopted"]:
            token = _token(private_read(Path(journal["operator_token_file"]), maximum=256))
            with _readonly_control(config.data_dir) as db:
                row = db.execute(
                    "SELECT id,admin,revoked FROM credentials WHERE token_hash=?",
                    (_hash(token.encode()),),
                ).fetchone()
            if row is None or row[1] != 1 or row[2] != 0:
                raise Forbidden("Adoption requires a live existing local administrator")
            journal["operator_credential_id"] = row[0]
        _journal(config, journal)
    if owned is not None and owned not in [_record(journal), journal.get("prior_record")]:
        raise Conflict("Setup ownership metadata changed")
    if (
        ownership is None
        and metadata
        and metadata.get("bootstrapped") == "1"
        and not journal["adopted"]
    ):
        raise Conflict("Authority was initialized outside this setup operation")
    before_corporate = journal.get("corporate")
    if (before_corporate is None) != (corporate is None) or (
        corporate is not None
        and any(
            before_corporate.get(key) != corporate.get(key)
            for key in ("url", "authority_id", "user_id", "token_file")
        )
    ):
        raise Conflict("Corporate authority or user binding changed; create a separate setup")
    tokens = {}
    if journal["adopted"]:
        tokens["operator"] = _token(private_read(Path(journal["operator_token_file"]), maximum=256))
    else:
        tokens["operator"] = _token(
            _secret(config, journal, "operator.token", committed=ownership is not None)
        )
    for kind in journal["resources"]:
        tokens[kind] = _token(
            _secret(config, journal, f"{kind}.token", committed=ownership is not None)
        )
    graph = _secret(config, journal, "graph.token", committed=ownership is not None)
    if not re.fullmatch(rb"[0-9a-f]{64}\n", graph):
        raise Conflict("Setup graph credential is invalid")
    _check_control_files(config.data_dir)
    store = MemoryStore(config.data_dir, authority_id=journal["authority_id"])
    if store.authority_id != journal["authority_id"]:
        raise Conflict("Local authority binding changed")
    if ownership is not None:
        _verify_database(store, journal, tokens)
        _check_files(journal)
    if journal["config_digest"] != config.digest:
        if journal["stage"] != "complete" or journal.get("prior_record") is not None:
            raise Conflict(
                "Complete the interrupted setup with its saved configuration before changing it"
            )
        journal["prior_record"] = _record(journal)
        journal["config_digest"] = config.digest
        journal["configuration"] = config.model_dump(mode="json")
        journal["corporate"] = corporate
        journal["stage"] = "reconfiguring"
        _journal(config, journal)
    elif journal.get("corporate") != corporate:
        raise Conflict("Corporate selected worlds changed without a matching setup configuration")
    if ownership is None:
        _provision_database(store, config, journal, tokens)
    elif owned != _record(journal):
        with store._control() as db, store._transaction(db):
            table = store._table(db, "metadata")
            changed = db.execute(
                table.update()
                .where(
                    table.c.key == f"setup:{config.instance}",
                    table.c.value == ownership,
                )
                .values(value=_json(_record(journal)).decode())
            )
            if changed.rowcount != 1:
                raise Conflict("Authority setup ownership changed during configuration apply")
    _verify_database(store, journal, tokens)
    journal["stage"] = "provisioned"
    _journal(config, journal)
    generated = _generated(config, journal)
    for name, data in generated.items():
        _write_owned(config, journal, name, data)
    state = _finish_state(config, journal)
    return {
        "instance": config.instance,
        "authority_id": state["authority_id"],
        "user_id": config.owner,
        "replayed": ownership is not None,
        "resources": {
            kind: {key: value for key, value in resource.items() if key != "token_file"}
            for kind, resource in state["resources"].items()
        },
        "profiles": [str(config.path(name)) for name in generated if name.startswith("pi-")],
        "retained_outputs": sorted(
            key for key in journal["files"] if Path(key).name not in generated
        ),
    }


def apply_setup(config: SetupConfig, *, adopt_admin_file: Path | None = None) -> dict:
    """Provision or reconfigure one stopped SQLite authority without network/model calls."""
    adopt_admin_file = adopt_admin_file or config.adopt_admin_file
    with setup_lock(config):
        if adopt_admin_file is not None:
            adopt_admin_file = check_path(adopt_admin_file)
        corporate = _corporate(config)
        for provider in (config.extraction, config.embedding):
            if provider.api_key_file is not None:
                private_read(provider.api_key_file, maximum=32_768)
        previous = (
            load_state(config, require_digest=False) if _exists(config.path("state.json")) else None
        )
        if previous:
            _matching(config, previous)
        if not _exists(config.path("provision.json")):
            if previous:
                raise Conflict("Setup operation journal is missing; refusing to infer ownership")
            nonempty = config.data_dir.exists() and any(config.data_dir.iterdir())
            if nonempty and adopt_admin_file is None:
                raise Conflict(
                    "Nonempty authority directory requires explicit authenticated adoption"
                )
            if adopt_admin_file is not None:
                control = config.data_dir / "control.sqlite3"
                if not _exists(control) or not stat.S_ISREG(control.lstat().st_mode):
                    raise Conflict("Adoption requires an already initialized local authority")
            elif not nonempty:
                # Persist the authority/credential IDs BEFORE mkdir or DaemonLock.
                # A killed first attempt must not leave an unowned lock-only
                # directory that subsequent setup cannot safely recognize.
                _journal(config, _new_journal(config, corporate, None, None))
        ensure_private_dir(config.data_dir)
        with DaemonLock(config.data_dir):
            return _apply_stopped(config, corporate, previous, adopt_admin_file)
