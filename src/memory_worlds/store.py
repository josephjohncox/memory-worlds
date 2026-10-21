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
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

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
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).expanduser().absolute()
        self._control_lock = threading.RLock()
        self._locks_lock = threading.Lock()
        self._world_locks: dict[str, threading.RLock] = {}
        self._directory(self.data_dir)
        self._directory(self.data_dir / "worlds")
        self.control_path = self.data_dir / "control.sqlite3"
        with self._control() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS worlds (
                    id TEXT PRIMARY KEY, authority_id TEXT NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL,
                    owner_id TEXT NOT NULL, created_at TEXT NOT NULL, alias TEXT
                );
                CREATE TABLE IF NOT EXISTS credentials (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    admin INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS grants (
                    credential_id TEXT NOT NULL REFERENCES credentials(id),
                    world_id TEXT NOT NULL REFERENCES worlds(id), role TEXT NOT NULL,
                    PRIMARY KEY (credential_id, world_id)
                );
                CREATE TABLE IF NOT EXISTS identities (
                    issuer TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    private_world_id TEXT NOT NULL REFERENCES worlds(id),
                    PRIMARY KEY (issuer, subject)
                );
                CREATE TABLE IF NOT EXISTS identity_sessions (
                    credential_id TEXT PRIMARY KEY REFERENCES credentials(id),
                    user_id TEXT NOT NULL REFERENCES identities(user_id),
                    token_digest TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS identity_group_state (
                    user_id TEXT PRIMARY KEY REFERENCES identities(user_id),
                    claims_iat REAL NOT NULL, groups_digest TEXT NOT NULL,
                    mappings_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    user_id TEXT NOT NULL, world_id TEXT NOT NULL REFERENCES worlds(id),
                    role TEXT NOT NULL CHECK(role IN ('reader','writer','manager')),
                    source TEXT NOT NULL,
                    PRIMARY KEY (user_id, world_id, source)
                );
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('authority_id', ?)", (uuid4().hex,))
            self.authority_id = db.execute(
                "SELECT value FROM metadata WHERE key='authority_id'"
            ).fetchone()[0]
            self._initialize_scopes(db)

    @staticmethod
    def _directory(path: Path) -> None:
        if path.is_symlink():
            raise Conflict("Memory storage directories cannot be symbolic links")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        if path.is_symlink():
            raise Conflict("Memory database files cannot be symbolic links")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        path.chmod(0o600)
        db = sqlite3.connect(path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _control(self) -> Iterator[sqlite3.Connection]:
        with self._control_lock:
            db = self._connect(self.control_path)
            try:
                yield db
            finally:
                db.close()

    def _lock(self, world_id: str) -> threading.RLock:
        if not _ID.fullmatch(world_id):
            raise NotFound("World not found")
        with self._locks_lock:
            return self._world_locks.setdefault(world_id, threading.RLock())

    def world_path(self, world_id: str, scope_id=None) -> Path:
        """Trusted administration helper; callers still authorize the world first."""
        self._lock(world_id)
        directory = self.data_dir / "worlds" / world_id
        if scope_id is not None:
            sid = scope_uuid(scope_id)
            if sid != world_shared_scope_id(self.authority_id, world_id):
                directory = directory / "scopes" / sid
        return directory / "source.sqlite3"

    @contextmanager
    def _world_db(self, world_id: str, scope_id=None) -> Iterator[sqlite3.Connection]:
        with self._lock(world_id):
            path = self.world_path(world_id, scope_id)
            if not path.parent.is_dir() or path.parent.is_symlink():
                raise NotFound("World storage not found")
            db = self._connect(path)
            try:
                yield db
            finally:
                db.close()

    @staticmethod
    @contextmanager
    def _transaction(db: sqlite3.Connection) -> Iterator[None]:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _initialize_world(self, world_id: str, scope_id=None) -> None:
        self._directory(self.data_dir / "worlds" / world_id)
        if scope_id is not None:
            self._directory(self.data_dir / "worlds" / world_id / "scopes")
            self._directory(self.world_path(world_id, scope_id).parent)
        with self._world_db(world_id, scope_id) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL, payload TEXT NOT NULL,
                    received_at TEXT NOT NULL, actor_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    projection_state TEXT NOT NULL,
                    agent_uuid TEXT, invocation_id TEXT, parent_invocation_id TEXT
                );
                CREATE TABLE IF NOT EXISTS projection (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    generation TEXT, state TEXT NOT NULL,
                    configuration TEXT NOT NULL DEFAULT '{}'
                );
                INSERT OR IGNORE INTO projection (singleton,generation,state)
                    VALUES (1, NULL, 'disabled');
                CREATE TABLE IF NOT EXISTS projection_jobs (
                    episode_id TEXT PRIMARY KEY REFERENCES episodes(id),
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    generation TEXT, error TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL, episode_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL, occurred_at TEXT NOT NULL
                );
            """)

    def _principal(self, db: sqlite3.Connection, principal: Principal) -> Principal:
        self._validate_ancestors(db, principal.credential_id)
        row = db.execute(
            "SELECT * FROM credentials WHERE id=? AND revoked=0", (principal.credential_id,)
        ).fetchone()
        if row is None:
            raise Unauthorized("Credential is invalid or revoked")
        session = db.execute(
            "SELECT s.expires_at,i.enabled FROM identity_sessions s "
            "JOIN identities i ON i.user_id=s.user_id WHERE s.credential_id=?",
            (principal.credential_id,),
        ).fetchone()
        identity = db.execute(
            "SELECT enabled FROM identities WHERE user_id=?", (row["user_id"],)
        ).fetchone()
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
                "UPDATE credentials SET revoked=1 WHERE id IN ("
                "SELECT s.credential_id FROM identity_sessions s "
                "JOIN identities i ON i.user_id=s.user_id "
                "WHERE ? IS NULL OR i.issuer<>?)",
                (active_issuer, active_issuer),
            )

    def reconcile_oidc_policy(self, issuer: str, policy_digest: str) -> None:
        """Invalidate sessions and descendants when the trusted token policy changes."""
        key = "oidc-policy:" + issuer
        with self._control() as db, self._transaction(db):
            previous = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            if previous is None or previous[0] != policy_digest:
                db.execute(
                    "UPDATE credentials SET revoked=1 WHERE id IN ("
                    "SELECT s.credential_id FROM identity_sessions s "
                    "JOIN identities i ON i.user_id=s.user_id WHERE i.issuer=?)",
                    (issuer,),
                )
            db.execute(
                "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (key, policy_digest),
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
                    world = db.execute("SELECT kind FROM worlds WHERE id=?", (world_id,)).fetchone()
                    if world is None or WorldKind(world["kind"]) in _PRIVATE:
                        raise ValueError(
                            "OIDC group mappings must target existing shared world IDs"
                        )
            db.execute(
                "DELETE FROM memberships WHERE source=? AND user_id IN ("
                "SELECT i.user_id FROM identities i "
                "LEFT JOIN identity_group_state g ON g.user_id=i.user_id "
                "WHERE i.issuer=? AND (g.mappings_digest IS NULL OR g.mappings_digest<>?))",
                ("oidc:" + issuer, issuer, digest),
            )

    def _admin(self, db: sqlite3.Connection, principal: Principal) -> None:
        if not self._principal(db, principal).admin:
            raise Forbidden("Administrative credential required")

    @staticmethod
    def _token(db: sqlite3.Connection, request: CreateToken) -> str:
        credential_id = uuid4().hex
        token = f"mw_{credential_id}.{secrets.token_urlsafe(32)}"
        db.execute(
            "INSERT INTO credentials (id,token_hash,user_id,agent_id,admin) VALUES (?,?,?,?,?)",
            (
                credential_id,
                hashlib.sha256(token.encode()).hexdigest(),
                request.user_id,
                request.agent_id,
                int(request.admin),
            ),
        )
        for world_id, role in request.grants.items():
            db.execute("INSERT INTO grants VALUES (?,?,?)", (credential_id, world_id, role.value))
        return token

    def _new_world(self, db: sqlite3.Connection, request: CreateWorld, owner_id: str) -> World:
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
            "INSERT INTO worlds VALUES (?,?,?,?,?,?,?)",
            (
                world.id,
                world.authority_id,
                world.name,
                world.kind.value,
                world.owner_id,
                world.created_at.isoformat(),
                world.alias,
            ),
        )
        self._create_shared_scope(db, world)
        return world

    def bootstrap(self, user_id: str = "owner") -> tuple[World, str]:
        request = CreateToken(user_id=user_id, agent_id="admin", admin=True)
        with self._control() as db, self._transaction(db):
            if db.execute("SELECT 1 FROM metadata WHERE key='bootstrapped'").fetchone():
                raise Conflict("This authority is already initialized")
            world = self._new_world(db, CreateWorld(name="Personal"), request.user_id)
            token = self._token(db, request)
            db.execute("INSERT INTO metadata VALUES ('bootstrapped', '1')")
            return world, token

    def authenticate(self, token: str) -> Principal:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._control() as db:
            row = db.execute(
                "SELECT * FROM credentials WHERE token_hash=? AND revoked=0", (digest,)
            ).fetchone()
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
            identity = db.execute(
                "SELECT * FROM identities WHERE issuer=? AND subject=?", (issuer, subject)
            ).fetchone()
            if identity is None:
                user_id = "user_" + uuid4().hex
                world = self._new_world(
                    db, CreateWorld(name="Private work", kind=WorldKind.PRIVATE_WORK), user_id
                )
                db.execute(
                    "INSERT INTO identities (issuer,subject,user_id,private_world_id) "
                    "VALUES (?,?,?,?)",
                    (issuer, subject, user_id, world.id),
                )
                db.execute(
                    "INSERT INTO memberships VALUES (?,?,?,'manual')",
                    (user_id, world.id, Role.MANAGER.value),
                )
            else:
                if not identity["enabled"]:
                    raise Unauthorized("Identity is disabled")
                user_id = identity["user_id"]
            session = db.execute(
                "SELECT c.* FROM credentials c JOIN identity_sessions s "
                "ON s.credential_id=c.id WHERE s.token_digest=?",
                (token_digest,),
            ).fetchone()
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
                previous = db.execute(
                    "SELECT * FROM identity_group_state WHERE user_id=?", (user_id,)
                ).fetchone()
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
                    "INSERT INTO identity_group_state VALUES (?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET claims_iat=excluded.claims_iat,"
                    "groups_digest=excluded.groups_digest,mappings_digest=excluded.mappings_digest",
                    (user_id, issued_at, groups_digest, mappings_digest),
                )
            else:
                # Disabling the map removes mapped memberships while retaining the
                # signed-claim watermark in case the operator enables mappings again.
                db.execute(
                    "UPDATE identity_group_state SET mappings_digest=? WHERE user_id=?",
                    (hashlib.sha256(_canonical({}).encode()).hexdigest(), user_id),
                )
            # An operator controls the map. Token strings can never name arbitrary worlds.
            mapped: dict[str, Role] = {}
            rank = {Role.READER: 0, Role.WRITER: 1, Role.MANAGER: 2}
            for group in groups or []:
                for world_id, role in (group_mappings or {}).get(group, {}).items():
                    world = db.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
                    if world is None or WorldKind(world["kind"]) in _PRIVATE:
                        raise Forbidden(
                            "Identity group mappings must target existing shared worlds"
                        )
                    if world_id not in mapped or rank[role] > rank[mapped[world_id]]:
                        mapped[world_id] = role
            db.execute(
                "DELETE FROM memberships WHERE user_id=? AND source=?", (user_id, "oidc:" + issuer)
            )
            for world_id, role in mapped.items():
                db.execute(
                    "INSERT INTO memberships VALUES (?,?,?,?)",
                    (user_id, world_id, role.value, "oidc:" + issuer),
                )
            if session is not None:
                return principal
            credential_id = uuid4().hex
            agent_id = "oidc"
            db.execute(
                "INSERT INTO credentials (id,token_hash,user_id,agent_id,admin) VALUES (?,?,?,?,0)",
                (credential_id, "oidc:" + token_digest, user_id, agent_id),
            )
            db.execute(
                "INSERT INTO identity_sessions VALUES (?,?,?,?)",
                (credential_id, user_id, token_digest, expires_at),
            )
            return Principal(credential_id=credential_id, user_id=user_id, agent_id=agent_id)

    def list_users(self, principal: Principal) -> list[dict]:
        with self._control() as db:
            self._admin(db, principal)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT user_id,issuer,subject,enabled,private_world_id "
                    "FROM identities ORDER BY user_id"
                ).fetchall()
            ]

    def set_user_enabled(self, principal: Principal, user_id: str, enabled: bool) -> None:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            changed = db.execute(
                "UPDATE identities SET enabled=? WHERE user_id=?", (int(enabled), user_id)
            ).rowcount
            if not changed:
                raise NotFound("Identity not found")
            if not enabled:
                db.execute("UPDATE credentials SET revoked=1 WHERE user_id=?", (user_id,))

    def list_memberships(self, principal: Principal, world_id: str) -> list[dict]:
        with self._control() as db:
            self._admin(db, principal)
            self._authorized(db, principal, world_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT user_id,world_id,role,source FROM memberships "
                    "WHERE world_id=? ORDER BY user_id,source",
                    (world_id,),
                ).fetchall()
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
                    "DELETE FROM memberships WHERE user_id=? AND world_id=? AND source='manual'",
                    (user_id, world_id),
                )
            else:
                db.execute(
                    "INSERT INTO memberships VALUES (?,?,?,'manual') "
                    "ON CONFLICT(user_id,world_id,source) DO UPDATE SET role=excluded.role",
                    (user_id, world_id, role.value),
                )

    def create_world(self, principal: Principal, request: CreateWorld) -> World:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            return self._new_world(db, request, request.owner_id or principal.user_id)

    def set_world_alias(self, principal: Principal, world_id: str, request: UpdateAlias) -> World:
        with self._control() as db, self._transaction(db):
            self._admin(db, principal)
            self._authorized(db, principal, world_id)
            db.execute("UPDATE worlds SET alias=? WHERE id=?", (request.alias, world_id))
            row = db.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
        return self._hydrate(row)

    def _authorized(
        self, db: sqlite3.Connection, principal: Principal, world_id: str, write: bool = False
    ) -> sqlite3.Row:
        self._lock(world_id)
        self._principal(db, principal)
        row = db.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
        if row is None:
            raise NotFound("World not found")
        if principal.admin:
            return row
        if WorldKind(row["kind"]) in _PRIVATE and row["owner_id"] != principal.user_id:
            raise Forbidden("World access denied")
        identity_session = db.execute(
            "SELECT 1 FROM identity_sessions WHERE credential_id=?",
            (principal.credential_id,),
        ).fetchone()
        if identity_session is not None:
            memberships = db.execute(
                "SELECT role FROM memberships WHERE user_id=? AND world_id=?",
                (principal.user_id, world_id),
            ).fetchall()
            roles = {membership["role"] for membership in memberships}
        else:
            # A separate agent credential is an explicit capability scope. The
            # owner's broader user memberships cannot elevate a reader or grant
            # access to worlds absent from this credential's grants.
            grant = db.execute(
                "SELECT role FROM grants WHERE credential_id=? AND world_id=?",
                (principal.credential_id, world_id),
            ).fetchone()
            roles = {grant["role"]} if grant is not None else set()
        if not roles or (write and not roles.intersection(_WRITERS)):
            raise Forbidden("World access denied")
        context = db.execute(
            "SELECT parent_credential_id FROM credential_contexts WHERE credential_id=?",
            (principal.credential_id,),
        ).fetchone()
        if context is not None:
            parent = db.execute("SELECT * FROM credentials WHERE id=?", (context[0],)).fetchone()
            self._authorized(db, self._credential_principal(db, parent), world_id, write)
        return row

    def _hydrate(self, row: sqlite3.Row, scope_id=None) -> World:
        scope_id = UUID(scope_uuid(scope_id or world_shared_scope_id(self.authority_id, row["id"])))
        with self._world_db(row["id"], scope_id) as db:
            projection = db.execute("SELECT generation,state FROM projection").fetchone()
        return World(
            **dict(row), generation=projection[0], projection_state=projection[1], scope_id=scope_id
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
            rows = db.execute("SELECT * FROM worlds ORDER BY created_at,id").fetchall()
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
                row = db.execute(
                    "SELECT parent_credential_id FROM credential_contexts WHERE credential_id=?",
                    (credential_id,),
                ).fetchone()
                if row is None or row[0] != principal.credential_id:
                    raise Forbidden(
                        "Only administrators or the delegating parent may revoke a credential"
                    )
            changed = db.execute(
                "UPDATE credentials SET revoked=1 WHERE id=?", (credential_id,)
            ).rowcount
            if not changed:
                raise NotFound("Credential not found")

    @staticmethod
    def _episode(row: sqlite3.Row, world_id: str) -> Episode:
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
        self, db: sqlite3.Connection, principal: Principal, world_id: str, episode: EpisodeInput
    ) -> Receipt:
        # Validate before Pydantic's JSON conversion can turn NaN into null and
        # make two distinct inputs acquire the same source identity digest.
        _canonical(episode.metadata)
        payload = _canonical(episode.model_dump(mode="json"))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        old = db.execute(
            "SELECT * FROM episodes WHERE source_id=?", (episode.source_id,)
        ).fetchone()
        if old:
            if old["digest"] != digest:
                raise Conflict("Source identity already exists with different content or metadata")
            return Receipt(episode=self._episode(old, world_id), replayed=True)
        episode_id, received = uuid4().hex, _now()
        state = "pending" if episode.project else "disabled"
        db.execute(
            "INSERT INTO episodes "
            "(id,source_id,digest,payload,received_at,actor_id,agent_id,projection_state,"
            "agent_uuid,invocation_id,parent_invocation_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                episode_id,
                episode.source_id,
                digest,
                payload,
                received,
                principal.user_id,
                principal.agent_id,
                state,
                str(principal.agent_uuid) if principal.agent_uuid else None,
                str(principal.invocation_id) if principal.invocation_id else None,
                str(principal.parent_invocation_id) if principal.parent_invocation_id else None,
            ),
        )
        if episode.project:
            db.execute(
                "INSERT INTO projection_jobs (episode_id,state,updated_at) VALUES (?,?,?)",
                (episode_id, "pending", received),
            )
        db.execute(
            "INSERT INTO audit (event,episode_id,actor_id,occurred_at) VALUES (?,?,?,?)",
            ("capture", episode_id, principal.user_id, received),
        )
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
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
            row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
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
            return [
                self._episode(r, world_id)
                for r in db.execute(
                    "SELECT * FROM episodes ORDER BY sequence"
                    if limit is None
                    else "SELECT * FROM episodes ORDER BY sequence DESC LIMIT ?",
                    () if limit is None else (limit,),
                )
            ]

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
        # Authorize the complete selection before touching any source database.
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
                for row in db.execute("SELECT * FROM episodes ORDER BY sequence DESC"):
                    content = json.loads(row["payload"])["content"].casefold()
                    if all(word in content for word in words):
                        matches.append(self._episode(row, world_id))
        return sorted(matches, key=lambda episode: episode.received_at, reverse=True)[:limit]

    def snapshot(self, principal: Principal, world_id: str, scope_id=None) -> dict:
        world = self.get_world(principal, world_id, scope_id=scope_id)
        with self._world_db(world_id, world.scope_id) as db:
            db.execute("BEGIN")
            try:
                projection = db.execute(
                    "SELECT generation,state,configuration FROM projection"
                ).fetchone()
                world = world.model_copy(
                    update={
                        "generation": projection[0],
                        "projection_state": projection[1],
                    }
                )
                episodes = [
                    self._episode(row, world_id).model_dump(mode="json")
                    for row in db.execute("SELECT * FROM episodes ORDER BY sequence")
                ]
                audit = [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY sequence")]
            finally:
                db.rollback()
        result = {
            "world": world.model_dump(mode="json"),
            "episodes": episodes,
            "audit": audit,
            "projection_configuration": json.loads(projection[2]),
        }
        result["scope"] = self.get_scope(principal, world_id, world.scope_id).model_dump(
            mode="json"
        )
        return result

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
            readers = control.execute(
                "SELECT c.* FROM credentials c JOIN scope_grants g ON g.credential_id=c.id "
                "WHERE c.revoked=0 AND c.user_id<>? AND g.scope_id=? "
                "AND g.operation='read' LIMIT 4097",
                (principal.user_id, scope["id"]),
            ).fetchall()
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
            rows = db.execute("SELECT * FROM worlds ORDER BY id").fetchall()
        targets = [self._hydrate(row) for row in rows]
        with self._control() as db:
            scopes = db.execute(
                "SELECT * FROM scopes WHERE kind<>'world-shared' ORDER BY world_id,id"
            ).fetchall()
        worlds = {row["id"]: row for row in rows}
        targets.extend(self._hydrate(worlds[row["world_id"]], row["id"]) for row in scopes)
        return targets

    def pending_jobs(self, world_id: str, scope_id=None) -> list[Episode]:
        with self._world_db(world_id, scope_id) as db:
            return [
                self._episode(row, world_id)
                for row in db.execute(
                    "SELECT * FROM episodes WHERE projection_state='pending' ORDER BY sequence"
                )
            ]

    def mark_processing(self, world_id: str, episode_id: str, scope_id=None) -> Episode | None:
        with self._world_db(world_id, scope_id) as db, self._transaction(db):
            if db.execute("SELECT state FROM projection").fetchone()[0] == "degraded":
                raise Unavailable("World projection requires a rebuild")
            changed = db.execute(
                "UPDATE projection_jobs SET state='processing', attempts=attempts+1, "
                "updated_at=? WHERE episode_id=? AND state='pending'",
                (_now(), episode_id),
            ).rowcount
            if not changed:
                return None
            db.execute(
                "UPDATE episodes SET projection_state='processing' WHERE id=?", (episode_id,)
            )
            row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
            return self._episode(row, world_id)

    def mark_projected(
        self, world_id: str, episode_id: str, generation: str, scope_id=None
    ) -> None:
        with self._world_db(world_id, scope_id) as db, self._transaction(db):
            state = db.execute("SELECT state FROM projection").fetchone()[0]
            if state == "degraded":
                raise Unavailable("World projection requires a rebuild")
            if not db.execute(
                "UPDATE projection_jobs SET state='projected',generation=?,error=NULL,updated_at=? "
                "WHERE episode_id=? AND state='processing'",
                (generation, _now(), episode_id),
            ).rowcount:
                raise Conflict("Projection job is not processing")
            db.execute("UPDATE episodes SET projection_state='projected' WHERE id=?", (episode_id,))
            db.execute("UPDATE projection SET generation=?,state='ready'", (generation,))

    def mark_unknown(self, world_id: str, episode_id: str, error: str, scope_id=None) -> None:
        with self._world_db(world_id, scope_id) as db, self._transaction(db):
            db.execute("UPDATE projection SET state='degraded'")
            db.execute("UPDATE episodes SET projection_state='unknown' WHERE id=?", (episode_id,))
            db.execute(
                "UPDATE projection_jobs SET state='unknown',error=?,updated_at=? "
                "WHERE episode_id=?",
                (error[:2000], _now(), episode_id),
            )

    def set_projection(
        self, world_id: str, generation: str | None, state: str, scope_id=None
    ) -> None:
        if state not in {"disabled", "ready", "degraded"}:
            raise ValueError("Invalid projection state")
        with self._world_db(world_id, scope_id) as db, self._transaction(db):
            db.execute("UPDATE projection SET generation=?,state=?", (generation, state))

    def set_projection_configuration(
        self, world_id: str, configuration: dict, scope_id=None
    ) -> None:
        """Persist public configuration metadata supplied by the trusted adapter."""
        with self._world_db(world_id, scope_id) as db, self._transaction(db):
            db.execute("UPDATE projection SET configuration=?", (_canonical(configuration),))

    def projection_configuration(self, world_id: str, scope_id=None) -> dict:
        with self._world_db(world_id, scope_id) as db:
            return json.loads(db.execute("SELECT configuration FROM projection").fetchone()[0])

    def projection_episodes(
        self, world_id: str, include_unprojected: bool = False, scope_id=None
    ) -> list[Episode]:
        with self._world_db(world_id, scope_id) as db:
            episodes = [
                self._episode(row, world_id)
                for row in db.execute("SELECT * FROM episodes ORDER BY sequence")
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
    ) -> None:
        with self._world_db(world_id, scope_id) as db, self._transaction(db):
            for episode_id in episode_ids:
                row = db.execute("SELECT id FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if row is None:
                    raise NotFound("Rebuild episode not found")
                # Projection consent is not part of immutable source identity. Keep
                # the original payload/digest so an ingestion replay stays stable.
                db.execute(
                    "UPDATE episodes SET projection_state='projected' WHERE id=?", (episode_id,)
                )
                db.execute(
                    "INSERT INTO projection_jobs (episode_id,state,generation,updated_at) "
                    "VALUES (?,'projected',?,?) ON CONFLICT(episode_id) DO UPDATE SET "
                    "state='projected',generation=excluded.generation,error=NULL,"
                    "updated_at=excluded.updated_at",
                    (episode_id, generation, _now()),
                )
            db.execute("UPDATE projection SET generation=?,state='ready'", (generation,))
            if configuration is not None:
                db.execute("UPDATE projection SET configuration=?", (_canonical(configuration),))

    def recover_interrupted_projections(self) -> int:
        recovered = 0
        for world in self.projection_worlds():
            with self._world_db(world.id, world.scope_id) as db, self._transaction(db):
                count = db.execute(
                    "SELECT COUNT(*) FROM episodes WHERE projection_state='processing'"
                ).fetchone()[0]
                if count:
                    db.execute("UPDATE projection SET state='degraded'")
                    db.execute(
                        "UPDATE episodes SET projection_state='unknown' "
                        "WHERE projection_state='processing'"
                    )
                    db.execute(
                        "UPDATE projection_jobs SET state='unknown',error=?,updated_at=? "
                        "WHERE state='processing'",
                        ("Worker interrupted after claiming delivery", _now()),
                    )
                    recovered += count
        return recovered
