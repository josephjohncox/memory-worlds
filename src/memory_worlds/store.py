"""Durable source storage and live authorization for one memory authority.

Only the daemon opens these files. Projection methods without a principal are
internal worker operations, never agent-facing API operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, RowMapping

from memory_worlds.errors import Conflict, Forbidden, NotFound, Unauthorized, Unavailable
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.models import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_EPISODES,
    CreateToken,
    CreateWorld,
    Episode,
    EpisodeInput,
    Principal,
    Receipt,
    Role,
    ScopeKind,
    UpdateAlias,
    World,
    WorldKind,
)
from memory_worlds.scopes import ScopedStore, scope_uuid, world_shared_scope_id

from . import (
    identity_names,  # noqa: F401 -- registers the current authority identity table
    lifecycle,
)
from .persistence import LeaseLost, Persistence, WorkerLease

_PRIVATE = {WorldKind.PERSONAL, WorldKind.PRIVATE_WORK}
_WRITERS = {Role.WRITER.value, Role.MANAGER.value}
_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: dict) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidRequest("Metadata must contain JSON values with finite numbers") from error


class MemoryStore(ScopedStore):
    def __init__(
        self, data_dir: Path, database_url: str | None = None, authority_id: str | None = None
    ):
        self.persistence = Persistence(data_dir, database_url, authority_id)
        self.data_dir = self.persistence.data_dir
        self.control_path = self.data_dir / "control.sqlite3"
        self._locks_lock = threading.Lock()
        self._world_locks: dict[str, threading.RLock] = {}
        self.persistence.initialize()
        self.authority_id = self.persistence.authority_id

    @staticmethod
    def database_url_from_env() -> str | None:
        """Validate the deployment selector without opening a recovery destination."""
        backend = os.environ.get("MEMORY_WORLDS_STORAGE_BACKEND")
        url = os.environ.get("MEMORY_WORLDS_DATABASE_URL", "").strip() or None
        if backend not in {None, "sqlite", "postgres"}:
            raise InvalidRequest("MEMORY_WORLDS_STORAGE_BACKEND must be sqlite or postgres")
        if backend == "postgres" and url is None:
            raise InvalidRequest("PostgreSQL storage requires MEMORY_WORLDS_DATABASE_URL")
        if backend == "sqlite" and url is not None:
            raise InvalidRequest("SQLite storage cannot configure MEMORY_WORLDS_DATABASE_URL")
        return url

    @classmethod
    def from_env(cls, data_dir: Path):
        return cls(
            data_dir,
            database_url=cls.database_url_from_env(),
            authority_id=os.environ.get("MEMORY_WORLDS_AUTHORITY_ID") or None,
        )

    def _table(self, db: Connection, name: str):
        return self.persistence.table(db, name)

    @staticmethod
    def _directory(path: Path):
        Persistence.directory(path)

    @contextmanager
    def _control(self) -> Iterator[Connection]:
        with self.persistence.control() as db, self.persistence.transaction(db):
            yield db

    def _lock(self, world_id: str) -> threading.RLock:
        if not _ID.fullmatch(world_id):
            raise NotFound("World not found")
        with self._locks_lock:
            return self._world_locks.setdefault(world_id, threading.RLock())

    def world_path(self, world_id: str, scope_id=None) -> Path:
        """Native SQLite administration only; shared storage has no local journals."""
        self._lock(world_id)
        return self.persistence.source_path(world_id, scope_id)

    @contextmanager
    def _world_db(self, world_id: str, scope_id=None) -> Iterator[Connection]:
        self._lock(world_id)
        with self.persistence.source(world_id, scope_id) as db:
            yield db

    @contextmanager
    def _transaction(self, db: Connection):
        with self.persistence.transaction(db):
            yield

    def _initialize_world(self, world_id: str, scope_id=None):
        self._lock(world_id)
        self.persistence.initialize_source(world_id, scope_id)

    def _principal(self, db: Connection, principal: Principal) -> Principal:
        self._validate_ancestors(db, principal.credential_id)
        row = (
            db.execute(
                text(f"SELECT * FROM {self._table(db, 'credentials')} WHERE id=:p0 AND revoked=0"),
                {"p0": principal.credential_id},
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            raise Unauthorized("Credential is invalid or revoked")
        session = (
            db.execute(
                text(
                    f"SELECT s.expires_at,i.enabled FROM "
                    f"{self._table(db, 'identity_sessions')} s "
                    f"JOIN {self._table(db, 'identities')} i ON "
                    f"i.user_id=s.user_id WHERE s.credential_id=:p0"
                ),
                {"p0": principal.credential_id},
            )
            .mappings()
            .fetchone()
        )
        identity = (
            db.execute(
                text(f"SELECT enabled FROM {self._table(db, 'identities')} WHERE user_id=:p0"),
                {"p0": row["user_id"]},
            )
            .mappings()
            .fetchone()
        )
        if identity is not None and not identity["enabled"]:
            raise Unauthorized("Identity is disabled")
        if session is not None and session["expires_at"] <= time.time():
            raise Unauthorized("Identity session is expired")
        current = self._credential_principal(db, row)
        if current != principal:
            raise Unauthorized("Credential identity does not match")
        return current

    def validate_principal(self, principal: Principal) -> Principal:
        """Recheck a previously authenticated caller, including revocation and expiry."""
        with self._control() as db:
            return self._principal(db, principal)

    def reconcile_oidc_issuer(self, active_issuer: str | None) -> None:
        """Invalidate stored external sessions when their issuer is no longer active.

        This is operator startup configuration, not an agent-facing operation.
        Identity records and manual memberships survive for later verified logins.
        """
        with self._control() as db, self._transaction(db):
            db.execute(
                text(
                    f"UPDATE {self._table(db, 'credentials')} SET "
                    f"revoked=1 WHERE id IN (SELECT "
                    f"s.credential_id FROM "
                    f"{self._table(db, 'identity_sessions')} s "
                    f"JOIN {self._table(db, 'identities')} i ON "
                    f"i.user_id=s.user_id WHERE CAST(:p0 AS TEXT) "
                    f"IS NULL OR i.issuer<>:p1)"
                ),
                {"p0": active_issuer, "p1": active_issuer},
            )

    def reconcile_oidc_policy(self, issuer: str, policy_digest: str) -> None:
        """Invalidate sessions and descendants when the trusted token policy changes."""
        key = "oidc-policy:" + issuer
        with self._control() as db, self._transaction(db):
            previous = (
                db.execute(
                    text(f"SELECT value FROM {self._table(db, 'metadata')} WHERE key=:p0"),
                    {"p0": key},
                )
                .mappings()
                .fetchone()
            )
            if previous is None or previous["value"] != policy_digest:
                db.execute(
                    text(
                        f"UPDATE {self._table(db, 'credentials')} SET "
                        f"revoked=1 WHERE id IN (SELECT "
                        f"s.credential_id FROM "
                        f"{self._table(db, 'identity_sessions')} s "
                        f"JOIN {self._table(db, 'identities')} i ON "
                        f"i.user_id=s.user_id WHERE i.issuer=:p0)"
                    ),
                    {"p0": issuer},
                )
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'metadata')} "
                    f"VALUES (:p0,:p1) ON CONFLICT(key) DO UPDATE "
                    f"SET value=excluded.value"
                ),
                {"p0": key, "p1": policy_digest},
            )

    def reconcile_oidc_mappings(
        self, issuer: str, group_mappings: dict[str, dict[str, Role]]
    ) -> None:
        """Drop stale mapped grants before pending work can reuse an old principal.

        A changed operator map supplies no fresh group evidence, so remove grants
        rather than guessing which groups the identity currently has. The next
        verified login recomputes them without resetting the signed-claim watermark.
        """
        digest = hashlib.sha256(_canonical(group_mappings).encode()).hexdigest()
        with self._control() as db, self._transaction(db):
            for mappings in group_mappings.values():
                for world_id in mappings:
                    world = (
                        db.execute(
                            text(f"SELECT kind FROM {self._table(db, 'worlds')} WHERE id=:p0"),
                            {"p0": world_id},
                        )
                        .mappings()
                        .fetchone()
                    )
                    if world is None or WorldKind(world["kind"]) in _PRIVATE:
                        raise ValueError(
                            "OIDC group mappings must target existing shared world IDs"
                        )
            db.execute(
                text(
                    f"DELETE FROM {self._table(db, 'memberships')} "
                    f"WHERE source=:p0 AND user_id IN (SELECT "
                    f"i.user_id FROM "
                    f"{self._table(db, 'identities')} i LEFT JOIN "
                    f"{self._table(db, 'identity_group_state')} g "
                    f"ON g.user_id=i.user_id WHERE i.issuer=:p1 "
                    f"AND (g.mappings_digest IS NULL OR "
                    f"g.mappings_digest<>:p2))"
                ),
                {"p0": "oidc:" + issuer, "p1": issuer, "p2": digest},
            )

    def _admin(self, db: Connection, principal: Principal) -> None:
        if not self._principal(db, principal).admin:
            raise Forbidden("Administrative credential required")

    def _token(self, db: Connection, request: CreateToken) -> str:
        credential_id = uuid4().hex
        token = f"mw_{credential_id}.{secrets.token_urlsafe(32)}"
        db.execute(
            text(
                f"INSERT INTO {self._table(db, 'credentials')} "
                f"(id,token_hash,user_id,agent_id,admin) "
                f"VALUES (:p0,:p1,:p2,:p3,:p4)"
            ),
            {
                "p0": credential_id,
                "p1": hashlib.sha256(token.encode()).hexdigest(),
                "p2": request.user_id,
                "p3": request.agent_id,
                "p4": int(request.admin),
            },
        )
        for world_id, role in request.grants.items():
            db.execute(
                text(f"INSERT INTO {self._table(db, 'grants')} VALUES (:p0,:p1,:p2)"),
                {"p0": credential_id, "p1": world_id, "p2": role.value},
            )
        return token

    def _new_world(self, db: Connection, request: CreateWorld, owner_id: str) -> World:
        world_id = uuid4().hex
        world = World(
            id=world_id,
            authority_id=self.authority_id,
            name=request.name,
            alias=request.alias,
            kind=request.kind,
            owner_id=owner_id,
            created_at=datetime.now(UTC),
            scope_id=UUID(world_shared_scope_id(self.authority_id, world_id)),
        )
        self._initialize_world(world.id)
        db.execute(
            text(f"INSERT INTO {self._table(db, 'worlds')} VALUES (:p0,:p1,:p2,:p3,:p4,:p5,:p6)"),
            {
                "p0": world.id,
                "p1": world.authority_id,
                "p2": world.name,
                "p3": world.kind.value,
                "p4": world.owner_id,
                "p5": world.created_at.isoformat(),
                "p6": world.alias,
            },
        )
        self._create_shared_scope(db, world)
        return world

    def bootstrap(self, user_id: str = "owner") -> tuple[World, str]:
        request = CreateToken(user_id=user_id, agent_id="admin", admin=True)
        with self._control() as db, self._transaction(db):
            if (
                db.execute(
                    text(f"SELECT 1 FROM {self._table(db, 'metadata')} WHERE key='bootstrapped'")
                )
                .mappings()
                .fetchone()
            ):
                raise Conflict("This authority is already initialized")
            world = self._new_world(db, CreateWorld(name="Personal"), request.user_id)
            token = self._token(db, request)
            db.execute(
                text(f"INSERT INTO {self._table(db, 'metadata')} VALUES ('bootstrapped', '1')")
            )
            return world, token

    def authenticate(self, token: str) -> Principal:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._control() as db:
            row = (
                db.execute(
                    text(
                        f"SELECT * FROM "
                        f"{self._table(db, 'credentials')} WHERE "
                        f"token_hash=:p0 AND revoked=0"
                    ),
                    {"p0": digest},
                )
                .mappings()
                .fetchone()
            )
            if row is None:
                raise Unauthorized("Credential is invalid or revoked")
            return self._principal(db, self._credential_principal(db, row))

    def authenticate_oidc(
        self,
        issuer: str,
        subject: str,
        token_digest: str,
        expires_at: float,
        groups: list[str] | None = None,
        group_mappings: dict[str, dict[str, Role]] | None = None,
        issued_at: float | None = None,
    ) -> Principal:
        """Accept only claims verified by OIDCAuthenticator, never API request data.

        Identity sessions deliberately have no local bearer hash. Calling authenticate()
        cannot turn an expired external token into a permanent local credential.
        """
        if not issuer or not subject or expires_at <= time.time():
            raise Unauthorized("Invalid identity session")
        with self._control() as db, self._transaction(db):
            identity = (
                db.execute(
                    text(
                        f"SELECT * FROM {self._table(db, 'identities')}"
                        f" WHERE issuer=:p0 AND subject=:p1"
                    ),
                    {"p0": issuer, "p1": subject},
                )
                .mappings()
                .fetchone()
            )
            if identity is None:
                user_id = "user_" + uuid4().hex
                world = self._new_world(
                    db, CreateWorld(name="Private work", kind=WorldKind.PRIVATE_WORK), user_id
                )
                db.execute(
                    text(
                        f"INSERT INTO {self._table(db, 'identities')} "
                        f"(issuer,subject,user_id,private_world_id) "
                        f"VALUES (:p0,:p1,:p2,:p3)"
                    ),
                    {"p0": issuer, "p1": subject, "p2": user_id, "p3": world.id},
                )
                db.execute(
                    text(
                        f"INSERT INTO {self._table(db, 'memberships')} "
                        f"VALUES (:p0,:p1,:p2,'manual')"
                    ),
                    {"p0": user_id, "p1": world.id, "p2": Role.MANAGER.value},
                )
            else:
                if not identity["enabled"]:
                    raise Unauthorized("Identity is disabled")
                user_id = identity["user_id"]
            session = (
                db.execute(
                    text(
                        f"SELECT c.* FROM "
                        f"{self._table(db, 'credentials')} c JOIN "
                        f"{self._table(db, 'identity_sessions')} s ON "
                        f"s.credential_id=c.id WHERE s.token_digest=:p0"
                    ),
                    {"p0": token_digest},
                )
                .mappings()
                .fetchone()
            )
            if session is not None:
                if session["revoked"] or session["user_id"] != user_id:
                    raise Unauthorized("Identity session is revoked")
                principal = Principal(
                    credential_id=session["id"],
                    user_id=user_id,
                    agent_id=session["agent_id"],
                    admin=False,
                )
                self._principal(db, principal)
            if group_mappings:
                if issued_at is None:
                    raise Unauthorized("Mapped identity groups require a signed issued-at time")
                groups_digest = hashlib.sha256(
                    _canonical({"groups": sorted(set(groups or []))}).encode()
                ).hexdigest()
                mappings_digest = hashlib.sha256(_canonical(group_mappings).encode()).hexdigest()
                previous = (
                    db.execute(
                        text(
                            f"SELECT * FROM "
                            f"{self._table(db, 'identity_group_state')} "
                            f"WHERE user_id=:p0"
                        ),
                        {"p0": user_id},
                    )
                    .mappings()
                    .fetchone()
                )
                if previous is not None and (
                    issued_at < previous["claims_iat"]
                    or (
                        issued_at == previous["claims_iat"]
                        and groups_digest != previous["groups_digest"]
                    )
                ):
                    raise Unauthorized("Identity groups conflict with the latest accepted claims")
                # Configuration changes are operator authority. Re-evaluate the same
                # accepted claim set against a new map on its next authentication;
                # an older or conflicting claim set can never advance this watermark.
                db.execute(
                    text(
                        f"INSERT INTO "
                        f"{self._table(db, 'identity_group_state')} "
                        f"VALUES (:p0,:p1,:p2,:p3) ON "
                        f"CONFLICT(user_id) DO UPDATE SET "
                        f"claims_iat=excluded.claims_iat,"
                        f"groups_digest=excluded.groups_digest,"
                        f"mappings_digest=excluded.mappings_digest"
                    ),
                    {"p0": user_id, "p1": issued_at, "p2": groups_digest, "p3": mappings_digest},
                )
            else:
                # Disabling the map removes mapped memberships while retaining the
                # signed-claim watermark in case the operator enables mappings again.
                db.execute(
                    text(
                        f"UPDATE "
                        f"{self._table(db, 'identity_group_state')} "
                        f"SET mappings_digest=:p0 WHERE user_id=:p1"
                    ),
                    {"p0": hashlib.sha256(_canonical({}).encode()).hexdigest(), "p1": user_id},
                )
            # An operator controls the map. Token strings can never name arbitrary worlds.
            mapped: dict[str, Role] = {}
            rank = {Role.READER: 0, Role.WRITER: 1, Role.MANAGER: 2}
            for group in groups or []:
                for world_id, role in (group_mappings or {}).get(group, {}).items():
                    world = (
                        db.execute(
                            text(f"SELECT * FROM {self._table(db, 'worlds')} WHERE id=:p0"),
                            {"p0": world_id},
                        )
                        .mappings()
                        .fetchone()
                    )
                    if world is None or WorldKind(world["kind"]) in _PRIVATE:
                        raise Forbidden(
                            "Identity group mappings must target existing shared worlds"
                        )
                    if world_id not in mapped or rank[role] > rank[mapped[world_id]]:
                        mapped[world_id] = role
            db.execute(
                text(
                    f"DELETE FROM {self._table(db, 'memberships')} WHERE user_id=:p0 AND source=:p1"
                ),
                {"p0": user_id, "p1": "oidc:" + issuer},
            )
            for world_id, role in mapped.items():
                db.execute(
                    text(f"INSERT INTO {self._table(db, 'memberships')} VALUES (:p0,:p1,:p2,:p3)"),
                    {"p0": user_id, "p1": world_id, "p2": role.value, "p3": "oidc:" + issuer},
                )
            if session is not None:
                return principal
            credential_id = uuid4().hex
            agent_id = "oidc"
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'credentials')} "
                    f"(id,token_hash,user_id,agent_id,admin) "
                    f"VALUES (:p0,:p1,:p2,:p3,0)"
                ),
                {"p0": credential_id, "p1": "oidc:" + token_digest, "p2": user_id, "p3": agent_id},
            )
            db.execute(
                text(
                    f"INSERT INTO {self._table(db, 'identity_sessions')} VALUES (:p0,:p1,:p2,:p3)"
                ),
                {"p0": credential_id, "p1": user_id, "p2": token_digest, "p3": expires_at},
            )
            return Principal(credential_id=credential_id, user_id=user_id, agent_id=agent_id)

    def list_users(self, principal: Principal) -> list[dict]:
        with self._control() as db:
            self._admin(db, principal)
            return [
                dict(row)
                for row in db.execute(
                    text(
                        f"SELECT user_id,issuer,subject,enabled,"
                        f"private_world_id FROM "
                        f"{self._table(db, 'identities')} ORDER BY "
                        f"user_id"
                    )
                )
                .mappings()
                .fetchall()
            ]

    def set_user_enabled(self, principal: Principal, user_id: str, enabled: bool) -> None:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            changed = db.execute(
                text(f"UPDATE {self._table(db, 'identities')} SET enabled=:p0 WHERE user_id=:p1"),
                {"p0": int(enabled), "p1": user_id},
            ).rowcount
            if not changed:
                raise NotFound("Identity not found")
            if not enabled:
                db.execute(
                    text(
                        f"UPDATE {self._table(db, 'credentials')} SET revoked=1 WHERE user_id=:p0"
                    ),
                    {"p0": user_id},
                )

    def list_memberships(self, principal: Principal, world_id: str) -> list[dict]:
        with self._control() as db:
            self._admin(db, principal)
            self._authorized(db, principal, world_id)
            return [
                dict(row)
                for row in db.execute(
                    text(
                        f"SELECT user_id,world_id,role,source FROM "
                        f"{self._table(db, 'memberships')} WHERE "
                        f"world_id=:p0 ORDER BY user_id,source"
                    ),
                    {"p0": world_id},
                )
                .mappings()
                .fetchall()
            ]

    def set_membership(
        self, principal: Principal, world_id: str, user_id: str, role: Role | None
    ) -> None:
        if not _ID.fullmatch(user_id):
            raise InvalidRequest("Invalid user identifier")
        if role is not None:
            role = Role(role)
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            world = self._authorized(db, principal, world_id)
            if WorldKind(world["kind"]) in _PRIVATE and world["owner_id"] != user_id:
                raise Forbidden("Private world memberships must belong to the world owner")
            if role is None:
                db.execute(
                    text(
                        f"DELETE FROM {self._table(db, 'memberships')} "
                        f"WHERE user_id=:p0 AND world_id=:p1 AND "
                        f"source='manual'"
                    ),
                    {"p0": user_id, "p1": world_id},
                )
            else:
                db.execute(
                    text(
                        f"INSERT INTO {self._table(db, 'memberships')} "
                        f"VALUES (:p0,:p1,:p2,'manual') ON "
                        f"CONFLICT(user_id,world_id,source) DO UPDATE "
                        f"SET role=excluded.role"
                    ),
                    {"p0": user_id, "p1": world_id, "p2": role.value},
                )

    def create_world(self, principal: Principal, request: CreateWorld) -> World:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            return self._new_world(db, request, request.owner_id or principal.user_id)

    def set_world_alias(self, principal: Principal, world_id: str, request: UpdateAlias) -> World:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            self._authorized(db, principal, world_id)
            db.execute(
                text(f"UPDATE {self._table(db, 'worlds')} SET alias=:p0 WHERE id=:p1"),
                {"p0": request.alias, "p1": world_id},
            )
            row = (
                db.execute(
                    text(f"SELECT * FROM {self._table(db, 'worlds')} WHERE id=:p0"),
                    {"p0": world_id},
                )
                .mappings()
                .fetchone()
            )
        return self._hydrate(row)

    def _authorized(
        self, db: Connection, principal: Principal, world_id: str, write: bool = False
    ) -> RowMapping:
        self._lock(world_id)
        self._principal(db, principal)
        row = (
            db.execute(
                text(f"SELECT * FROM {self._table(db, 'worlds')} WHERE id=:p0"), {"p0": world_id}
            )
            .mappings()
            .fetchone()
        )
        if row is None:
            raise NotFound("World not found")
        if principal.admin:
            return row
        if WorldKind(row["kind"]) in _PRIVATE and row["owner_id"] != principal.user_id:
            raise Forbidden("World access denied")
        identity_session = (
            db.execute(
                text(
                    f"SELECT 1 FROM {self._table(db, 'identity_sessions')} WHERE credential_id=:p0"
                ),
                {"p0": principal.credential_id},
            )
            .mappings()
            .fetchone()
        )
        if identity_session is not None:
            memberships = (
                db.execute(
                    text(
                        f"SELECT role FROM "
                        f"{self._table(db, 'memberships')} WHERE "
                        f"user_id=:p0 AND world_id=:p1"
                    ),
                    {"p0": principal.user_id, "p1": world_id},
                )
                .mappings()
                .fetchall()
            )
            roles = {membership["role"] for membership in memberships}
        else:
            # A separate agent credential is an explicit capability scope. The
            # owner's broader user memberships cannot elevate a reader or grant
            # access to worlds absent from this credential's grants.
            grant = (
                db.execute(
                    text(
                        f"SELECT role FROM {self._table(db, 'grants')} "
                        f"WHERE credential_id=:p0 AND world_id=:p1"
                    ),
                    {"p0": principal.credential_id, "p1": world_id},
                )
                .mappings()
                .fetchone()
            )
            roles = {grant["role"]} if grant is not None else set()
        if not roles or (write and not roles.intersection(_WRITERS)):
            raise Forbidden("World access denied")
        context = (
            db.execute(
                text(
                    f"SELECT parent_credential_id FROM "
                    f"{self._table(db, 'credential_contexts')} "
                    f"WHERE credential_id=:p0"
                ),
                {"p0": principal.credential_id},
            )
            .mappings()
            .fetchone()
        )
        if context is not None:
            parent = (
                db.execute(
                    text(f"SELECT * FROM {self._table(db, 'credentials')} WHERE id=:p0"),
                    {"p0": context["parent_credential_id"]},
                )
                .mappings()
                .fetchone()
            )
            self._authorized(db, self._credential_principal(db, parent), world_id, write)
        return row

    def _hydrate(self, row: RowMapping, scope_id=None) -> World:
        scope_id = UUID(scope_uuid(scope_id or world_shared_scope_id(self.authority_id, row["id"])))
        with self._world_db(row["id"], scope_id) as db:
            projection = (
                db.execute(text(f"SELECT generation,state FROM {self._table(db, 'projection')}"))
                .mappings()
                .fetchone()
            )
        return World(
            **dict(row),
            generation=projection["generation"],
            projection_state=projection["state"],
            scope_id=scope_id,
        )

    def get_world(
        self, principal: Principal, world_id: str, write: bool = False, scope_id=None
    ) -> World:
        with self._control() as db:
            row = self._authorized(db, principal, world_id, write)
            scope = self._scope_authorized(
                db, principal, world_id, scope_id, "write" if write else "read"
            )
        return self._hydrate(row, scope["id"])

    def list_worlds(self, principal: Principal) -> list[World]:
        with self._control() as db:
            self._principal(db, principal)
            rows = (
                db.execute(
                    text(f"SELECT * FROM {self._table(db, 'worlds')} ORDER BY created_at,id")
                )
                .mappings()
                .fetchall()
            )
            visible = []
            for row in rows:
                try:
                    self._authorized(db, principal, row["id"])
                    visible.append(row)
                except Forbidden:
                    continue
        # A scope-only invocation may discover its world's identity without
        # receiving the shared journal's projection state or any shared data.
        if principal.restricted:
            return [
                World(
                    **dict(row),
                    scope_id=UUID(world_shared_scope_id(self.authority_id, row["id"])),
                )
                for row in visible
            ]
        return [self._hydrate(row) for row in visible]

    def issue_token(self, principal: Principal, request: CreateToken) -> str:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            for world_id in request.grants:
                row = self._authorized(db, principal, world_id)
                if WorldKind(row["kind"]) in _PRIVATE and row["owner_id"] != request.user_id:
                    raise Forbidden("Private world grants must belong to the world owner")
            return self._token(db, request)

    def revoke_token(self, principal: Principal, credential_id: str) -> None:
        with self._control() as db, self._transaction(db):
            self._principal(db, principal)
            if not principal.admin:
                row = (
                    db.execute(
                        text(
                            f"SELECT parent_credential_id FROM "
                            f"{self._table(db, 'credential_contexts')} "
                            f"WHERE credential_id=:p0"
                        ),
                        {"p0": credential_id},
                    )
                    .mappings()
                    .fetchone()
                )
                if row is None or row["parent_credential_id"] != principal.credential_id:
                    raise Forbidden(
                        "Only administrators or the delegating parent may revoke a credential"
                    )
            changed = db.execute(
                text(f"UPDATE {self._table(db, 'credentials')} SET revoked=1 WHERE id=:p0"),
                {"p0": credential_id},
            ).rowcount
            if not changed:
                raise NotFound("Credential not found")

    @staticmethod
    def _episode(row: RowMapping, world_id: str) -> Episode:
        return Episode(
            **json.loads(row["payload"]),
            id=row["id"],
            world_id=world_id,
            sequence=row["sequence"],
            digest=row["digest"],
            received_at=row["received_at"],
            actor_id=row["actor_id"],
            agent_id=row["agent_id"],
            agent_uuid=row["agent_uuid"],
            invocation_id=row["invocation_id"],
            parent_invocation_id=row["parent_invocation_id"],
            projection_state=row["projection_state"],
        )

    def _insert(
        self, db: Connection, principal: Principal, world_id: str, episode: EpisodeInput
    ) -> Receipt:
        # Validate before Pydantic's JSON conversion can turn NaN into null and
        # make two distinct inputs acquire the same source identity digest.
        lifecycle.assert_source_live(self, db, episode.source_id)
        _canonical(episode.metadata)
        payload = _canonical(episode.model_dump(mode="json"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        old = (
            db.execute(
                text(f"SELECT * FROM {self._table(db, 'episodes')} WHERE source_id=:p0"),
                {"p0": episode.source_id},
            )
            .mappings()
            .fetchone()
        )
        if old:
            if old["digest"] != digest:
                raise Conflict("Source identity already exists with different content or metadata")
            return Receipt(episode=self._episode(old, world_id), replayed=True)
        episode_id, received = uuid4().hex, _now()
        state = "pending" if episode.project else "disabled"
        db.execute(
            text(
                f"INSERT INTO {self._table(db, 'episodes')} "
                f"(id,source_id,digest,payload,received_at,"
                f"actor_id,agent_id,projection_state,"
                f"agent_uuid,invocation_id,"
                f"parent_invocation_id) VALUES (:p0,:p1,:p2,"
                f":p3,:p4,:p5,:p6,:p7,:p8,:p9,:p10)"
            ),
            {
                "p0": episode_id,
                "p1": episode.source_id,
                "p2": digest,
                "p3": payload,
                "p4": received,
                "p5": principal.user_id,
                "p6": principal.agent_id,
                "p7": state,
                "p8": str(principal.agent_uuid) if principal.agent_uuid else None,
                "p9": str(principal.invocation_id) if principal.invocation_id else None,
                "p10": str(principal.parent_invocation_id)
                if principal.parent_invocation_id
                else None,
            },
        )
        if episode.project:
            db.execute(
                text(
                    f"INSERT INTO "
                    f"{self._table(db, 'projection_jobs')} "
                    f"(episode_id,state,updated_at) VALUES (:p0,"
                    f":p1,:p2)"
                ),
                {"p0": episode_id, "p1": "pending", "p2": received},
            )
        db.execute(
            text(
                f"INSERT INTO {self._table(db, 'audit')} "
                f"(event,episode_id,actor_id,occurred_at) "
                f"VALUES (:p0,:p1,:p2,:p3)"
            ),
            {"p0": "capture", "p1": episode_id, "p2": principal.user_id, "p3": received},
        )
        row = (
            db.execute(
                text(f"SELECT * FROM {self._table(db, 'episodes')} WHERE id=:p0"),
                {"p0": episode_id},
            )
            .mappings()
            .fetchone()
        )
        return Receipt(episode=self._episode(row, world_id))

    def capture(
        self, principal: Principal, world_id: str, episode: EpisodeInput, shared: bool = False
    ) -> Receipt:
        operation = "publish" if shared else "write"
        with self._control() as control:
            scope = self._scope_authorized(
                control, principal, world_id, episode.scope_id, operation
            )
            row = self._authorized(control, principal, world_id, write=True)
        sid = scope["id"]
        world = self._hydrate(row, sid)
        requires_publication = scope["kind"] == "run-shared" or (
            scope["kind"] == "world-shared" and world.kind not in _PRIVATE
        )
        if shared != requires_publication:
            raise Forbidden(
                "Use explicit publication for shared worlds and capture for private worlds"
            )
        normalized = episode.model_copy(update={"scope_id": UUID(sid)})
        attributed = self._capture_principal(principal, world_id, sid, episode.context_id)
        with self._world_db(world_id, sid) as db, self._transaction(db):
            receipt = self._insert(db, attributed, world_id, normalized)
        return receipt

    def get_episode(
        self, principal: Principal, world_id: str, episode_id: str, scope_id=None
    ) -> Episode:
        world = self.get_world(principal, world_id, scope_id=scope_id)
        with self._world_db(world_id, world.scope_id) as db:
            table = self._table(db, "episodes")
            row = (
                db.execute(
                    select(table).where(
                        table.c.id == episode_id, lifecycle.visible_clause(self, db, table)
                    )
                )
                .mappings()
                .fetchone()
            )
        if row is None:
            raise NotFound("Episode not found")
        return self._episode(row, world_id)

    def list_episodes(
        self, principal: Principal, world_id: str, scope_id=None, limit=None
    ) -> list[Episode]:
        world = self.get_world(principal, world_id, scope_id=scope_id)
        if limit is not None and not 1 <= limit <= 1000:
            raise InvalidRequest("Episode limit must be between 1 and 1000")
        with self._world_db(world_id, world.scope_id) as db:
            table = self._table(db, "episodes")
            statement = select(table).where(lifecycle.visible_clause(self, db, table))
            statement = (
                statement.order_by(table.c.sequence)
                if limit is None
                else statement.order_by(table.c.sequence.desc()).limit(limit)
            )
            return [self._episode(row, world_id) for row in db.execute(statement).mappings()]

    def search(
        self,
        principal: Principal,
        world_ids: list[str],
        query: str,
        limit: int = 10,
        scope_id=None,
        scopes=None,
    ) -> list[Episode]:
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        selections = [(world_id, scope_id) for world_id in world_ids]
        selections.extend((selection.world_id, selection.scope_id) for selection in (scopes or []))
        selected = set()
        with self._control() as db:
            self._principal(db, principal)
            for world_id, sid in selections:
                scope = self._scope_authorized(db, principal, world_id, sid)
                selected.add((world_id, scope["id"]))
        words = query.casefold().split()
        if not words:
            return []
        matches = []
        for world_id, sid in selected:
            with self._world_db(world_id, sid) as db:
                table = self._table(db, "episodes")
                statement = select(table).where(lifecycle.visible_clause(self, db, table))
                for row in db.execute(statement.order_by(table.c.sequence.desc())).mappings():
                    content = json.loads(row["payload"])["content"].casefold()
                    if all(word in content for word in words):
                        matches.append(self._episode(row, world_id))
        return sorted(matches, key=lambda episode: episode.received_at, reverse=True)[:limit]

    def snapshot(self, principal: Principal, world_id: str, scope_id=None) -> dict:
        world = self.get_world(principal, world_id, scope_id=scope_id)
        with self._world_db(world_id, world.scope_id) as db, self._transaction(db):
            projection = db.execute(select(self._table(db, "projection"))).mappings().one()
            world = world.model_copy(
                update={
                    "generation": projection["generation"],
                    "projection_state": projection["state"],
                }
            )
            table = self._table(db, "episodes")
            visible = lifecycle.visible_clause(self, db, table)
            episodes = [
                self._episode(row, world_id).model_dump(mode="json")
                for row in db.execute(
                    select(table).where(visible).order_by(table.c.sequence)
                ).mappings()
            ]
            audit_table = self._table(db, "audit")
            audit = [
                dict(row)
                for row in db.execute(
                    select(audit_table)
                    .where(audit_table.c.episode_id.in_(select(table.c.id).where(visible)))
                    .order_by(audit_table.c.sequence)
                ).mappings()
            ]
        return {
            "world": world.model_dump(mode="json"),
            "episodes": episodes,
            "audit": audit,
            "projection_configuration": json.loads(projection["configuration"]),
            "scope": self.get_scope(principal, world_id, world.scope_id).model_dump(mode="json"),
        }

    def import_episodes(
        self,
        principal: Principal,
        world_id: str,
        scope_id: UUID | str,
        episodes: list[EpisodeInput],
    ) -> dict:
        """Atomically import historical evidence into the caller's private audience."""
        if not 1 <= len(episodes) <= MAX_IMPORT_EPISODES:
            raise InvalidRequest(f"An import requires 1 to {MAX_IMPORT_EPISODES} episodes")
        with self._control() as control:
            world = self._authorized(control, principal, world_id, write=True)
            scope = self._scope_authorized(control, principal, world_id, scope_id, "write")
            if WorldKind(world["kind"]) not in _PRIVATE or world["owner_id"] != principal.user_id:
                raise Forbidden("Imports require a private world owned by the importing user")
            if scope["kind"] not in {
                ScopeKind.AGENT_PERSISTENT.value,
                ScopeKind.INVOCATION_PRIVATE.value,
            }:
                raise Forbidden("Imports require an agent-persistent or invocation-private scope")
            # Same-user agents may share private memory. A different user's live
            # explicit read grant makes this destination unsuitable for import.
            readers = (
                control.execute(
                    text(
                        f"SELECT c.* FROM "
                        f"{self._table(control, 'credentials')} c JOIN "
                        f"{self._table(control, 'scope_grants')} g ON "
                        f"g.credential_id=c.id WHERE c.revoked=0 AND "
                        f"c.user_id<>:p0 AND g.scope_id=:p1 AND "
                        f"g.operation='read' LIMIT 4097"
                    ),
                    {"p0": principal.user_id, "p1": scope["id"]},
                )
                .mappings()
                .fetchall()
            )
            if len(readers) > 4096:
                raise Forbidden("Import destination reader audience cannot be verified")
            for row in readers:
                try:
                    reader = self._credential_principal(control, row)
                    self._scope_authorized(control, reader, world_id, scope["id"])
                except (Unauthorized, Forbidden, NotFound):
                    continue
                raise Forbidden("Imports cannot target a scope readable by another user")
            sid = UUID(scope["id"])
            normalized = []
            payload_size = len(b'{"episodes":[]}')
            for episode in episodes:
                if episode.scope_id is not None and episode.scope_id != sid:
                    raise Forbidden("Import episodes must match the explicit target scope")
                if episode.context_id is not None or episode.source_type == "publication":
                    raise Forbidden("Imports cannot contain context bindings or publications")
                value = episode.model_copy(update={"scope_id": sid})
                # Metadata must remain finite before Pydantic's JSON conversion.
                _canonical(value.metadata)
                try:
                    encoded = json.dumps(
                        value.model_dump(mode="json"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                except (TypeError, ValueError, UnicodeError) as error:
                    raise InvalidRequest(
                        "Import episodes must contain valid JSON and Unicode"
                    ) from error
                payload_size += len(encoded) + bool(normalized)
                if payload_size > MAX_IMPORT_BYTES:
                    raise InvalidRequest("Import payload exceeds 32 MiB")
                normalized.append(value)
            attributed = self._capture_principal(principal, world_id, scope["id"], None)
            counts = {"imported": 0, "replayed": 0}
            # Keep the control lock through the source commit, so concurrent
            # grant changes in this daemon linearize before or after this batch.
            with self._world_db(world_id, sid) as db, self._transaction(db):
                for episode in normalized:
                    receipt = self._insert(db, attributed, world_id, episode)
                    counts["replayed" if receipt.replayed else "imported"] += 1
            return counts

    def restore(
        self, principal: Principal, world_id: str, episodes: list[EpisodeInput], scope_id=None
    ) -> dict:
        # Administrative imports may seed shared worlds; agent capture may not.
        with self._control() as db:
            self._admin(db, principal)
            self._authorized(db, principal, world_id, write=True)
            scope = self._scope_authorized(db, principal, world_id, scope_id, "write")
            sid = scope["id"]
            allowed = {sid}
            if scope["kind"] == "world-shared":
                allowed.add(None)
            if any(
                (str(episode.scope_id) if episode.scope_id else None) not in allowed
                for episode in episodes
            ):
                raise Forbidden("Restore episodes must match the explicit target scope")
            if any(episode.context_id is not None for episode in episodes):
                raise Forbidden(
                    "Restore must clear source context bindings and retain them as origin metadata"
                )
        counts = {"imported": 0, "replayed": 0}
        with self._world_db(world_id, sid) as db, self._transaction(db):
            for episode in episodes:
                normalized = episode.model_copy(update={"scope_id": UUID(sid)})
                receipt = self._insert(db, principal, world_id, normalized)
                counts["replayed" if receipt.replayed else "imported"] += 1
        return counts

    def projection_worlds(self) -> list[World]:
        with self._control() as db:
            rows = (
                db.execute(text(f"SELECT * FROM {self._table(db, 'worlds')} ORDER BY id"))
                .mappings()
                .fetchall()
            )
        targets = [self._hydrate(row) for row in rows]
        with self._control() as db:
            scopes = (
                db.execute(
                    text(
                        f"SELECT * FROM {self._table(db, 'scopes')} "
                        f"WHERE kind<>'world-shared' ORDER BY world_id,"
                        f"id"
                    )
                )
                .mappings()
                .fetchall()
            )
        worlds = {row["id"]: row for row in rows}
        targets.extend(self._hydrate(worlds[row["world_id"]], row["id"]) for row in scopes)
        return targets

    def projection_resource(self, world_id: str, scope_id=None) -> str:
        sid = (
            scope_uuid(scope_id) if scope_id else world_shared_scope_id(self.authority_id, world_id)
        )
        return f"projection:{world_id}:{sid}"

    def _projection_lease(self, lease: WorkerLease, world_id: str, scope_id=None):
        if lease.resource != self.projection_resource(world_id, scope_id):
            raise LeaseLost("Worker lease does not belong to the selected projection")
        return self.persistence.fenced(lease)

    def generation_inventory(self, world_id=None, scope_id=None) -> list[dict]:
        with self._control() as db:
            table = self._table(db, "projection_generations")
            statement = select(table)
            if world_id is not None:
                statement = statement.where(table.c.world_id == world_id)
            if scope_id is not None:
                statement = statement.where(table.c.scope_id == scope_uuid(scope_id))
            return [
                dict(row)
                for row in db.execute(
                    statement.order_by(table.c.world_id, table.c.scope_id, table.c.created_at)
                ).mappings()
            ]

    def register_generation(
        self, world_id: str, generation: str, *, lease: WorkerLease, scope_id=None
    ) -> None:
        """Inventory a namespace before its first possible external write."""
        sid = (
            scope_uuid(scope_id) if scope_id else world_shared_scope_id(self.authority_id, world_id)
        )
        with self._projection_lease(lease, world_id, sid), self._control() as db:
            table = self._table(db, "projection_generations")
            self.persistence.insert_ignore(
                db,
                table,
                dict(
                    world_id=world_id,
                    scope_id=sid,
                    generation=generation,
                    state="building",
                    created_at=_now(),
                    lease_fence=lease.fence,
                ),
                ["world_id", "scope_id", "generation"],
            )
            row = (
                db.execute(
                    select(table).where(
                        table.c.world_id == world_id,
                        table.c.scope_id == sid,
                        table.c.generation == generation,
                    )
                )
                .mappings()
                .one()
            )
            if row["state"] in {"failed", "retired", "purged"}:
                raise Unavailable("Graph generation is quarantined; rebuild into a fresh namespace")
            db.execute(
                table.update()
                .where(
                    table.c.world_id == world_id,
                    table.c.scope_id == sid,
                    table.c.generation == generation,
                )
                .values(lease_fence=lease.fence)
            )

    def _catalog_state(self, db, world_id, scope_id, generation, state, lease):
        table = self._table(db, "projection_generations")
        sid = (
            scope_uuid(scope_id) if scope_id else world_shared_scope_id(self.authority_id, world_id)
        )
        row = (
            db.execute(
                select(table).where(
                    table.c.world_id == world_id,
                    table.c.scope_id == sid,
                    table.c.generation == generation,
                )
            )
            .mappings()
            .fetchone()
        )
        if row is None or row["lease_fence"] != lease.fence:
            raise LeaseLost("Graph generation is not registered to the current worker")
        if state == "active":
            if row["state"] not in {"building", "active"}:
                raise Unavailable("A quarantined graph namespace cannot be activated")
            db.execute(
                table.update()
                .where(
                    table.c.world_id == world_id,
                    table.c.scope_id == sid,
                    table.c.generation != generation,
                    table.c.state == "active",
                )
                .values(state="retired", retired_at=_now())
            )
        db.execute(
            table.update()
            .where(
                table.c.world_id == world_id,
                table.c.scope_id == sid,
                table.c.generation == generation,
            )
            .values(state=state, retired_at=_now() if state in {"failed", "retired"} else None)
        )

    def fail_generation(self, world_id, generation, *, lease: WorkerLease, scope_id=None):
        with self._projection_lease(lease, world_id, scope_id), self._control() as db:
            self._catalog_state(db, world_id, scope_id, generation, "failed", lease)

    def pending_jobs(self, world_id: str, scope_id=None) -> list[Episode]:
        with self._world_db(world_id, scope_id) as db:
            episodes = self._table(db, "episodes")
            return [
                self._episode(row, world_id)
                for row in db.execute(
                    select(episodes)
                    .where(
                        episodes.c.projection_state == "pending",
                        lifecycle.visible_clause(self, db, episodes),
                    )
                    .order_by(episodes.c.sequence)
                ).mappings()
            ]

    def assert_projection_live(self, world_id, scope_id=None, *, lease: WorkerLease):
        with (
            self._projection_lease(lease, world_id, scope_id),
            self._world_db(world_id, scope_id) as db,
            self._transaction(db),
        ):
            if lifecycle.has_pending_purges(self, world_id, scope_id):
                raise Unavailable("Graph erasure is pending for the selected scope")

    def mark_processing(
        self, world_id: str, episode_id: str, scope_id=None, *, lease: WorkerLease
    ) -> Episode | None:
        with (
            self._projection_lease(lease, world_id, scope_id),
            self._world_db(world_id, scope_id) as db,
            self._transaction(db),
        ):
            if lifecycle.has_pending_purges(self, world_id, scope_id):
                raise Unavailable("Graph erasure is pending for the selected scope")
            jobs, episodes = self._table(db, "projection_jobs"), self._table(db, "episodes")
            projection = self._table(db, "projection")
            if db.execute(select(projection.c.state)).scalar_one() == "degraded":
                raise Unavailable("World projection requires a rebuild")
            changed = db.execute(
                jobs.update()
                .where(jobs.c.episode_id == episode_id, jobs.c.state == "pending")
                .values(
                    state="processing",
                    attempts=jobs.c.attempts + 1,
                    updated_at=_now(),
                    lease_owner=lease.owner,
                    lease_fence=lease.fence,
                    lease_expires_at=lease.expires_at,
                )
            ).rowcount
            if not changed:
                return None
            db.execute(
                episodes.update()
                .where(episodes.c.id == episode_id)
                .values(projection_state="processing")
            )
            row = db.execute(select(episodes).where(episodes.c.id == episode_id)).mappings().one()
            return self._episode(row, world_id)

    def mark_projected(
        self, world_id: str, episode_id: str, generation: str, scope_id=None, *, lease: WorkerLease
    ) -> None:
        with (
            self._projection_lease(lease, world_id, scope_id),
            self._control() as control,
            self._world_db(world_id, scope_id) as db,
            self._transaction(db),
        ):
            if lifecycle.has_pending_purges(self, world_id, scope_id):
                raise Unavailable("Graph erasure is pending for the selected scope")
            jobs, episodes = self._table(db, "projection_jobs"), self._table(db, "episodes")
            projection = self._table(db, "projection")
            if db.execute(select(projection.c.state)).scalar_one() == "degraded":
                raise Unavailable("World projection requires a rebuild")
            updated = db.execute(
                jobs.update()
                .where(
                    jobs.c.episode_id == episode_id,
                    jobs.c.state == "processing",
                    jobs.c.lease_owner == lease.owner,
                    jobs.c.lease_fence == lease.fence,
                )
                .values(state="projected", generation=generation, error=None, updated_at=_now())
            )
            if not updated.rowcount:
                raise LeaseLost("Projection job is no longer owned by this worker")
            self._catalog_state(control, world_id, scope_id, generation, "active", lease)
            db.execute(
                episodes.update()
                .where(episodes.c.id == episode_id)
                .values(projection_state="projected")
            )
            db.execute(projection.update().values(generation=generation, state="ready"))

    def mark_unknown(
        self, world_id: str, episode_id: str, error: str, scope_id=None, *, lease: WorkerLease
    ) -> None:
        with (
            self._projection_lease(lease, world_id, scope_id),
            self._world_db(world_id, scope_id) as db,
            self._transaction(db),
        ):
            jobs, episodes = self._table(db, "projection_jobs"), self._table(db, "episodes")
            changed = db.execute(
                jobs.update()
                .where(
                    jobs.c.episode_id == episode_id,
                    jobs.c.state == "processing",
                    jobs.c.lease_owner == lease.owner,
                    jobs.c.lease_fence == lease.fence,
                )
                .values(state="unknown", error=error[:2000], updated_at=_now())
            ).rowcount
            if not changed:
                raise LeaseLost("Projection job is no longer owned by this worker")
            db.execute(self._table(db, "projection").update().values(state="degraded"))
            db.execute(
                episodes.update()
                .where(episodes.c.id == episode_id)
                .values(projection_state="unknown")
            )

    def set_projection(
        self,
        world_id: str,
        generation: str | None,
        state: str,
        scope_id=None,
        *,
        lease: WorkerLease | None = None,
    ) -> None:
        if state not in {"disabled", "ready", "degraded"}:
            raise ValueError("Invalid projection state")
        from contextlib import nullcontext

        with self._projection_lease(lease, world_id, scope_id) if lease else nullcontext():
            with self._world_db(world_id, scope_id) as db, self._transaction(db):
                db.execute(
                    self._table(db, "projection")
                    .update()
                    .values(generation=generation, state=state)
                )

    def set_projection_configuration(
        self, world_id: str, configuration: dict, scope_id=None, *, lease: WorkerLease | None = None
    ) -> None:
        from contextlib import nullcontext

        with self._projection_lease(lease, world_id, scope_id) if lease else nullcontext():
            with self._world_db(world_id, scope_id) as db, self._transaction(db):
                db.execute(
                    self._table(db, "projection")
                    .update()
                    .values(configuration=_canonical(configuration))
                )

    def projection_configuration(self, world_id: str, scope_id=None) -> dict:
        with self._world_db(world_id, scope_id) as db:
            return json.loads(
                db.execute(select(self._table(db, "projection").c.configuration)).scalar_one()
            )

    def projection_episodes(
        self, world_id: str, include_unprojected: bool = False, scope_id=None
    ) -> list[Episode]:
        with self._world_db(world_id, scope_id) as db:
            table = self._table(db, "episodes")
            episodes = [
                self._episode(row, world_id)
                for row in db.execute(
                    select(table)
                    .where(lifecycle.visible_clause(self, db, table))
                    .order_by(table.c.sequence)
                ).mappings()
            ]
        return [
            episode
            for episode in episodes
            if (include_unprojected or episode.project or episode.projection_state != "disabled")
        ]

    def activate_generation(
        self,
        world_id: str,
        generation: str,
        episode_ids: list[str],
        configuration: dict | None = None,
        scope_id=None,
        *,
        lease: WorkerLease,
    ) -> None:
        with (
            self._projection_lease(lease, world_id, scope_id),
            self._control() as control,
            self._world_db(world_id, scope_id) as db,
            self._transaction(db),
        ):
            if lifecycle.has_pending_purges(self, world_id, scope_id):
                raise Unavailable("Graph erasure is pending for the selected scope")
            episodes, jobs = self._table(db, "episodes"), self._table(db, "projection_jobs")
            for episode_id in episode_ids:
                if (
                    db.execute(
                        select(episodes.c.id).where(
                            episodes.c.id == episode_id,
                            lifecycle.visible_clause(self, db, episodes),
                        )
                    ).scalar_one_or_none()
                    is None
                ):
                    raise NotFound("Rebuild episode not found")
                db.execute(
                    episodes.update()
                    .where(episodes.c.id == episode_id)
                    .values(projection_state="projected")
                )
                self.persistence.insert_ignore(
                    db,
                    jobs,
                    dict(
                        episode_id=episode_id,
                        state="projected",
                        generation=generation,
                        updated_at=_now(),
                    ),
                    ["episode_id"],
                )
                db.execute(
                    jobs.update()
                    .where(jobs.c.episode_id == episode_id)
                    .values(
                        state="projected",
                        generation=generation,
                        error=None,
                        updated_at=_now(),
                        lease_owner=lease.owner,
                        lease_fence=lease.fence,
                        lease_expires_at=lease.expires_at,
                    )
                )
            self._catalog_state(control, world_id, scope_id, generation, "active", lease)
            values = dict(generation=generation, state="ready")
            if configuration is not None:
                values["configuration"] = _canonical(configuration)
            db.execute(self._table(db, "projection").update().values(**values))

    def recover_scope_projection(self, world_id: str, scope_id=None, *, lease: WorkerLease) -> int:
        """Quarantine expired owner's unknown writes, never replay their namespace."""
        with (
            self._projection_lease(lease, world_id, scope_id),
            self._control() as control,
            self._world_db(world_id, scope_id) as db,
            self._transaction(db),
        ):
            episodes, jobs = self._table(db, "episodes"), self._table(db, "projection_jobs")
            changed = db.execute(
                jobs.update()
                .where(jobs.c.state == "processing")
                .values(
                    state="unknown",
                    error="Previous worker lost its lease during graph delivery",
                    updated_at=_now(),
                )
            ).rowcount
            if changed:
                db.execute(
                    episodes.update()
                    .where(episodes.c.projection_state == "processing")
                    .values(projection_state="unknown")
                )
                db.execute(self._table(db, "projection").update().values(state="degraded"))
            # A lost rebuild can leave an inventoried namespace without a
            # processing source job. Retain it as failed for later exact purge.
            catalog = self._table(control, "projection_generations")
            sid = (
                scope_uuid(scope_id)
                if scope_id
                else world_shared_scope_id(self.authority_id, world_id)
            )
            states = ["building", "active"] if changed else ["building"]
            control.execute(
                catalog.update()
                .where(
                    catalog.c.world_id == world_id,
                    catalog.c.scope_id == sid,
                    catalog.c.lease_fence < lease.fence,
                    catalog.c.state.in_(states),
                )
                .values(state="failed", retired_at=_now())
            )
            return changed

    def recover_interrupted_projections(self) -> int:
        """Recover expired ownership only; starting a replica never steals live work."""
        recovered = 0
        for world in self.projection_worlds():
            lease = self.persistence.acquire_lease(
                self.projection_resource(world.id, world.scope_id)
            )
            if lease is None:
                continue
            try:
                recovered += self.recover_scope_projection(world.id, world.scope_id, lease=lease)
            finally:
                self.persistence.release_lease(lease)
        return recovered
