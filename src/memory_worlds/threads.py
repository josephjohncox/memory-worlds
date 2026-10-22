"""Resumable thread-private journals using the authority's live scope grants.

Creation reserves exact identities before initializing a separate source journal.
No thread is discoverable until its scope, grants and first revision commit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .errors import Conflict, Forbidden, NotFound
from .errors import MemoryError as InvalidRequest
from .models import Principal, ScopeKind
from .scopes import ALL_OPERATIONS
from .thread_models import AppendThread, CreateThread, Thread, ThreadRevision


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _revision(thread_id, revision, sources, created_at):
    content = {
        "thread_id": str(thread_id),
        "revision": revision,
        "sources": [source.model_dump(mode="json") for source in sources],
    }
    return ThreadRevision(**content, digest=_digest(content), created_at=created_at)


class ThreadService:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _id(value):
        try:
            return str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            raise NotFound("Thread not found") from None

    def _now(self, db):
        return datetime.fromtimestamp(self.store.persistence.database_time(db), UTC)

    def _thread(self, row, now):
        thread = Thread.model_validate_json(row["payload"])
        if thread.expires_at is not None and thread.expires_at <= now:
            if thread.state != "retracted":
                thread = thread.model_copy(update={"state": "expired"})
        return thread

    def _row(self, db, principal, thread_id, operation="read"):
        self.store._principal(db, principal)
        table = self.store._table(db, "memory_threads")
        row = db.execute(select(table).where(table.c.id == self._id(thread_id))).mappings().first()
        if row is None:
            raise NotFound("Thread not found")
        scope = self.store._scope_authorized(
            db, principal, row["world_id"], row["scope_id"], operation
        )
        if scope["kind"] != ScopeKind.THREAD_PRIVATE or scope["thread_id"] != row["id"]:
            raise Conflict("Thread scope registration is inconsistent")
        return row

    def _operation(self, db, principal, operation_id, kind, digest):
        table = self.store._table(db, "thread_operations")
        row = db.execute(select(table).where(table.c.id == str(operation_id))).mappings().first()
        if row is not None and (
            row["credential_id"] != principal.credential_id
            or row["kind"] != kind
            or row["request_digest"] != digest
        ):
            raise Conflict("Thread operation identity already has different input or authority")
        return row

    def create(self, principal: Principal, request: CreateThread) -> Thread:
        request = CreateThread.model_validate(request)
        digest = _digest(request.model_dump(mode="json"))
        try:
            with self.store._control() as db, self.store._transaction(db):
                self.store._authorized(db, principal, request.world_id, write=True)
                if principal.restricted:
                    raise Forbidden("Delegated invocations cannot create independent threads")
                old = self._operation(db, principal, request.operation_id, "create", digest)
                if old is not None:
                    prepared = Thread.model_validate_json(old["payload"])
                    if old["state"] == "applied":
                        self._row(db, principal, prepared.id, "write")
                        return self._thread({"payload": old["receipt"]}, self._now(db))
                else:
                    now = self._now(db)
                    if request.expires_at is not None and request.expires_at <= now:
                        raise InvalidRequest("New thread expiry must be in the future")
                    self.store._validate_run(db, principal, request.run_id)
                    if request.agent_id is not None:
                        from .identity_names import IdentityRegistry

                        IdentityRegistry(self.store).get(principal, "agent", request.agent_id)
                    thread_id = request.thread_id or uuid5(
                        NAMESPACE_URL,
                        f"memory-worlds:{self.store.authority_id}:thread:"
                        f"{principal.credential_id}:{request.operation_id}",
                    )
                    scope_id = uuid5(thread_id, "thread-private-scope")
                    prepared = Thread(
                        id=thread_id,
                        authority_id=self.store.authority_id,
                        world_id=request.world_id,
                        scope_id=scope_id,
                        owner_id=principal.user_id,
                        creator_credential_id=principal.credential_id,
                        name=request.name,
                        alias=request.alias,
                        agent_id=request.agent_id,
                        run_id=request.run_id,
                        revision=0,
                        head_digest=_revision(thread_id, 0, [], now).digest,
                        created_at=now,
                        expires_at=request.expires_at,
                        state="open",
                    )
                    threads = self.store._table(db, "memory_threads")
                    if db.execute(
                        select(threads.c.id).where(threads.c.id == str(thread_id))
                    ).first():
                        raise Conflict("Thread identity is already registered")
                    operations = self.store._table(db, "thread_operations")
                    db.execute(
                        operations.insert().values(
                            id=str(request.operation_id),
                            credential_id=principal.credential_id,
                            kind="create",
                            request_digest=digest,
                            payload=prepared.model_dump_json(),
                            state="prepared",
                        )
                    )
            # Idempotent initialization can leave an unregistered journal after a
            # crash. Its frozen scope UUID is reused on the next exact request.
            self.store._initialize_world(prepared.world_id, prepared.scope_id)
            with self.store._control() as db, self.store._transaction(db):
                self.store._authorized(db, principal, prepared.world_id, write=True)
                old = self._operation(db, principal, request.operation_id, "create", digest)
                if old["state"] == "applied":
                    self._row(db, principal, prepared.id, "write")
                    return self._thread({"payload": old["receipt"]}, self._now(db))
                scopes = self.store._table(db, "scopes")
                db.execute(
                    scopes.insert().values(
                        id=str(prepared.scope_id),
                        world_id=prepared.world_id,
                        kind=ScopeKind.THREAD_PRIVATE.value,
                        name=prepared.name,
                        created_at=prepared.created_at.isoformat(),
                        creator_credential_id=principal.credential_id,
                        run_id=str(prepared.run_id) if prepared.run_id else None,
                        agent_id=str(prepared.agent_id) if prepared.agent_id else None,
                        thread_id=str(prepared.id),
                    )
                )
                grants = self.store._table(db, "scope_grants")
                for operation in ALL_OPERATIONS:
                    db.execute(
                        grants.insert().values(
                            credential_id=principal.credential_id,
                            scope_id=str(prepared.scope_id),
                            operation=operation,
                        )
                    )
                threads = self.store._table(db, "memory_threads")
                db.execute(
                    threads.insert().values(
                        id=str(prepared.id),
                        world_id=prepared.world_id,
                        scope_id=str(prepared.scope_id),
                        owner_id=prepared.owner_id,
                        creator_credential_id=principal.credential_id,
                        alias=prepared.alias,
                        revision=0,
                        payload=prepared.model_dump_json(),
                    )
                )
                revisions = self.store._table(db, "thread_revisions")
                db.execute(
                    revisions.insert().values(
                        thread_id=str(prepared.id),
                        revision=0,
                        payload=_revision(
                            prepared.id, 0, [], prepared.created_at
                        ).model_dump_json(),
                    )
                )
                operations = self.store._table(db, "thread_operations")
                db.execute(
                    operations.update()
                    .where(operations.c.id == str(request.operation_id))
                    .values(state="applied", receipt=prepared.model_dump_json())
                )
                return self._thread({"payload": prepared.model_dump_json()}, self._now(db))
        except IntegrityError:
            raise Conflict("Thread identity or alias is already registered") from None

    def get(self, principal: Principal, thread_id: UUID) -> Thread:
        with self.store._control() as db:
            return self._thread(self._row(db, principal, thread_id), self._now(db))

    def list(self, principal: Principal, world_id=None, *, include_expired=False) -> list[Thread]:
        with self.store._control() as db:
            self.store._principal(db, principal)
            table = self.store._table(db, "memory_threads")
            grants = self.store._table(db, "scope_grants")
            query = (
                select(table)
                .join(grants, grants.c.scope_id == table.c.scope_id)
                .where(
                    grants.c.credential_id == principal.credential_id,
                    grants.c.operation == "read",
                )
                .order_by(table.c.id)
            )
            if world_id is not None:
                self.store._authorized(db, principal, world_id)
                query = query.where(table.c.world_id == world_id)
            visible = []
            now = self._now(db)
            for row in db.execute(query).mappings():
                try:
                    self.store._scope_authorized(db, principal, row["world_id"], row["scope_id"])
                except (Forbidden, NotFound):
                    continue
                thread = self._thread(row, now)
                if include_expired or thread.state not in {"expired", "retracted"}:
                    visible.append(thread)
                if len(visible) == 1000:
                    break
            return visible

    def _sources_live(self, principal, thread, sources):
        for source in sources:
            try:
                episode = self.store.get_episode(
                    principal, thread.world_id, source.episode_id, scope_id=thread.scope_id
                )
            except NotFound:
                raise Conflict("Thread source is no longer available in its scope") from None
            if episode.digest != source.digest:
                raise Conflict("Thread source digest does not match its immutable episode")

    def _snapshot(self, db, principal, row, revision):
        thread = Thread.model_validate_json(row["payload"])
        if revision is None:
            revision = thread.revision
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise InvalidRequest("Thread revision must be a nonnegative integer")
        table = self.store._table(db, "thread_revisions")
        result = db.execute(
            select(table.c.payload).where(
                table.c.thread_id == str(thread.id), table.c.revision == revision
            )
        ).scalar()
        if result is None:
            raise NotFound("Thread revision not found")
        snapshot = ThreadRevision.model_validate_json(result)
        self._sources_live(principal, thread, snapshot.sources)
        return snapshot

    def snapshot(self, principal, thread_id, revision=None) -> ThreadRevision:
        with self.store._control() as db:
            row = self._row(db, principal, thread_id)
            return self._snapshot(db, principal, row, revision)

    def append(
        self, principal, thread_id, operation_id, expected_revision, sources
    ) -> ThreadRevision:
        request = AppendThread(
            operation_id=operation_id, expected_revision=expected_revision, sources=sources
        )
        canonical = {"thread_id": self._id(thread_id), **request.model_dump(mode="json")}
        digest = _digest(canonical)
        with self.store._control() as db, self.store._transaction(db):
            row = self._row(db, principal, thread_id, "write")
            thread = self._thread(row, self._now(db))
            old = self._operation(db, principal, request.operation_id, "append", digest)
            if old is not None:
                result = ThreadRevision.model_validate_json(old["receipt"])
                self._sources_live(principal, thread, result.sources)
                return result
            if thread.state != "open":
                raise Conflict("Only an open, unexpired thread accepts new sources")
            if thread.revision != request.expected_revision:
                raise Conflict("Thread head changed; select its current revision explicitly")
            previous = self._snapshot(db, principal, row, thread.revision)
            self._sources_live(principal, thread, request.sources)
            combined = [*previous.sources, *request.sources]
            if len({source.episode_id for source in combined}) != len(combined):
                raise Conflict("Thread source membership must be unique")
            if len(combined) > 1000:
                raise InvalidRequest("Thread revision exceeds the 1000 source limit")
            result = _revision(thread.id, thread.revision + 1, combined, self._now(db))
            threads = self.store._table(db, "memory_threads")
            updated = thread.model_copy(
                update={"revision": result.revision, "head_digest": result.digest}
            )
            changed = db.execute(
                threads.update()
                .where(threads.c.id == str(thread.id), threads.c.revision == thread.revision)
                .values(revision=result.revision, payload=updated.model_dump_json())
            ).rowcount
            if changed != 1:
                raise Conflict("Thread head changed during append")
            revisions = self.store._table(db, "thread_revisions")
            db.execute(
                revisions.insert().values(
                    thread_id=str(thread.id),
                    revision=result.revision,
                    payload=result.model_dump_json(),
                )
            )
            operations = self.store._table(db, "thread_operations")
            db.execute(
                operations.insert().values(
                    id=str(request.operation_id),
                    credential_id=principal.credential_id,
                    kind="append",
                    request_digest=digest,
                    payload=_json(canonical),
                    state="applied",
                    receipt=result.model_dump_json(),
                )
            )
            return result
