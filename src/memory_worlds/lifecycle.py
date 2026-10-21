"""Explicit source lifecycle and bounded retention for one authority.

Tombstones and purge intents commit with the source change. Graph deletion is a
separate maintenance operation: a receipt never equates a SQL commit with an
external namespace having been erased.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import Column, Table, Text, exists, or_, select

from .errors import Conflict, Forbidden, NotFound
from .errors import MemoryError as InvalidRequest
from .models import Model, Principal, ScopeSelection
from .persistence import CONTROL_METADATA, SOURCE_METADATA

Table(
    "lifecycle_tombstones",
    SOURCE_METADATA,
    Column("source_id", Text, primary_key=True),
    Column("episode_id", Text, nullable=False, unique=True),
    Column("state", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("changed_at", Text, nullable=False),
    Column("operation_id", Text, nullable=False),
)
Table(
    "lifecycle_operations",
    SOURCE_METADATA,
    Column("id", Text, primary_key=True),
    Column("request_digest", Text, nullable=False),
    Column("receipt", Text, nullable=False),
)
Table(
    "graph_purges",
    SOURCE_METADATA,
    Column("generation", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("requested_at", Text, nullable=False),
    Column("completed_at", Text),
    Column("error", Text),
)
Table(
    "retention_operations",
    CONTROL_METADATA,
    Column("id", Text, primary_key=True),
    Column("request_digest", Text, nullable=False),
    Column("plan", Text, nullable=False),
    Column("receipt", Text),
    Column("created_at", Text, nullable=False),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def visible_clause(store, db, episodes_table=None):
    """SQL expression used by every ordinary source/projection read."""
    episodes = episodes_table if episodes_table is not None else store._table(db, "episodes")
    tombstones = store._table(db, "lifecycle_tombstones")
    return ~exists(
        select(tombstones.c.source_id)
        .where(tombstones.c.source_id == episodes.c.source_id)
        .correlate(episodes)
    )


def assert_source_live(store, db, source_id: str) -> None:
    """Called before replay detection, so an exact old import cannot resurrect."""
    table = store._table(db, "lifecycle_tombstones")
    if db.execute(select(table.c.source_id).where(table.c.source_id == source_id)).first():
        raise Conflict("Source identity is tombstoned and cannot be imported or captured again")


def has_pending_purges(store, world_id: str, scope_id) -> bool:
    with store._world_db(world_id, scope_id) as db:
        table = store._table(db, "graph_purges")
        return (
            db.execute(select(table.c.generation).where(table.c.state != "purged").limit(1)).first()
            is not None
        )


class LifecycleRequest(Model):
    operation_id: UUID
    action: Literal["retract", "erase"]
    episode_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("episode_ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("Episode IDs must contain between 1 and 100 characters")
        if len(values) != len(set(values)):
            raise ValueError("Episode IDs must be unique")
        return values


class EpisodeLifecycle:
    def __init__(self, store):
        self.store = store
        store.persistence.ensure_control_tables()

    def _manager(self, db, principal: Principal, world_id: str) -> None:
        self.store._authorized(db, principal, world_id, write=True)
        if principal.admin:
            return
        sessions = self.store._table(db, "identity_sessions")
        if db.execute(
            select(sessions.c.credential_id).where(
                sessions.c.credential_id == principal.credential_id
            )
        ).first():
            memberships = self.store._table(db, "memberships")
            manager = db.execute(
                select(memberships.c.user_id).where(
                    memberships.c.user_id == principal.user_id,
                    memberships.c.world_id == world_id,
                    memberships.c.role == "manager",
                )
            ).first()
        else:
            grants = self.store._table(db, "grants")
            manager = db.execute(
                select(grants.c.credential_id).where(
                    grants.c.credential_id == principal.credential_id,
                    grants.c.world_id == world_id,
                    grants.c.role == "manager",
                )
            ).first()
        if manager is None:
            raise Forbidden("Lifecycle actions require a world manager and scope manage authority")
        contexts = self.store._table(db, "credential_contexts")
        parent = db.execute(
            select(contexts.c.parent_credential_id).where(
                contexts.c.credential_id == principal.credential_id
            )
        ).scalar()
        if parent is not None:
            credentials = self.store._table(db, "credentials")
            row = db.execute(select(credentials).where(credentials.c.id == parent)).mappings().one()
            self._manager(db, self.store._credential_principal(db, row), world_id)

    def _authorize(self, db, principal, world_id, scope_id):
        self._manager(db, principal, world_id)
        if principal.admin:
            return self.store._scope_row(db, world_id, scope_id)
        return self.store._scope_authorized(db, principal, world_id, scope_id, "manage")

    def _generations(self, db, world_id, scope_id, inventory):
        projection = self.store._table(db, "projection")
        jobs = self.store._table(db, "projection_jobs")
        generations = {row["generation"] for row in inventory if row["state"] != "purged"}
        generations.update(db.execute(select(projection.c.generation)).scalars())
        generations.update(db.execute(select(jobs.c.generation).distinct()).scalars())
        return sorted(value for value in generations if value)

    def _targets(self, db, request):
        episodes = self.store._table(db, "episodes")
        tombstones = self.store._table(db, "lifecycle_tombstones")
        rows = {
            row["id"]: dict(row)
            for row in db.execute(
                select(episodes).where(episodes.c.id.in_(request.episode_ids))
            ).mappings()
        }
        deleted = {
            row["episode_id"]: dict(row)
            for row in db.execute(
                select(tombstones).where(tombstones.c.episode_id.in_(request.episode_ids))
            ).mappings()
        }
        if any(item not in rows and item not in deleted for item in request.episode_ids):
            raise NotFound("One or more lifecycle episode targets were not found in this scope")
        if request.action == "retract" and any(
            item in deleted and deleted[item]["state"] == "erased" for item in request.episode_ids
        ):
            raise Conflict("An erased episode cannot become retained evidence again")
        return rows, deleted

    def _queue(self, db, generations, *, reason="lifecycle"):
        table = self.store._table(db, "graph_purges")
        for generation in generations:
            self.store.persistence.insert_ignore(
                db,
                table,
                {
                    "generation": generation,
                    "state": "pending",
                    "reason": reason,
                    "requested_at": _now(),
                },
                ["generation"],
            )
            if reason == "lifecycle":
                db.execute(
                    table.update()
                    .where(table.c.generation == generation, table.c.state != "purged")
                    .values(reason="lifecycle")
                )

    def _view(self, db, receipt):
        purges = self.store._table(db, "graph_purges")
        pending = list(
            db.execute(
                select(purges.c.generation)
                .where(purges.c.state != "purged", purges.c.generation.in_(receipt["generations"]))
                .order_by(purges.c.generation)
            ).scalars()
        )
        result = {
            **receipt,
            "pending_generations": pending,
            "graph_state": "pending" if pending else "purged",
        }
        result["erasure_complete"] = (
            receipt["action"] == "erase" and receipt["source_checkpointed"] and not pending
        )
        return result

    def preview(self, principal, world_id, scope_id, request: LifecycleRequest) -> dict:
        with self.store._control() as control:
            scope = self._authorize(control, principal, world_id, scope_id)
            inventory = self.store.generation_inventory(world_id, scope["id"])
            with self.store._world_db(world_id, scope["id"]) as db:
                rows, _ = self._targets(db, request)
                generations = self._generations(db, world_id, scope["id"], inventory)
        return {
            "operation_id": str(request.operation_id),
            "action": request.action,
            "authority_id": self.store.authority_id,
            "world_id": world_id,
            "scope_id": scope["id"],
            "episode_ids": sorted(request.episode_ids),
            "retained_source_records": len(rows),
            "generations_to_purge": generations,
            "writes": False,
        }

    def apply(self, principal, world_id, scope_id, request: LifecycleRequest) -> dict:
        with self.store.persistence.maintenance_guard():
            result = self._apply_source(principal, world_id, scope_id, request)
            if request.action == "erase":
                # The deletion transaction must be closed before native SQLite
                # WAL truncation. A failed/busy checkpoint leaves the durable
                # operation incomplete and retryable, even with no graph data.
                self.store.persistence.checkpoint_source(world_id, result["scope_id"])
                with self.store._control() as control:
                    self._authorize(control, principal, world_id, result["scope_id"])
                    with self.store._world_db(world_id, result["scope_id"]) as db:
                        table = self.store._table(db, "lifecycle_operations")
                        value = db.execute(
                            select(table.c.receipt).where(table.c.id == str(request.operation_id))
                        ).scalar_one()
                        receipt = json.loads(value)
                        receipt["source_checkpointed"] = True
                        db.execute(
                            table.update()
                            .where(table.c.id == str(request.operation_id))
                            .values(receipt=_json(receipt))
                        )
                        result = self._view(db, receipt)
            return result

    def _apply_source(self, principal, world_id, scope_id, request: LifecycleRequest) -> dict:
        # Freeze all registered writers before taking the namespace inventory.
        # This short barrier does not claim unknown timed-out external writes
        # have stopped: only the explicit final purge attestation can do that.
        with self.store.persistence.maintenance_guard():
            with self.store._control() as control, self.store._transaction(control):
                scope = self._authorize(control, principal, world_id, scope_id)
                sid = scope["id"]
                digest = _digest(
                    {
                        "world_id": world_id,
                        "scope_id": sid,
                        "action": request.action,
                        "episode_ids": sorted(request.episode_ids),
                    }
                )
                inventory = self.store.generation_inventory(world_id, sid)
                with self.store._world_db(world_id, sid) as db, self.store._transaction(db):
                    operations = self.store._table(db, "lifecycle_operations")
                    old = (
                        db.execute(
                            select(operations).where(operations.c.id == str(request.operation_id))
                        )
                        .mappings()
                        .first()
                    )
                    if old:
                        if old["request_digest"] != digest:
                            raise Conflict("Lifecycle operation ID already has different input")
                        return self._view(db, json.loads(old["receipt"]))
                    rows, deleted = self._targets(db, request)
                    generations = self._generations(db, world_id, sid, inventory)
                    self._queue(db, generations)
                    tombstones = self.store._table(db, "lifecycle_tombstones")
                    episodes = self.store._table(db, "episodes")
                    jobs = self.store._table(db, "projection_jobs")
                    audit = self.store._table(db, "audit")
                    now = _now()
                    for episode_id in request.episode_ids:
                        row = rows.get(episode_id)
                        prior = deleted.get(episode_id)
                        values = dict(
                            source_id=row["source_id"] if row else prior["source_id"],
                            episode_id=episode_id,
                            state="erased" if request.action == "erase" else "retracted",
                            owner_id=row["actor_id"] if row else prior["owner_id"],
                            changed_at=now,
                            operation_id=str(request.operation_id),
                        )
                        if prior:
                            db.execute(
                                tombstones.update()
                                .where(tombstones.c.episode_id == episode_id)
                                .values(**values)
                            )
                        else:
                            db.execute(tombstones.insert().values(**values))
                        db.execute(jobs.delete().where(jobs.c.episode_id == episode_id))
                        if request.action == "erase":
                            db.execute(episodes.delete().where(episodes.c.id == episode_id))
                        db.execute(
                            audit.insert().values(
                                event=request.action,
                                episode_id=episode_id,
                                actor_id=principal.user_id,
                                occurred_at=now,
                            )
                        )
                    projection = self.store._table(db, "projection")
                    db.execute(
                        projection.update().values(
                            state="degraded" if generations else "disabled", generation=None
                        )
                    )
                    receipt = {
                        "operation_id": str(request.operation_id),
                        "action": request.action,
                        "authority_id": self.store.authority_id,
                        "world_id": world_id,
                        "scope_id": sid,
                        "episode_ids": sorted(request.episode_ids),
                        "generations": generations,
                        "source_state": "erased" if request.action == "erase" else "retracted",
                        "source_checkpointed": False,
                        "created_at": now,
                        "boundaries": {
                            "published_copies": "separate-authority",
                            "backups": "separate-retention",
                        },
                    }
                    db.execute(
                        operations.insert().values(
                            id=str(request.operation_id),
                            request_digest=digest,
                            receipt=_json(receipt),
                        )
                    )
                    return self._view(db, receipt)

    def receipt(self, principal, world_id, scope_id, operation_id: UUID) -> dict:
        with self.store._control() as control:
            scope = self._authorize(control, principal, world_id, scope_id)
            with self.store._world_db(world_id, scope["id"]) as db:
                operations = self.store._table(db, "lifecycle_operations")
                value = db.execute(
                    select(operations.c.receipt).where(operations.c.id == str(operation_id))
                ).scalar()
                if value is None:
                    raise NotFound("Lifecycle operation not found")
                return self._view(db, json.loads(value))

    async def purge(
        self, principal, world_id, scope_id, adapter, *, workers_drained: bool = False
    ) -> dict:
        if not workers_drained:
            raise InvalidRequest("Graph purge requires explicit workers-drained confirmation")
        async with self.store.persistence.async_maintenance_guard():
            with self.store._control() as control:
                scope = self._authorize(control, principal, world_id, scope_id)
                sid = scope["id"]
                row = self.store._authorized(control, principal, world_id)
            world = self.store._hydrate(row, sid)
            inventory = self.store.generation_inventory(world_id, sid)
            with self.store._world_db(world_id, sid) as db, self.store._transaction(db):
                table = self.store._table(db, "graph_purges")
                lifecycle_pending = db.execute(
                    select(table.c.generation).where(
                        table.c.state != "purged", table.c.reason == "lifecycle"
                    )
                ).first()
                # Re-inventory only a pending lifecycle invalidation. A later
                # retention purge must preserve a clean active generation.
                if lifecycle_pending:
                    self._queue(db, self._generations(db, world_id, sid, inventory))
                pending = list(
                    db.execute(
                        select(table.c.generation)
                        .where(table.c.state != "purged")
                        .order_by(table.c.generation)
                    ).scalars()
                )
            completed, failed = [], []
            for generation in pending:
                try:
                    if adapter is None:
                        raise InvalidRequest("A graph adapter is required for namespace purge")
                    await adapter.purge_generation(world, generation)
                except Exception:
                    # Provider exception strings can include queries or source
                    # content. Persist only a fixed operational error category.
                    with self.store._world_db(world_id, sid) as db:
                        table = self.store._table(db, "graph_purges")
                        db.execute(
                            table.update()
                            .where(table.c.generation == generation)
                            .values(state="failed", error="graph-purge-failed", completed_at=None)
                        )
                        projection = self.store._table(db, "projection")
                        db.execute(projection.update().values(state="degraded"))
                    failed.append(generation)
                    continue
                with self.store._world_db(world_id, sid) as db:
                    table = self.store._table(db, "graph_purges")
                    db.execute(
                        table.update()
                        .where(table.c.generation == generation)
                        .values(state="purged", completed_at=_now(), error=None)
                    )
                with self.store._control() as db:
                    table = self.store._table(db, "projection_generations")
                    db.execute(
                        table.update()
                        .where(
                            table.c.world_id == world_id,
                            table.c.scope_id == sid,
                            table.c.generation == generation,
                        )
                        .values(state="purged", retired_at=_now())
                    )
                completed.append(generation)
            if not failed and lifecycle_pending:
                with self.store._world_db(world_id, sid) as db:
                    table = self.store._table(db, "projection")
                    db.execute(table.update().values(generation=None, state="disabled"))
            return {
                "authority_id": self.store.authority_id,
                "world_id": world_id,
                "scope_id": sid,
                "purged_generations": completed,
                "failed_generations": failed,
                "graph_state": "pending" if failed else "purged",
            }


class RetentionPolicy(Model):
    scopes: list[ScopeSelection] = Field(default_factory=list, max_length=100)
    source_before: AwareDatetime | None = None
    expired_invocations_before: AwareDatetime | None = None
    terminal_deliveries_before: AwareDatetime | None = None
    retired_generations_before: AwareDatetime | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator(
        "source_before",
        "expired_invocations_before",
        "terminal_deliveries_before",
        "retired_generations_before",
    )
    @classmethod
    def utc_cutoff(cls, value):
        return value.astimezone(UTC) if value else value

    @model_validator(mode="after")
    def explicit_policy(self):
        cutoffs = [
            self.source_before,
            self.expired_invocations_before,
            self.terminal_deliveries_before,
            self.retired_generations_before,
        ]
        if not any(cutoffs):
            raise ValueError("At least one explicit retention cutoff is required")
        if bool(self.scopes) != (self.source_before is not None):
            raise ValueError("Source retention requires both explicit scopes and source_before")
        if len({(scope.world_id, scope.scope_id) for scope in self.scopes}) != len(self.scopes):
            raise ValueError("Retention scopes must be unique")
        if any(value and value > datetime.now(UTC) for value in cutoffs):
            raise ValueError("Retention cutoffs cannot be in the future")
        return self


class RetentionRequest(Model):
    operation_id: UUID
    policy: RetentionPolicy


class RetentionEngine:
    """Administrator-selected policies freeze bounded target IDs before writes.

    Apply is resumable across SQLite journals: its plan is durable before any
    per-scope lifecycle operation, and deterministic child operation IDs make a
    crash between a source commit and the final receipt safe to retry.
    """

    def __init__(self, store):
        self.store = store
        self.lifecycle = EpisodeLifecycle(store)

    def _plan(self, principal, policy: RetentionPolicy):
        with self.store._control() as db:
            self.store._admin(db, principal)
            scopes = {
                (scope.world_id, str(scope.scope_id)): policy.source_before
                for scope in policy.scopes
            }
            if policy.expired_invocations_before:
                scope_table = self.store._table(db, "scopes")
                contexts = self.store._table(db, "credential_contexts")
                rows = db.execute(
                    select(scope_table.c.world_id, scope_table.c.id)
                    .join(contexts, contexts.c.invocation_id == scope_table.c.invocation_id)
                    .where(
                        scope_table.c.kind == "invocation-private",
                        contexts.c.expires_at <= policy.expired_invocations_before.timestamp(),
                    )
                    .order_by(scope_table.c.world_id, scope_table.c.id)
                    .limit(policy.limit + 1)
                )
                for row in rows.mappings():
                    scopes[(row["world_id"], row["id"])] = None
            for world_id, sid in scopes:
                self.store._scope_row(db, world_id, sid)
        targets, remaining = [], False
        for (world_id, sid), cutoff in sorted(scopes.items()):
            if len(targets) >= policy.limit:
                remaining = True
                break
            with self.store._world_db(world_id, sid) as db:
                episodes = self.store._table(db, "episodes")
                # Retained retractions are eligible for physical source erasure.
                tombstones = self.store._table(db, "lifecycle_tombstones")
                statement = select(episodes.c.id).where(
                    or_(
                        visible_clause(self.store, db, episodes),
                        exists(
                            select(tombstones.c.source_id)
                            .where(
                                tombstones.c.source_id == episodes.c.source_id,
                                tombstones.c.state == "retracted",
                            )
                            .correlate(episodes)
                        ),
                    )
                )
                if cutoff:
                    statement = statement.where(episodes.c.received_at < cutoff.isoformat())
                count = policy.limit - len(targets)
                ids = list(
                    db.execute(statement.order_by(episodes.c.sequence).limit(count + 1)).scalars()
                )
                remaining |= len(ids) > count
                targets.extend(
                    {
                        "kind": "episode",
                        "world_id": world_id,
                        "scope_id": sid,
                        "episode_id": episode_id,
                    }
                    for episode_id in ids[:count]
                )
        if policy.terminal_deliveries_before and len(targets) < policy.limit:
            with self.store.persistence.outbox() as db:
                table = self.store._table(db, "deliveries")
                count = policy.limit - len(targets)
                rows = (
                    db.execute(
                        select(table.c.id)
                        .where(
                            table.c.state.in_(["delivered", "blocked"]),
                            table.c.pruned == 0,
                            table.c.created_at < policy.terminal_deliveries_before.isoformat(),
                        )
                        .order_by(table.c.created_at, table.c.id)
                        .limit(count + 1)
                    )
                    .scalars()
                    .all()
                )
                remaining |= len(rows) > count
                targets.extend({"kind": "delivery", "delivery_id": value} for value in rows[:count])
        if policy.retired_generations_before and len(targets) < policy.limit:
            count = policy.limit - len(targets)
            with self.store._control() as db:
                table = self.store._table(db, "projection_generations")
                cutoff = policy.retired_generations_before.isoformat()
                rows = (
                    db.execute(
                        select(table)
                        .where(
                            table.c.state.in_(["retired", "failed"]),
                            or_(
                                table.c.retired_at < cutoff,
                                (table.c.retired_at.is_(None)) & (table.c.created_at < cutoff),
                            ),
                        )
                        .order_by(table.c.created_at, table.c.generation)
                        .limit(count + 1)
                    )
                    .mappings()
                    .all()
                )
            remaining |= len(rows) > count
            targets.extend(
                {
                    "kind": "generation",
                    "world_id": row["world_id"],
                    "scope_id": row["scope_id"],
                    "generation": row["generation"],
                }
                for row in rows[:count]
            )
        return {
            "authority_id": self.store.authority_id,
            "targets": targets,
            "selected": len(targets),
            "more_available": remaining,
            "policy": policy.model_dump(mode="json"),
        }

    def preview(self, principal, request: RetentionRequest) -> dict:
        return {
            "operation_id": str(request.operation_id),
            **self._plan(principal, request.policy),
            "writes": False,
        }

    def _view(self, plan, operation_id, state):
        return {
            "operation_id": str(operation_id),
            "authority_id": self.store.authority_id,
            "state": state,
            "selected": plan["selected"],
            "more_available": plan["more_available"],
            "targets": plan["targets"],
            "boundaries": {
                "published_copies": "separate-authority",
                "backups": "separate-retention",
                "graph_purge": "requires-workers-drained-maintenance",
            },
        }

    def apply(self, principal, request: RetentionRequest) -> dict:
        digest = _digest(request.policy.model_dump(mode="json"))
        with self.store._control() as db:
            self.store._admin(db, principal)
            table = self.store._table(db, "retention_operations")
            old = (
                db.execute(select(table).where(table.c.id == str(request.operation_id)))
                .mappings()
                .first()
            )
        if old is not None:
            if old["request_digest"] != digest:
                raise Conflict("Retention operation ID already has a different policy")
            if old["receipt"]:
                return json.loads(old["receipt"])
            plan = json.loads(old["plan"])
        else:
            plan = self._plan(principal, request.policy)
            with self.store._control() as db, self.store._transaction(db):
                self.store._admin(db, principal)
                table = self.store._table(db, "retention_operations")
                self.store.persistence.insert_ignore(
                    db,
                    table,
                    {
                        "id": str(request.operation_id),
                        "request_digest": digest,
                        "plan": _json(plan),
                        "created_at": _now(),
                    },
                    ["id"],
                )
                row = (
                    db.execute(select(table).where(table.c.id == str(request.operation_id)))
                    .mappings()
                    .one()
                )
                if row["request_digest"] != digest:
                    raise Conflict("Retention operation ID already has a different policy")
                plan = json.loads(row["plan"])
        grouped = {}
        outcomes = []
        for target in plan["targets"]:
            if target["kind"] == "episode":
                grouped.setdefault((target["world_id"], target["scope_id"]), []).append(
                    target["episode_id"]
                )
        for (world_id, sid), episode_ids in grouped.items():
            for offset in range(0, len(episode_ids), 100):
                child = uuid5(request.operation_id, f"source:{world_id}:{sid}:{offset}")
                self.lifecycle.apply(
                    principal,
                    world_id,
                    sid,
                    LifecycleRequest(
                        operation_id=child,
                        action="erase",
                        episode_ids=episode_ids[offset : offset + 100],
                    ),
                )
        for target in plan["targets"]:
            # Every effect revalidates current administrator authority. A durable
            # intent does not keep a revoked credential alive for later retries.
            with self.store._control() as control:
                self.store._admin(control, principal)
                if target["kind"] == "delivery":
                    with self.store.persistence.outbox() as db:
                        table = self.store._table(db, "deliveries")
                        db.execute(
                            table.update()
                            .where(
                                table.c.id == target["delivery_id"],
                                table.c.state.in_(["delivered", "blocked"]),
                                table.c.pruned == 0,
                            )
                            .values(
                                payload="{}", principal="{}", credential_id="", error=None, pruned=1
                            )
                        )
                        row = db.execute(
                            select(table.c.pruned).where(table.c.id == target["delivery_id"])
                        ).first()
                        outcomes.append(
                            {**target, "state": "pruned" if row and row.pruned else "preserved"}
                        )
                elif target["kind"] == "generation":
                    # A generation that became active since preview is preserved.
                    catalog = self.store._table(control, "projection_generations")
                    row = (
                        control.execute(
                            select(catalog).where(
                                catalog.c.world_id == target["world_id"],
                                catalog.c.scope_id == target["scope_id"],
                                catalog.c.generation == target["generation"],
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if row and row["state"] in {"retired", "failed"}:
                        with self.store._world_db(target["world_id"], target["scope_id"]) as db:
                            self.lifecycle._queue(db, [target["generation"]], reason="retention")
                            projection = self.store._table(db, "projection")
                            db.execute(projection.update().values(state="degraded"))
                        outcomes.append({**target, "state": "purge-pending"})
                    else:
                        outcomes.append(
                            {
                                **target,
                                "state": "purged"
                                if row and row["state"] == "purged"
                                else "preserved",
                            }
                        )
                else:
                    outcomes.append({**target, "state": "source-erased"})
        receipt = self._view(plan, request.operation_id, "applied")
        receipt["outcomes"] = outcomes
        with self.store._control() as db:
            self.store._admin(db, principal)
            table = self.store._table(db, "retention_operations")
            db.execute(
                table.update()
                .where(table.c.id == str(request.operation_id))
                .values(receipt=_json(receipt))
            )
        return receipt

    def receipt(self, principal, operation_id: UUID):
        with self.store._control() as db:
            self.store._admin(db, principal)
            table = self.store._table(db, "retention_operations")
            row = (
                db.execute(select(table).where(table.c.id == str(operation_id))).mappings().first()
            )
            if row is None:
                raise NotFound("Retention operation not found")
            return (
                json.loads(row["receipt"])
                if row["receipt"]
                else self._view(json.loads(row["plan"]), operation_id, "pending")
            )
