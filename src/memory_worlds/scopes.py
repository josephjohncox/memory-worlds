"""Scope ACLs and attenuated agent invocations within a world authority.

The world grants are always the outer ceiling. A scope never broadens them.
Private journals are separate SQLite databases; no extraction sees sibling data.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text

from .errors import Conflict, Forbidden, NotFound, Unauthorized
from .errors import MemoryError as InvalidRequest
from .models import (
    AgentContext,
    BindContext,
    CreateScope,
    DelegateCredential,
    Principal,
    Role,
    Scope,
    ScopedPublication,
    ScopeFlow,
    ScopeKind,
    ScopeOperation,
    ScopeSelection,
    UpdateAlias,
    UpdateScopeGrant,
    WorldKind,
)

ALL_OPERATIONS = frozenset(op.value for op in ScopeOperation)
WRITE_OPERATIONS = frozenset({"write", "publish", "delegate", "manage"})


def scope_uuid(value: str | UUID) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise NotFound("Scope not found") from error


def world_shared_scope_id(authority_id: str, world_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"memory-worlds:{authority_id}:{world_id}:world-shared"))


def _expiry_ceiling(timestamp: float) -> datetime:
    """Represent an inherited expiry without rounding beyond its numeric bound."""
    bound = datetime.fromtimestamp(timestamp, UTC)
    if bound.timestamp() > timestamp:
        bound -= timedelta(microseconds=1)
    return bound


class ScopedStore:
    def _create_shared_scope(self, db, world):
        sid = world_shared_scope_id(self.authority_id, world.id)
        db.execute(
            text(
                f"INSERT INTO {self._table(db, 'scopes')} (id,"
                f"world_id,kind,name,created_at,alias) VALUES "
                f"(:p0,:p1,:p2,:p3,:p4,:p5)"
            ),
            {
                "p0": sid,
                "p1": world.id,
                "p2": ScopeKind.WORLD_SHARED.value,
                "p3": "World shared",
                "p4": world.created_at.isoformat(),
                "p5": "shared",
            },
        )

    def _scope_row(self, db, world_id: str, scope_id: str | UUID | None):
        if scope_id is None:
            scope_id = world_shared_scope_id(self.authority_id, world_id)
        row = (
            db.execute(
                text(f"SELECT * FROM {self._table(db, 'scopes')} WHERE id=:p0 AND world_id=:p1"),
                {"p0": scope_uuid(scope_id), "p1": world_id},
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            raise NotFound("Scope not found")
        return row

    def _credential_principal(self, db, row) -> Principal:
        context = (
            db.execute(
                text(
                    f"SELECT * FROM "
                    f"{self._table(db, 'credential_contexts')} "
                    f"WHERE credential_id=:p0"
                ),
                {"p0": row["id"]},
            )
            .mappings()
            .fetchone()
        )
        return Principal(
            credential_id=row["id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            admin=bool(row["admin"]),
            restricted=context is not None,
            agent_uuid=context["agent_uuid"] if context else None,
            invocation_id=context["invocation_id"] if context else None,
            parent_invocation_id=context["parent_invocation_id"] if context else None,
        )

    def _validate_ancestors(self, db, credential_id: str) -> None:
        seen = set()
        current_id = credential_id
        while True:
            if current_id in seen or len(seen) >= 32:
                raise Unauthorized("Credential delegation chain is invalid")
            seen.add(current_id)
            credential = (
                db.execute(
                    text(
                        f"SELECT * FROM {self._table(db, 'credentials')} WHERE id=:p0 AND revoked=0"
                    ),
                    {"p0": current_id},
                )
                .mappings()
                .fetchone()
            )
            if credential is None:
                raise Unauthorized("An ancestor credential is revoked")
            identity = (
                db.execute(
                    text(f"SELECT enabled FROM {self._table(db, 'identities')} WHERE user_id=:p0"),
                    {"p0": credential["user_id"]},
                )
                .mappings()
                .fetchone()
            )
            session = (
                db.execute(
                    text(
                        f"SELECT expires_at FROM "
                        f"{self._table(db, 'identity_sessions')} WHERE "
                        f"credential_id=:p0"
                    ),
                    {"p0": current_id},
                )
                .mappings()
                .fetchone()
            )
            if identity is not None and not identity["enabled"]:
                raise Unauthorized("Identity is disabled")
            if session is not None and session["expires_at"] <= time.time():
                raise Unauthorized("An ancestor identity session is expired")
            context = (
                db.execute(
                    text(
                        f"SELECT * FROM "
                        f"{self._table(db, 'credential_contexts')} "
                        f"WHERE credential_id=:p0"
                    ),
                    {"p0": current_id},
                )
                .mappings()
                .fetchone()
            )
            if context is None:
                return
            if context["expires_at"] <= time.time():
                raise Unauthorized("Delegated credential is expired")
            current_id = context["parent_credential_id"]

    def _scope_authorized(self, db, principal, world_id, scope_id, operation="read"):
        self._authorized(db, principal, world_id, write=operation in WRITE_OPERATIONS)
        row = self._scope_row(db, world_id, scope_id)
        current = principal
        seen = set()
        while True:
            if current.credential_id in seen or len(seen) >= 32:
                raise Forbidden("Scope delegation chain is invalid")
            seen.add(current.credential_id)
            context = (
                db.execute(
                    text(
                        f"SELECT * FROM "
                        f"{self._table(db, 'credential_contexts')} "
                        f"WHERE credential_id=:p0"
                    ),
                    {"p0": current.credential_id},
                )
                .mappings()
                .fetchone()
            )
            if row["kind"] == ScopeKind.WORLD_SHARED.value and context is None:
                return row
            grant = (
                db.execute(
                    text(
                        f"SELECT 1 FROM "
                        f"{self._table(db, 'scope_grants')} WHERE "
                        f"credential_id=:p0 AND scope_id=:p1 AND "
                        f"operation=:p2"
                    ),
                    {"p0": current.credential_id, "p1": row["id"], "p2": operation},
                )
                .mappings()
                .fetchone()
            )
            if grant is None:
                raise Forbidden("Scope operation denied")
            # The explicit creator grant anchors a new private audience. Its
            # ancestor created the invocation but does not acquire its contents.
            anchor = (
                db.execute(
                    text(
                        f"SELECT 1 FROM "
                        f"{self._table(db, 'scope_grant_anchors')} "
                        f"WHERE credential_id=:p0 AND scope_id=:p1 AND "
                        f"operation=:p2"
                    ),
                    {"p0": current.credential_id, "p1": row["id"], "p2": operation},
                )
                .mappings()
                .fetchone()
            )
            if context is None or row["creator_credential_id"] == current.credential_id or anchor:
                self._thread_scope_operation(db, row, operation)
                return row
            parent_row = (
                db.execute(
                    text(f"SELECT * FROM {self._table(db, 'credentials')} WHERE id=:p0"),
                    {"p0": context["parent_credential_id"]},
                )
                .mappings()
                .fetchone()
            )
            current = self._credential_principal(db, parent_row)
            self._scope_authorized_delegate(db, current, world_id, row)

    def _thread_scope_operation(self, db, scope, operation):
        """Thread metadata never grants access; enforce writes after the ACL check."""
        if scope["kind"] != ScopeKind.THREAD_PRIVATE.value:
            return
        from .thread_models import Thread

        table = self._table(db, "memory_threads")
        payload = db.execute(
            select(table.c.payload).where(
                table.c.id == scope["thread_id"],
                table.c.world_id == scope["world_id"],
                table.c.scope_id == scope["id"],
            )
        ).scalar_one_or_none()
        if payload is None:
            raise Conflict("Thread scope registration is incomplete")
        thread = Thread.model_validate_json(payload)
        if (
            str(thread.id) != scope["thread_id"]
            or str(thread.scope_id) != scope["id"]
            or thread.world_id != scope["world_id"]
        ):
            raise Conflict("Thread scope registration is inconsistent")
        if operation in {"write", "publish"} and (
            thread.state != "open"
            or thread.expires_at is not None
            and thread.expires_at.timestamp() <= self.persistence.database_time(db)
        ):
            raise Conflict("Thread no longer accepts new writes")

    def _scope_authorized_delegate(self, db, principal, world_id, scope):
        self._authorized(db, principal, world_id, write=True)
        if scope["kind"] == ScopeKind.WORLD_SHARED.value and not principal.restricted:
            return
        if (
            not db.execute(
                text(
                    f"SELECT 1 FROM "
                    f"{self._table(db, 'scope_grants')} WHERE "
                    f"credential_id=:p0 AND scope_id=:p1 AND "
                    f"operation='delegate'"
                ),
                {"p0": principal.credential_id, "p1": scope["id"]},
            )
            .mappings()
            .fetchone()
        ):
            raise Forbidden("An ancestor no longer permits delegation")

    def _hydrate_scope(self, row, control=None) -> Scope:
        with self._world_db(row["world_id"], row["id"]) as db:
            projection = (
                db.execute(text(f"SELECT generation,state FROM {self._table(db, 'projection')}"))
                .mappings()
                .fetchone()
            )
        with nullcontext(control) if control is not None else self._control() as db:
            coordinator = (
                db.execute(
                    text(
                        f"SELECT credential_id FROM "
                        f"{self._table(db, 'scope_grant_anchors')} "
                        f"WHERE scope_id=:p0 AND operation='read'"
                    ),
                    {"p0": row["id"]},
                )
                .mappings()
                .fetchone()
            )
        return Scope(
            **dict(row),
            generation=projection["generation"],
            projection_state=projection["state"],
            coordinator_credential_id=coordinator["credential_id"] if coordinator else None,
        )

    def create_scope(self, principal: Principal, world_id: str, request: CreateScope) -> Scope:
        if request.kind == ScopeKind.THREAD_PRIVATE or request.thread_id is not None:
            raise InvalidRequest("Create thread-private scopes through the thread registry")
        with self._control() as db, self._transaction(db):
            self._authorized(db, principal, world_id, write=True)
            if request.kind == ScopeKind.WORLD_SHARED:
                row = self._scope_authorized(db, principal, world_id, None, "write")
                return self._hydrate_scope(row)
            if principal.restricted:
                raise Forbidden("Delegated invocations create child scopes through delegation")
            run_id, agent_id, invocation_id = (
                request.run_id,
                request.agent_id,
                request.invocation_id,
            )
            if request.kind != ScopeKind.RUN_SHARED:
                self._validate_run(db, principal, run_id)
            if request.kind == ScopeKind.RUN_SHARED:
                if run_id is None:
                    run_id = uuid4()
                else:
                    known = (
                        db.execute(
                            text(
                                f"SELECT * FROM {self._table(db, 'scopes')} "
                                f"WHERE world_id=:p0 AND run_id=:p1 AND "
                                f"kind='run-shared'"
                            ),
                            {"p0": world_id, "p1": str(run_id)},
                        )
                        .mappings()
                        .fetchone()
                    )
                    if known is None:
                        raise InvalidRequest(
                            "Use a server-issued run UUID or omit it to create a run"
                        )
                    self._scope_authorized(db, principal, world_id, known["id"])
            if request.kind == ScopeKind.AGENT_PERSISTENT:
                agent_id = agent_id or uuid4()
            if request.kind == ScopeKind.INVOCATION_PRIVATE:
                if invocation_id is None:
                    invocation_id = uuid4()
                elif (
                    not db.execute(
                        text(
                            f"SELECT 1 FROM "
                            f"{self._table(db, 'agent_contexts')} WHERE "
                            f"credential_id=:p0 AND invocation_id=:p1"
                        ),
                        {"p0": principal.credential_id, "p1": str(invocation_id)},
                    )
                    .mappings()
                    .fetchone()
                ):
                    raise Forbidden("Invocation identity must belong to an existing agent context")
            sid = str(uuid4())
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'scopes')} "
                    f"(id,world_id,kind,name,created_at,creator_credential_id,run_id,"
                    f"agent_id,invocation_id,parent_invocation_id,alias) "
                    f"VALUES (:p0,:p1,:p2,:p3,:p4,:p5,:p6,:p7,:p8,"
                    f":p9,:p10)"
                ),
                {
                    "p0": sid,
                    "p1": world_id,
                    "p2": request.kind.value,
                    "p3": request.name,
                    "p4": datetime.now(UTC).isoformat(),
                    "p5": principal.credential_id,
                    "p6": str(run_id) if run_id else None,
                    "p7": str(agent_id) if agent_id else None,
                    "p8": str(invocation_id) if invocation_id else None,
                    "p9": str(principal.invocation_id) if principal.invocation_id else None,
                    "p10": request.alias,
                },
            )
            for op in ALL_OPERATIONS:
                db.execute(
                    text(f"INSERT INTO {self._table(db, 'scope_grants')} VALUES (:p0,:p1,:p2)"),
                    {"p0": principal.credential_id, "p1": sid, "p2": op},
                )
            self._initialize_world(world_id, sid)
            return self._hydrate_scope(self._scope_row(db, world_id, sid))

    def get_scope(self, principal: Principal, world_id: str, scope_id: str | UUID) -> Scope:
        with self._control() as db:
            row = self._scope_authorized(db, principal, world_id, scope_id)
        return self._hydrate_scope(row)

    def set_scope_alias(self, principal, world_id: str, scope_id, request: UpdateAlias) -> Scope:
        with self._control() as db, self._transaction(db):
            row = self._scope_authorized(db, principal, world_id, scope_id, "manage")
            db.execute(
                text(f"UPDATE {self._table(db, 'scopes')} SET alias=:p0 WHERE id=:p1"),
                {"p0": request.alias, "p1": row["id"]},
            )
            row = self._scope_row(db, world_id, scope_id)
        return self._hydrate_scope(row)

    def scope_flow(self, principal: Principal, request: ScopeFlow) -> bool:
        """Check current reader inclusion without exposing either audience.

        This guard supports automatic capture. Credentials still authorize every
        operation, and explicit publication remains a separate action. Membership
        can change later, so it is not a promise about future sharing decisions.
        """
        with self._control() as db, self._transaction(db):
            self._principal(db, principal)
            sources = []
            for selected in request.read_scopes:
                source = self._scope_authorized(db, principal, selected.world_id, selected.scope_id)
                sources.append((source["world_id"], source["id"]))
            target = self._scope_authorized(
                db, principal, request.write_scope.world_id, request.write_scope.scope_id, "write"
            )
            if not sources:
                return True
            # Private capture normally checks one invocation and its optional
            # coordinator. Bound large shared audiences conservatively rather
            # than turn every capture into an unbounded control-database scan.
            maximum = 4096
            if target["kind"] == ScopeKind.WORLD_SHARED.value:
                candidates = (
                    db.execute(
                        text(
                            f"SELECT * FROM "
                            f"{self._table(db, 'credentials')} WHERE "
                            f"revoked=0 LIMIT :p0"
                        ),
                        {"p0": maximum + 1},
                    )
                    .mappings()
                    .fetchall()
                )
            else:
                candidates = (
                    db.execute(
                        text(
                            f"SELECT c.* FROM "
                            f"{self._table(db, 'credentials')} c JOIN "
                            f"{self._table(db, 'scope_grants')} g ON "
                            f"g.credential_id=c.id WHERE c.revoked=0 AND "
                            f"g.scope_id=:p0 AND g.operation='read' LIMIT "
                            f":p1"
                        ),
                        {"p0": target["id"], "p1": maximum + 1},
                    )
                    .mappings()
                    .fetchall()
                )
            if len(candidates) > maximum:
                return False
            for credential in candidates:
                try:
                    reader = self._credential_principal(db, credential)
                    self._principal(db, reader)
                    self._scope_authorized(db, reader, target["world_id"], target["id"])
                except (Unauthorized, Forbidden, NotFound):
                    continue
                except (ValueError, TypeError):
                    return False
                for world_id, scope_id in dict.fromkeys(sources):
                    try:
                        self._scope_authorized(db, reader, world_id, scope_id)
                    except (Unauthorized, Forbidden, NotFound, ValueError, TypeError):
                        return False
            return True

    def list_scopes(self, principal: Principal, world_id: str) -> list[Scope]:
        with self._control() as db:
            self._authorized(db, principal, world_id)
            visible = []
            for row in db.execute(
                text(
                    f"SELECT * FROM {self._table(db, 'scopes')} "
                    f"WHERE world_id=:p0 ORDER BY created_at,id"
                ),
                {"p0": world_id},
            ).mappings():
                try:
                    self._scope_authorized(db, principal, world_id, row["id"])
                    visible.append(row)
                except Forbidden:
                    continue
        return [self._hydrate_scope(row) for row in visible]

    def set_scope_grant(self, principal, world_id, scope_id, request: UpdateScopeGrant):
        with self._control() as db, self._transaction(db):
            self._principal(db, principal)
            if principal.restricted:
                raise Forbidden(
                    "Delegated invocations grant access only through attenuated delegation"
                )
            scope = self._scope_authorized(db, principal, world_id, scope_id, "manage")
            target = (
                db.execute(
                    text(f"SELECT * FROM {self._table(db, 'credentials')} WHERE id=:p0"),
                    {"p0": request.credential_id},
                )
                .mappings()
                .fetchone()
            )
            if target is None:
                raise NotFound("Credential not found")
            target_principal = self._credential_principal(db, target)
            self._authorized(db, target_principal, world_id)
            if target_principal.restricted:
                raise Forbidden(
                    "Delegated scope grants are immutable; issue a new child credential"
                )
            for op in request.operations:
                self._scope_authorized(db, principal, world_id, scope_id, op.value)
            db.execute(
                text(
                    f"DELETE FROM {self._table(db, 'scope_grants')}"
                    f" WHERE credential_id=:p0 AND scope_id=:p1"
                ),
                {"p0": request.credential_id, "p1": scope["id"]},
            )
            db.execute(
                text(
                    f"DELETE FROM "
                    f"{self._table(db, 'scope_grant_anchors')} "
                    f"WHERE credential_id=:p0 AND scope_id=:p1"
                ),
                {"p0": request.credential_id, "p1": scope["id"]},
            )
            for op in set(request.operations):
                db.execute(
                    text(f"INSERT INTO {self._table(db, 'scope_grants')} VALUES (:p0,:p1,:p2)"),
                    {"p0": request.credential_id, "p1": scope["id"], "p2": op.value},
                )

    def bind_context(self, principal: Principal, request: BindContext) -> AgentContext:
        with self._control() as db, self._transaction(db):
            self._principal(db, principal)
            for selected in request.read_scopes:
                self._scope_authorized(db, principal, selected.world_id, selected.scope_id)
            if request.write_scope:
                self._scope_authorized(
                    db,
                    principal,
                    request.write_scope.world_id,
                    request.write_scope.scope_id,
                    "write",
                )
            self._validate_run(db, principal, request.run_id)
            if principal.restricted and request.agent_id not in {None, principal.agent_uuid}:
                raise Forbidden("A delegated context cannot change its agent identity")
            context = AgentContext(
                **request.model_dump(exclude={"agent_id"}),
                agent_id=principal.agent_uuid or request.agent_id or uuid4(),
                id=uuid4(),
                credential_id=principal.credential_id,
                invocation_id=principal.invocation_id or uuid4(),
                parent_invocation_id=principal.parent_invocation_id,
                created_at=datetime.now(UTC),
            )
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'agent_contexts')} VALUES (:p0,:p1,:p2,:p3,:p4)"
                ),
                {
                    "p0": str(context.id),
                    "p1": principal.credential_id,
                    "p2": str(context.invocation_id),
                    "p3": context.model_dump_json(),
                    "p4": context.created_at.isoformat(),
                },
            )
            return context

    def _validate_run(self, db, principal, run_id):
        if run_id is None:
            return
        if not principal.restricted:
            from .identity_names import validate_run

            if validate_run(self, db, principal, run_id):
                return
        rows = (
            db.execute(
                text(
                    f"SELECT * FROM {self._table(db, 'scopes')} "
                    f"WHERE kind='run-shared' AND run_id=:p0"
                ),
                {"p0": str(run_id)},
            )
            .mappings()
            .fetchall()
        )
        for row in rows:
            try:
                self._scope_authorized(db, principal, row["world_id"], row["id"])
                return
            except Forbidden:
                continue
        raise Forbidden("Run identity must name an authorized server-issued run")

    def get_context(self, principal: Principal, context_id: str | UUID) -> AgentContext:
        with self._control() as db:
            self._principal(db, principal)
            row = (
                db.execute(
                    text(
                        f"SELECT * FROM "
                        f"{self._table(db, 'agent_contexts')} WHERE "
                        f"id=:p0 AND credential_id=:p1"
                    ),
                    {"p0": scope_uuid(context_id), "p1": principal.credential_id},
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise NotFound("Context not found")
            context = AgentContext.model_validate_json(row["payload"])
            for selected in context.read_scopes:
                self._scope_authorized(db, principal, selected.world_id, selected.scope_id)
            if context.write_scope:
                self._scope_authorized(
                    db,
                    principal,
                    context.write_scope.world_id,
                    context.write_scope.scope_id,
                    "write",
                )
            return context

    def _capture_principal(self, principal, world_id, scope_id, context_id):
        if context_id is None:
            return principal
        context = self.get_context(principal, context_id)
        if context.write_scope is None:
            raise Forbidden("This context has no write destination")
        with self._control() as db:
            target = self._scope_row(db, world_id, scope_id)
            selected = self._scope_row(
                db, context.write_scope.world_id, context.write_scope.scope_id
            )
        if context.write_scope.world_id != world_id or selected["id"] != target["id"]:
            raise Forbidden("Capture must match the context's write destination")
        if principal.restricted and context.invocation_id != principal.invocation_id:
            raise Forbidden("Context invocation does not match its credential")
        return principal.model_copy(
            update={
                "agent_uuid": context.agent_id,
                "invocation_id": context.invocation_id,
                "parent_invocation_id": context.parent_invocation_id,
            }
        )

    def delegate_credential(self, principal: Principal, request: DelegateCredential) -> dict:
        if not request.grants and request.private_world_id is None:
            raise InvalidRequest("A child needs a selected scope or private world")
        if request.coordinator_read and request.private_world_id is None:
            raise InvalidRequest("Coordinator inspection requires a new private scope")
        now = datetime.now(UTC)
        expires = request.expires_at or now + timedelta(hours=1)
        if expires <= now or expires > now + timedelta(days=7):
            raise InvalidRequest("Child expiry must be in the next seven days")
        with self._control() as db, self._transaction(db):
            self._principal(db, principal)
            self._validate_run(db, principal, request.run_id)
            parent_invocation = principal.invocation_id
            if request.parent_context_id is not None:
                context_row = (
                    db.execute(
                        text(
                            f"SELECT payload FROM "
                            f"{self._table(db, 'agent_contexts')} WHERE "
                            f"id=:p0 AND credential_id=:p1"
                        ),
                        {"p0": str(request.parent_context_id), "p1": principal.credential_id},
                    )
                    .mappings()
                    .fetchone()
                )
                if context_row is None:
                    raise NotFound("Parent context not found")
                parent_invocation = AgentContext.model_validate_json(
                    context_row["payload"]
                ).invocation_id
            parent_context = (
                db.execute(
                    text(
                        f"SELECT * FROM "
                        f"{self._table(db, 'credential_contexts')} "
                        f"WHERE credential_id=:p0"
                    ),
                    {"p0": principal.credential_id},
                )
                .mappings()
                .fetchone()
            )
            if parent_context is not None:
                if not parent_context["allow_delegation"]:
                    raise Forbidden("This invocation cannot delegate")
                if (
                    request.expires_at is None
                    and expires.timestamp() > parent_context["expires_at"]
                ):
                    expires = min(expires, _expiry_ceiling(parent_context["expires_at"]))
                if expires.timestamp() > parent_context["expires_at"]:
                    raise Forbidden("A child cannot outlive its parent")
            identity_session = (
                db.execute(
                    text(
                        f"SELECT expires_at FROM "
                        f"{self._table(db, 'identity_sessions')} WHERE "
                        f"credential_id=:p0"
                    ),
                    {"p0": principal.credential_id},
                )
                .mappings()
                .fetchone()
            )
            if (
                identity_session is not None
                and request.expires_at is None
                and expires.timestamp() > identity_session["expires_at"]
            ):
                expires = min(expires, _expiry_ceiling(identity_session["expires_at"]))
            if (
                identity_session is not None
                and expires.timestamp() > identity_session["expires_at"]
            ):
                raise Forbidden("A child cannot outlive its identity session")
            selected = {}
            world_ids = set()
            for grant in request.grants:
                row = self._scope_authorized(
                    db, principal, grant.world_id, grant.scope_id, "delegate"
                )
                for operation in grant.operations:
                    self._scope_authorized(
                        db, principal, grant.world_id, grant.scope_id, operation.value
                    )
                if "delegate" in grant.operations and not request.allow_delegation:
                    raise InvalidRequest("Delegate operations require allow_delegation")
                selected.setdefault((grant.world_id, row["id"]), set()).update(
                    op.value for op in grant.operations
                )
                world_ids.add(grant.world_id)
            if request.private_world_id:
                self._authorized(db, principal, request.private_world_id, write=True)
                if principal.restricted:
                    candidates = (
                        db.execute(
                            text(
                                f"SELECT s.id FROM "
                                f"{self._table(db, 'scope_grants')} g JOIN "
                                f"{self._table(db, 'scopes')} s ON "
                                f"s.id=g.scope_id WHERE g.credential_id=:p0 "
                                f"AND g.operation='delegate' AND s.world_id=:p1"
                            ),
                            {"p0": principal.credential_id, "p1": request.private_world_id},
                        )
                        .mappings()
                        .fetchall()
                    )
                    permitted = False
                    for candidate in candidates:
                        try:
                            self._scope_authorized(
                                db, principal, request.private_world_id, candidate["id"], "delegate"
                            )
                            permitted = True
                            break
                        except Forbidden:
                            continue
                    if not permitted:
                        raise Forbidden("Creating a child scope requires delegation in that world")
                world_ids.add(request.private_world_id)
            for world_id in world_ids:
                world = self._authorized(db, principal, world_id)
                if (
                    world["kind"] in {WorldKind.PERSONAL.value, WorldKind.PRIVATE_WORK.value}
                    and world["owner_id"] != principal.user_id
                ):
                    raise Forbidden("Delegated private world grants must belong to the same user")
            credential_id = uuid4().hex
            agent_uuid, invocation_id = request.agent_id or uuid4(), uuid4()
            token = f"mw_{credential_id}.{secrets.token_urlsafe(32)}"
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'credentials')} "
                    f"(id,token_hash,user_id,agent_id,admin) "
                    f"VALUES (:p0,:p1,:p2,:p3,0)"
                ),
                {
                    "p0": credential_id,
                    "p1": hashlib.sha256(token.encode()).hexdigest(),
                    "p2": principal.user_id,
                    "p3": str(agent_uuid),
                },
            )
            db.execute(
                text(
                    f"INSERT INTO "
                    f"{self._table(db, 'credential_contexts')} "
                    f"VALUES (:p0,:p1,:p2,:p3,:p4,:p5,:p6)"
                ),
                {
                    "p0": credential_id,
                    "p1": principal.credential_id,
                    "p2": expires.timestamp(),
                    "p3": str(agent_uuid),
                    "p4": str(invocation_id),
                    "p5": str(parent_invocation) if parent_invocation else None,
                    "p6": int(request.allow_delegation),
                },
            )
            self._validate_ancestors(db, credential_id)
            for wid in world_ids:
                writes = wid == request.private_world_id or any(
                    world == wid and ops.intersection(WRITE_OPERATIONS)
                    for (world, _sid), ops in selected.items()
                )
                db.execute(
                    text(f"INSERT INTO {self._table(db, 'grants')} VALUES (:p0,:p1,:p2)"),
                    {
                        "p0": credential_id,
                        "p1": wid,
                        "p2": Role.WRITER.value if writes else Role.READER.value,
                    },
                )
            for (_wid, sid), operations in selected.items():
                for operation in operations:
                    db.execute(
                        text(f"INSERT INTO {self._table(db, 'scope_grants')} VALUES (:p0,:p1,:p2)"),
                        {"p0": credential_id, "p1": sid, "p2": operation},
                    )
            private_scope = None
            if request.private_world_id:
                sid = str(uuid4())
                db.execute(
                    text(
                        f"INSERT INTO {self._table(db, 'scopes')} "
                        f"(id,world_id,kind,name,created_at,creator_credential_id,run_id,"
                        f"agent_id,invocation_id,parent_invocation_id,alias) "
                        f"VALUES (:p0,:p1,:p2,:p3,:p4,:p5,:p6,:p7,:p8,"
                        f":p9,:p10)"
                    ),
                    {
                        "p0": sid,
                        "p1": request.private_world_id,
                        "p2": ScopeKind.INVOCATION_PRIVATE.value,
                        "p3": "Invocation private",
                        "p4": now.isoformat(),
                        "p5": credential_id,
                        "p6": str(request.run_id) if request.run_id else None,
                        "p7": str(agent_uuid),
                        "p8": str(invocation_id),
                        "p9": str(parent_invocation) if parent_invocation else None,
                        "p10": None,
                    },
                )
                operations = {"read", "write"}
                if request.allow_delegation:
                    operations.add("delegate")
                for operation in operations:
                    db.execute(
                        text(f"INSERT INTO {self._table(db, 'scope_grants')} VALUES (:p0,:p1,:p2)"),
                        {"p0": credential_id, "p1": sid, "p2": operation},
                    )
                if request.coordinator_read:
                    db.execute(
                        text(
                            f"INSERT INTO {self._table(db, 'scope_grants')} VALUES (:p0,:p1,'read')"
                        ),
                        {"p0": principal.credential_id, "p1": sid},
                    )
                    db.execute(
                        text(
                            f"INSERT INTO "
                            f"{self._table(db, 'scope_grant_anchors')} "
                            f"VALUES (:p0,:p1,'read')"
                        ),
                        {"p0": principal.credential_id, "p1": sid},
                    )
                self._initialize_world(request.private_world_id, sid)
                private_scope = self._hydrate_scope(
                    self._scope_row(db, request.private_world_id, sid), control=db
                )
            reads = [
                ScopeSelection(world_id=wid, scope_id=sid)
                for (wid, sid), ops in selected.items()
                if "read" in ops
            ]
            write_scope = None
            if private_scope:
                write_scope = ScopeSelection(
                    world_id=private_scope.world_id, scope_id=private_scope.id
                )
                reads.append(write_scope)
            elif writable := [(wid, sid) for (wid, sid), ops in selected.items() if "write" in ops]:
                write_scope = ScopeSelection(world_id=writable[0][0], scope_id=writable[0][1])
            context = AgentContext(
                read_scopes=reads,
                write_scope=write_scope,
                agent_id=agent_uuid,
                run_id=request.run_id,
                id=uuid4(),
                credential_id=credential_id,
                invocation_id=invocation_id,
                parent_invocation_id=parent_invocation,
                created_at=now,
            )
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'agent_contexts')} VALUES (:p0,:p1,:p2,:p3,:p4)"
                ),
                {
                    "p0": str(context.id),
                    "p1": credential_id,
                    "p2": str(invocation_id),
                    "p3": context.model_dump_json(),
                    "p4": now.isoformat(),
                },
            )
            return {
                "token": token,
                "credential_id": credential_id,
                "context": context,
                "private_scope": private_scope,
                "expires_at": expires,
            }

    def publish_scope(self, principal, world_id, scope_id, request: ScopedPublication):
        from .models import EpisodeInput

        with self._control() as db:
            destination = self._scope_authorized(db, principal, world_id, scope_id, "publish")
        metadata = {}
        if request.source_scope is not None or request.source_episode_id is not None:
            if request.source_scope is None or request.source_episode_id is None:
                raise InvalidRequest("A publication source requires a scope and episode")
            source = self.get_episode(
                principal,
                request.source_scope.world_id,
                request.source_episode_id,
                scope_id=request.source_scope.scope_id,
            )
            metadata["publication_source"] = {
                **request.source_scope.model_dump(mode="json"),
                "episode_id": source.id,
                "digest": source.digest,
            }
        episode = EpisodeInput(
            source_id=request.source_id,
            content=request.content,
            occurred_at=request.occurred_at,
            source_type="publication",
            project=request.project,
            scope_id=destination["id"],
            metadata=metadata,
            context_id=request.context_id,
        )
        attributed = self._capture_principal(
            principal, world_id, destination["id"], request.context_id
        )
        with (
            self._world_db(world_id, destination["id"]) as db,
            self._transaction(db),
        ):
            receipt = self._insert(db, attributed, world_id, episode)
        return receipt
