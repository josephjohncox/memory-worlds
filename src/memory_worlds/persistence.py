"""Current SQLAlchemy Core storage contract for local and shared authorities.

SQL statements bind values through SQLAlchemy. Tables are explicitly qualified;
there is no SQL text translation, search-path routing, or historical schema reader.
Normal sessions are synchronous transactions and must not span an ``await``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.pool import NullPool

from .errors import Conflict, NotFound, Unavailable
from .errors import MemoryError as InvalidRequest

CONTROL_METADATA = MetaData()
SOURCE_METADATA = MetaData()
OUTBOX_METADATA = MetaData()
Table(
    "metadata",
    CONTROL_METADATA,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)

Table(
    "worlds",
    CONTROL_METADATA,
    Column("id", Text, primary_key=True),
    Column("authority_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("owner_id", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("alias", Text),
)

Table(
    "credentials",
    CONTROL_METADATA,
    Column("id", Text, primary_key=True),
    Column("token_hash", Text, nullable=False),
    Column("user_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("admin", Integer, nullable=False),
    Column("revoked", Integer, nullable=False, server_default=text("0")),
    UniqueConstraint("token_hash"),
)

Table(
    "grants",
    CONTROL_METADATA,
    Column("credential_id", Text, ForeignKey("credentials.id"), primary_key=True),
    Column("world_id", Text, ForeignKey("worlds.id"), primary_key=True),
    Column("role", Text, nullable=False),
)

Table(
    "identities",
    CONTROL_METADATA,
    Column("issuer", Text, primary_key=True),
    Column("subject", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("enabled", Integer, nullable=False, server_default=text("1")),
    Column("private_world_id", Text, ForeignKey("worlds.id"), nullable=False),
    UniqueConstraint("user_id"),
)

Table(
    "identity_sessions",
    CONTROL_METADATA,
    Column("credential_id", Text, ForeignKey("credentials.id"), primary_key=True),
    Column("user_id", Text, ForeignKey("identities.user_id"), nullable=False),
    Column("token_digest", Text, nullable=False),
    Column("expires_at", Float, nullable=False),
    UniqueConstraint("token_digest"),
)

Table(
    "identity_group_state",
    CONTROL_METADATA,
    Column("user_id", Text, ForeignKey("identities.user_id"), primary_key=True),
    Column("claims_iat", Float, nullable=False),
    Column("groups_digest", Text, nullable=False),
    Column("mappings_digest", Text, nullable=False),
)

Table(
    "memberships",
    CONTROL_METADATA,
    Column("user_id", Text, primary_key=True),
    Column("world_id", Text, ForeignKey("worlds.id"), primary_key=True),
    Column("role", Text, nullable=False),
    Column("source", Text, primary_key=True),
    CheckConstraint("role IN ('reader','writer','manager')"),
)

Table(
    "scopes",
    CONTROL_METADATA,
    Column("id", Text, primary_key=True),
    Column("world_id", Text, ForeignKey("worlds.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("creator_credential_id", Text, ForeignKey("credentials.id")),
    Column("run_id", Text),
    Column("agent_id", Text),
    Column("invocation_id", Text),
    Column("parent_invocation_id", Text),
    Column("alias", Text),
)

Table(
    "scope_grants",
    CONTROL_METADATA,
    Column("credential_id", Text, ForeignKey("credentials.id"), primary_key=True),
    Column("scope_id", Text, ForeignKey("scopes.id"), primary_key=True),
    Column("operation", Text, primary_key=True),
)

Table(
    "scope_grant_anchors",
    CONTROL_METADATA,
    Column("credential_id", Text, ForeignKey("credentials.id"), primary_key=True),
    Column("scope_id", Text, ForeignKey("scopes.id"), primary_key=True),
    Column("operation", Text, primary_key=True),
)

Table(
    "credential_contexts",
    CONTROL_METADATA,
    Column("credential_id", Text, ForeignKey("credentials.id"), primary_key=True),
    Column("parent_credential_id", Text, ForeignKey("credentials.id"), nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("agent_uuid", Text, nullable=False),
    Column("invocation_id", Text, nullable=False),
    Column("parent_invocation_id", Text),
    Column("allow_delegation", Integer, nullable=False, server_default=text("0")),
    UniqueConstraint("invocation_id"),
)

Table(
    "agent_contexts",
    CONTROL_METADATA,
    Column("id", Text, primary_key=True),
    Column("credential_id", Text, ForeignKey("credentials.id"), nullable=False),
    Column("invocation_id", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", Text, nullable=False),
)

Table(
    "episodes",
    SOURCE_METADATA,
    Column("sequence", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
    Column("id", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("digest", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("received_at", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("projection_state", Text, nullable=False),
    Column("agent_uuid", Text),
    Column("invocation_id", Text),
    Column("parent_invocation_id", Text),
    UniqueConstraint("source_id"),
    UniqueConstraint("id"),
)

Table(
    "projection",
    SOURCE_METADATA,
    Column("singleton", Integer, primary_key=True),
    Column("generation", Text),
    Column("state", Text, nullable=False),
    Column("configuration", Text, nullable=False, server_default=text("'{}'")),
    CheckConstraint("singleton=1"),
)

Table(
    "projection_jobs",
    SOURCE_METADATA,
    Column("episode_id", Text, ForeignKey("episodes.id"), primary_key=True),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("generation", Text),
    Column("error", Text),
    Column("updated_at", Text, nullable=False),
)

Table(
    "audit",
    SOURCE_METADATA,
    Column("sequence", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
    Column("event", Text, nullable=False),
    Column("episode_id", Text, nullable=False),
    Column("actor_id", Text, nullable=False),
    Column("occurred_at", Text, nullable=False),
)

Table(
    "deliveries",
    OUTBOX_METADATA,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("credential_id", Text, nullable=False),
    Column("principal", Text, nullable=False),
    Column("mount_id", Text, nullable=False),
    Column("authority_id", Text, nullable=False),
    Column("endpoint", Text, nullable=False),
    Column("world_id", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("digest", Text, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("next_attempt", Float, nullable=False, server_default=text("0")),
    Column("created_at", Text, nullable=False),
    Column("delivered_at", Text),
    Column("error", Text),
    Column("remote_episode_id", Text),
    Column("pruned", Integer, nullable=False, server_default=text("0")),
    UniqueConstraint("user_id", "mount_id", "world_id", "source_id"),
)


Index(
    "world_shared_scope",
    CONTROL_METADATA.tables["scopes"].c.world_id,
    unique=True,
    sqlite_where=text("kind='world-shared'"),
    postgresql_where=text("kind='world-shared'"),
)
Table(
    "worker_leases",
    CONTROL_METADATA,
    Column("resource", Text, primary_key=True),
    Column("owner", Text),
    Column("fence", BigInteger, nullable=False, server_default=text("0")),
    Column("expires_at", Float, nullable=False, server_default=text("0")),
)
Table(
    "projection_generations",
    CONTROL_METADATA,
    Column("world_id", Text, primary_key=True),
    Column("scope_id", Text, primary_key=True),
    Column("generation", Text, primary_key=True),
    Column("state", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("retired_at", Text),
    Column("lease_fence", BigInteger, nullable=False, server_default=text("0")),
)
for _metadata, _name in ((SOURCE_METADATA, "projection_jobs"), (OUTBOX_METADATA, "deliveries")):
    _metadata.tables[_name].append_column(Column("lease_owner", Text))
    _metadata.tables[_name].append_column(Column("lease_fence", BigInteger))
    _metadata.tables[_name].append_column(Column("lease_expires_at", Float))


@dataclass(frozen=True)
class WorkerLease:
    resource: str
    owner: str
    fence: int
    expires_at: float


class LeaseLost(Unavailable):
    """A stale worker cannot commit effects or activate its graph generation."""


class _Barrier:
    def __init__(self):
        self.condition = threading.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @contextmanager
    def hold(self, exclusive: bool):
        with self.condition:
            if exclusive:
                self.waiting_writers += 1
                try:
                    self.condition.wait_for(lambda: not self.writer and not self.readers)
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
            else:
                if self.writer or self.waiting_writers:
                    raise Unavailable("Authority maintenance is in progress")
                self.readers += 1
        try:
            yield
        finally:
            with self.condition:
                if exclusive:
                    self.writer = False
                else:
                    self.readers -= 1
                self.condition.notify_all()


_BARRIERS: dict[str, _Barrier] = {}
_BARRIER_LOCK = threading.Lock()
_GUARDS: ContextVar[dict[str, str]] = ContextVar("memory_worlds_guards", default={})


class Persistence:
    """One current authority, with an explicitly selected durable backend."""

    def __init__(
        self, data_dir: Path, database_url: str | None = None, authority_id: str | None = None
    ):
        self.data_dir = Path(data_dir).expanduser().absolute()
        self.is_postgres = database_url is not None
        self.kind = "postgresql" if self.is_postgres else "sqlite"
        self.authority_id = UUID(str(authority_id)).hex if authority_id else uuid4().hex
        if self.is_postgres and authority_id is None:
            raise InvalidRequest("PostgreSQL storage requires an explicit authority UUID")
        self._engines: dict[str, Engine] = {}
        self._tables: dict[tuple[str, str], dict[str, Table]] = {}
        self._engine_lock = threading.RLock()
        self._sessions: ContextVar[dict[str, Connection]] = ContextVar(
            f"memory_worlds_sessions_{id(self)}", default={}
        )
        self._session_kind: ContextVar[tuple[str, str | None]] = ContextVar(
            f"memory_worlds_session_kind_{id(self)}", default=("control", None)
        )
        if self.is_postgres:
            from .postgres import postgres_engine

            self._engine = postgres_engine(database_url)
            self._guard_engine = postgres_engine(database_url, pooled=False)
            self._guard_slots = threading.BoundedSemaphore(16)
            # A restored authority may use the same UUID in a different database.
            # Barrier nesting must identify the database as well as the authority.
            url = self._engine.url
            identity = (url.host, url.port or 5432, url.database, url.query.get("host"))
            fingerprint = hashlib.sha256(repr(identity).encode()).hexdigest()[:32]
            self._barrier_key = f"postgres:{fingerprint}:{self.authority_id}"
        else:
            self.directory(self.data_dir)
            self.directory(self.data_dir / "worlds")
            self._engine = None
            self._guard_engine = None
            self._barrier_key = f"sqlite:{os.getpid()}:{self.data_dir.resolve()}"
        with _BARRIER_LOCK:
            self._barrier = _BARRIERS.setdefault(self._barrier_key, _Barrier())

    @staticmethod
    def directory(path: Path) -> None:
        if path.is_symlink():
            raise Conflict("Memory storage directories cannot be symbolic links")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def schema(self, kind: str, scope_id: str | None = None) -> str | None:
        if not self.is_postgres:
            return None
        if kind == "source":
            if scope_id is None:
                raise InvalidRequest("A source session requires its canonical scope UUID")
            value = hashlib.sha256(f"{self.authority_id}:{scope_id}".encode()).hexdigest()[:48]
            return f"mw_s_{value}"
        return f"mw_{'a' if kind == 'control' else 'o'}_{self.authority_id}"

    def table(self, db: Connection, name: str) -> Table:
        groups = {"control": CONTROL_METADATA, "source": SOURCE_METADATA, "outbox": OUTBOX_METADATA}
        selected = [kind for kind, metadata in groups.items() if name in metadata.tables]
        if len(selected) != 1:
            raise InvalidRequest("Unknown or ambiguous current storage table")
        kind = selected[0]
        active_kind, scope_id = (
            self._session_kind.get() if self.is_postgres else db.info["memory_worlds_resource"]
        )
        if not self.is_postgres and active_kind != kind:
            raise InvalidRequest("Table does not belong to the selected SQLite journal")
        schema = self.schema(kind, scope_id if kind == "source" else None)
        key = (kind, schema or "")
        templates = groups[kind]
        with self._engine_lock:
            existing = self._tables.get(key)
            if existing is None or set(existing) != set(templates.tables):
                metadata = MetaData(schema=schema)
                existing = {
                    name: table.to_metadata(metadata, schema=schema)
                    for name, table in templates.tables.items()
                }
                self._tables[key] = existing
        return existing[name]

    def source_path(self, world_id: str, scope_id: str | UUID | None = None) -> Path:
        if self.is_postgres:
            raise InvalidRequest("PostgreSQL journals do not have SQLite file paths")
        from .scopes import scope_uuid, world_shared_scope_id

        path = self.data_dir / "worlds" / world_id
        if scope_id is not None:
            sid = scope_uuid(scope_id)
            if sid != world_shared_scope_id(self.authority_id, world_id):
                path = path / "scopes" / sid
        return path / "source.sqlite3"

    def _sqlite_engine(self, path: Path) -> Engine:
        for component in (path, *path.parents):
            if component.is_symlink():
                raise Conflict("Memory database paths cannot contain symbolic links")
            if component == self.data_dir:
                break
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise NotFound("Memory storage not found")
        with self._engine_lock:
            key = str(path)
            if key not in self._engines:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
                path.chmod(0o600)
                engine = create_engine(
                    f"sqlite+pysqlite:///{path}",
                    poolclass=NullPool,
                    hide_parameters=True,
                    connect_args={"check_same_thread": False, "timeout": 15},
                )

                @event.listens_for(engine, "connect")
                def configure(connection, _):
                    connection.isolation_level = None
                    cursor = connection.cursor()
                    cursor.execute("PRAGMA busy_timeout=15000")
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=FULL")
                    cursor.execute("PRAGMA secure_delete=ON")
                    cursor.close()

                @event.listens_for(engine, "begin")
                def begin(connection):
                    connection.exec_driver_sql("BEGIN IMMEDIATE")

                self._engines[key] = engine
            return self._engines[key]

    @contextmanager
    def _session_guard(self, connection: Connection, guarded: bool):
        if not guarded:
            yield
            return
        if not self.is_postgres:
            with self.operation_guard():
                yield
            return
        active = _GUARDS.get()
        if active.get(self._barrier_key):
            yield
            return
        from .postgres import advisory_key

        # A normal SQL session needs only its own pooled connection. Its
        # transaction-level lock conflicts with dedicated maintenance locks.
        available = connection.execute(
            text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
            {"key": advisory_key(self.authority_id, "authority-maintenance")},
        ).scalar_one()
        if not available:
            raise Unavailable("Authority maintenance is in progress")
        marker = _GUARDS.set({**active, self._barrier_key: "shared"})
        try:
            yield
        finally:
            _GUARDS.reset(marker)

    @contextmanager
    def _session(
        self,
        kind: str,
        world_id: str | None = None,
        scope_id: str | UUID | None = None,
        *,
        guarded: bool = True,
    ) -> Iterator[Connection]:
        from .scopes import scope_uuid, world_shared_scope_id

        sid = (
            (
                scope_uuid(scope_id)
                if scope_id is not None
                else world_shared_scope_id(self.authority_id, world_id)
            )
            if kind == "source"
            else None
        )
        key = "postgres" if self.is_postgres else f"{kind}:{world_id}:{sid}"
        active = self._sessions.get()
        marker = self._session_kind.set((kind, sid))
        try:
            if key in active:
                with self._session_guard(active[key], guarded):
                    yield active[key]
                return
            if self.is_postgres:
                engine = self._engine
            else:
                path = (
                    self.source_path(world_id, sid)
                    if kind == "source"
                    else (
                        self.data_dir
                        / ("control.sqlite3" if kind == "control" else "router.sqlite3")
                    )
                )
                engine = self._sqlite_engine(path)
            local_guard = (
                self.operation_guard() if guarded and not self.is_postgres else nullcontext()
            )
            with local_guard, engine.begin() as connection:
                if not self.is_postgres:
                    connection.info["memory_worlds_resource"] = (kind, sid)
                with self._session_guard(connection, guarded):
                    token = self._sessions.set({**active, key: connection})
                    try:
                        yield connection
                    finally:
                        self._sessions.reset(token)
        except (PoolTimeout, OperationalError) as error:
            raise Unavailable("Durable storage is unavailable") from error
        finally:
            self._session_kind.reset(marker)

    def control(self):
        return self._session("control")

    def source(self, world_id: str, scope_id=None):
        return self._session("source", world_id, scope_id)

    def outbox(self):
        return self._session("outbox")

    @contextmanager
    def transaction(self, db: Connection):
        if self.is_postgres:
            from .postgres import advisory_key

            kind, sid = self._session_kind.get()
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": advisory_key(self.authority_id, f"transaction:{kind}:{sid}")},
            )
        with db.begin_nested() if db.in_transaction() else db.begin():
            yield

    def _create(self, db: Connection, kind: str, scope_id=None):
        from sqlalchemy.schema import CreateSchema

        schema = self.schema(kind, scope_id)
        if schema:
            db.execute(CreateSchema(schema, if_not_exists=True))
        templates = {
            "control": CONTROL_METADATA,
            "source": SOURCE_METADATA,
            "outbox": OUTBOX_METADATA,
        }[kind]
        tables = [self.table(db, name) for name in templates.tables]
        if tables:
            tables[0].metadata.create_all(db)

    def initialize(self):
        with self.control() as db, self.transaction(db):
            self._create(db, "control")
            table = self.table(db, "metadata")
            self.insert_ignore(
                db, table, {"key": "authority_id", "value": self.authority_id}, ["key"]
            )
            stored = db.execute(
                select(table.c.value).where(table.c.key == "authority_id")
            ).scalar_one()
            if self.is_postgres and stored != self.authority_id:
                raise Conflict("PostgreSQL authority identity does not match its configured schema")
            self.authority_id = stored
        with self.outbox() as db, self.transaction(db):
            self._create(db, "outbox")

    def ensure_control_tables(self):
        with self.control() as db, self.transaction(db):
            self._create(db, "control")

    def initialize_source(self, world_id: str, scope_id=None):
        from .scopes import scope_uuid, world_shared_scope_id

        sid = (
            scope_uuid(scope_id) if scope_id else world_shared_scope_id(self.authority_id, world_id)
        )
        if not self.is_postgres:
            world_directory = self.data_dir / "worlds" / world_id
            self.directory(world_directory)
            target = self.source_path(world_id, sid).parent
            if target != world_directory:
                self.directory(world_directory / "scopes")
                self.directory(target)
        with self.source(world_id, sid) as db, self.transaction(db):
            self._create(db, "source", sid)
            self.insert_ignore(
                db,
                self.table(db, "projection"),
                {"singleton": 1, "generation": None, "state": "disabled"},
                ["singleton"],
            )

    @staticmethod
    def _ttl(ttl: float) -> float:
        if isinstance(ttl, bool) or not isinstance(ttl, (float, int)) or not 1 <= ttl <= 300:
            raise InvalidRequest("Worker lease TTL must be between one and 300 seconds")
        return float(ttl)

    def database_time(self, db: Connection) -> float:
        value = (
            func.extract("epoch", func.clock_timestamp())
            if self.is_postgres
            else (func.julianday("now") - 2440587.5) * 86400
        )
        return float(db.execute(select(value)).scalar_one())

    def acquire_lease(
        self, resource: str, owner: str | None = None, ttl: float = 30
    ) -> WorkerLease | None:
        ttl = self._ttl(ttl)
        owner = owner or uuid4().hex
        if not isinstance(resource, str) or not 1 <= len(resource) <= 500:
            raise InvalidRequest("Invalid worker lease resource")
        if not isinstance(owner, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", owner):
            raise InvalidRequest("Invalid worker lease owner")
        # Heartbeats and ownership bookkeeping do not mutate source/graph data;
        # they must remain available while a maintenance writer is waiting.
        with self._session("control") as db, self.transaction(db):
            table = self.table(db, "worker_leases")
            self.insert_ignore(db, table, {"resource": resource}, ["resource"])
            row = (
                db.execute(select(table).where(table.c.resource == resource).with_for_update())
                .mappings()
                .one()
            )
            now = self.database_time(db)
            if row["owner"] is not None and row["expires_at"] > now:
                return None
            lease = WorkerLease(resource, owner, row["fence"] + 1, now + ttl)
            db.execute(
                table.update()
                .where(table.c.resource == resource)
                .values(owner=lease.owner, fence=lease.fence, expires_at=lease.expires_at)
            )
            return lease

    def _assert_lease(self, db: Connection, lease: WorkerLease):
        table = self.table(db, "worker_leases")
        row = (
            db.execute(select(table).where(table.c.resource == lease.resource).with_for_update())
            .mappings()
            .fetchone()
        )
        if (
            row is None
            or row["owner"] != lease.owner
            or row["fence"] != lease.fence
            or row["expires_at"] <= self.database_time(db)
        ):
            raise LeaseLost("Worker lease is no longer valid")
        return row

    @contextmanager
    def fenced(self, lease: WorkerLease):
        """Guard a short durable mutation; never hold this context across network I/O."""
        with self._session("control", guarded=False) as db, self.transaction(db):
            self._assert_lease(db, lease)
            yield db

    def assert_lease(self, lease: WorkerLease):
        with self.fenced(lease):
            return None

    def renew_lease(self, lease: WorkerLease, ttl: float = 30) -> WorkerLease:
        ttl = self._ttl(ttl)
        with self.fenced(lease) as db:
            expiry = self.database_time(db) + ttl
            table = self.table(db, "worker_leases")
            db.execute(
                table.update().where(table.c.resource == lease.resource).values(expires_at=expiry)
            )
            return WorkerLease(lease.resource, lease.owner, lease.fence, expiry)

    def release_lease(self, lease: WorkerLease) -> bool:
        with self._session("control", guarded=False) as db, self.transaction(db):
            table = self.table(db, "worker_leases")
            return bool(
                db.execute(
                    table.update()
                    .where(
                        table.c.resource == lease.resource,
                        table.c.owner == lease.owner,
                        table.c.fence == lease.fence,
                    )
                    .values(owner=None, expires_at=0)
                ).rowcount
            )

    @asynccontextmanager
    async def maintain_lease(self, lease: WorkerLease, ttl: float = 30):
        """Renew ownership independently of network calls; fence every durable completion."""
        ttl = self._ttl(ttl)
        failures = []

        async def heartbeat():
            try:
                while True:
                    await asyncio.sleep(ttl / 3)
                    await asyncio.to_thread(self.renew_lease, lease, ttl)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures.append(error)

        def assert_live():
            if failures:
                raise LeaseLost("Worker ownership could not be renewed") from failures[0]
            self.assert_lease(lease)

        assert_live()
        task = asyncio.create_task(heartbeat())
        try:
            yield assert_live
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def checkpoint_source(self, world_id: str, scope_id=None):
        """Truncate the local source WAL only while exclusive maintenance drains readers."""
        if self.is_postgres:
            return
        if _GUARDS.get().get(self._barrier_key) != "exclusive":
            raise Conflict("Source checkpoint requires exclusive authority maintenance")
        engine = self._sqlite_engine(self.source_path(world_id, scope_id))
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                busy, _, _ = cursor.fetchone()
                if busy:
                    raise Unavailable("Source checkpoint is waiting for an external reader")
            finally:
                cursor.close()
        finally:
            connection.close()

    def reset_sequences(self, db: Connection, table: Table):
        """Advance a restored current source table's native sequence, without guessing names."""
        if not self.is_postgres or "sequence" not in table.c:
            return
        sequence = db.execute(select(func.pg_get_serial_sequence(str(table), "sequence")))
        sequence = sequence.scalar_one_or_none()
        if sequence is None:
            return
        maximum = db.execute(select(func.max(table.c.sequence))).scalar_one()
        db.execute(
            text("SELECT setval(CAST(:sequence AS regclass), :value, :used)"),
            {
                "sequence": sequence,
                "value": maximum if maximum is not None else 1,
                "used": maximum is not None,
            },
        )

    @staticmethod
    def insert_ignore(db: Connection, table: Table, values: dict, columns: list[str]):
        if db.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        return db.execute(
            insert(table).values(**values).on_conflict_do_nothing(index_elements=columns)
        )

    @contextmanager
    def _sqlite_file_guard(self, exclusive: bool):
        """Coordinate all local authority processes on one persistent inode."""
        import fcntl

        path = self.data_dir / ".authority-maintenance.lock"
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600,
            )
        except OSError:
            raise Conflict("Authority maintenance requires a private regular lock file") from None
        locked = False
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise Conflict("Authority maintenance requires a private regular lock file")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(
                    descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH | fcntl.LOCK_NB
                )
            except BlockingIOError:
                raise Unavailable("Authority maintenance is in progress") from None
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _backend_guard(self, exclusive: bool):
        if not self.is_postgres:
            with self._barrier.hold(exclusive), self._sqlite_file_guard(exclusive):
                yield
            return
        from .postgres import advisory_key

        # Long-lived graph guards use their own connections and a nonblocking
        # concurrency limit; they cannot exhaust the SQL session pool or wait
        # synchronously for another graph operation on the same event loop.
        slot = False
        if not exclusive:
            slot = self._guard_slots.acquire(blocking=False)
            if not slot:
                raise Unavailable("Authority graph operation capacity is busy")
        try:
            key = advisory_key(self.authority_id, "authority-maintenance")
            unlock = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
            with self._guard_engine.connect() as connection:
                if exclusive:
                    connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                elif not connection.execute(
                    text("SELECT pg_try_advisory_lock_shared(:key)"), {"key": key}
                ).scalar_one():
                    raise Unavailable("Authority maintenance is in progress")
                connection.commit()
                try:
                    yield
                finally:
                    try:
                        connection.execute(text(f"SELECT {unlock}(:key)"), {"key": key})
                        connection.commit()
                    except BaseException:
                        connection.invalidate()
                        raise
        except OperationalError as error:
            raise Unavailable("Durable storage is unavailable") from error
        finally:
            if slot:
                self._guard_slots.release()

    @contextmanager
    def _guard(self, exclusive: bool):
        active = _GUARDS.get()
        held = active.get(self._barrier_key)
        if held:
            if exclusive and held != "exclusive":
                raise Conflict("Cannot upgrade an active operation to authority maintenance")
            yield
            return
        marker = _GUARDS.set({**active, self._barrier_key: "exclusive" if exclusive else "shared"})
        try:
            with self._backend_guard(exclusive):
                yield
        finally:
            _GUARDS.reset(marker)

    @asynccontextmanager
    async def async_maintenance_guard(self):
        """Drain operations without blocking their event loop, including cancellation cleanup."""
        active = _GUARDS.get()
        held = active.get(self._barrier_key)
        if held:
            if held != "exclusive":
                raise Conflict("Cannot upgrade an active operation to authority maintenance")
            yield
            return
        marker = _GUARDS.set({**active, self._barrier_key: "exclusive"})
        guard = self._backend_guard(True)
        acquiring = asyncio.create_task(asyncio.to_thread(guard.__enter__))
        entered = False
        try:
            try:
                await asyncio.shield(acquiring)
                entered = True
            except asyncio.CancelledError:
                # The lock waiter continues in its thread; reclaim it before
                # propagating cancellation, rather than leak an exclusive lock.
                await acquiring
                entered = True
                raise
            yield
        finally:
            try:
                if entered:
                    await asyncio.shield(asyncio.to_thread(guard.__exit__, None, None, None))
            finally:
                _GUARDS.reset(marker)

    def operation_guard(self):
        return self._guard(False)

    def maintenance_guard(self):
        return self._guard(True)

    def close(self):
        for engine in self._engines.values():
            engine.dispose()
        if self._engine is not None:
            self._engine.dispose()
        if self._guard_engine is not None:
            self._guard_engine.dispose()
