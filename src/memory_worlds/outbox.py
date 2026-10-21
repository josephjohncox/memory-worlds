"""Durable publication envelopes, with a receipt independent from remote delivery."""

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field
from sqlalchemy import and_, or_, select

from .errors import Conflict, Forbidden, NotFound
from .models import Identifier, Model, Principal, Publication
from .persistence import LeaseLost, Persistence, WorkerLease


class RoutedPublication(Publication):
    destination_mount_id: Identifier
    world_id: Identifier


class Delivery(Model):
    id: str
    destination_mount_id: str
    destination_authority_id: str
    world_id: str
    source_id: str
    user_id: str
    agent_id: str
    state: Literal["pending", "delivered", "blocked"]
    attempts: int
    created_at: AwareDatetime
    delivered_at: AwareDatetime | None = None
    error: str | None = None
    remote_episode_id: str | None = None


class RouterSearch(Model):
    mount_ids: list[Identifier] = Field(min_length=1, max_length=20)
    query: str = Field(min_length=1, max_length=2000)
    mode: Literal["source", "graph"] = "graph"
    limit: int = Field(default=10, ge=1, le=100)


class PublicationOutbox:
    def __init__(
        self, data_dir: Path, authority_id: str, *, persistence: Persistence | None = None
    ):
        self.path = Path(data_dir) / "router.sqlite3"
        self.authority_id = authority_id
        self.persistence = persistence or Persistence(data_dir, authority_id=authority_id)
        if persistence is None:
            self.persistence.initialize()
        self.worker_id = uuid4().hex

    @contextmanager
    def db(self):
        with self.persistence.outbox() as db:
            yield db

    def _table(self, db):
        return self.persistence.table(db, "deliveries")

    @staticmethod
    def view(row):
        return Delivery(
            id=row["id"],
            destination_mount_id=row["mount_id"],
            destination_authority_id=row["authority_id"],
            world_id=row["world_id"],
            source_id=row["source_id"],
            user_id=row["user_id"],
            agent_id=row["agent_id"],
            state="pending" if row["state"] == "processing" else row["state"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            delivered_at=row["delivered_at"],
            error=row["error"],
            remote_episode_id=row["remote_episode_id"],
        )

    def enqueue(self, principal: Principal, mount, request: RoutedPublication) -> Delivery:
        payload = Publication.model_validate(
            request.model_dump(exclude={"destination_mount_id", "world_id"})
        ).model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.db() as db, self.persistence.transaction(db):
            table = self._table(db)
            old = (
                db.execute(
                    select(table).where(
                        table.c.user_id == principal.user_id,
                        table.c.mount_id == mount.id,
                        table.c.world_id == request.world_id,
                        table.c.source_id == request.source_id,
                    )
                )
                .mappings()
                .fetchone()
            )
            if old:
                if (
                    old["digest"] != digest
                    or old["endpoint"] != mount.url
                    or old["authority_id"] != mount.authority_id
                ):
                    raise Conflict("publication identity already exists with different input")
                return self.view(old)
            delivery_id = uuid4().hex
            payload["source_id"] = f"publication:{self.authority_id}:{delivery_id}"
            values = dict(
                id=delivery_id,
                user_id=principal.user_id,
                agent_id=principal.agent_id,
                credential_id=principal.credential_id,
                principal=principal.model_dump_json(),
                mount_id=mount.id,
                authority_id=mount.authority_id,
                endpoint=mount.url,
                world_id=request.world_id,
                source_id=request.source_id,
                payload=json.dumps(payload),
                digest=digest,
                state="pending",
                created_at=datetime.now(UTC).isoformat(),
            )
            db.execute(table.insert().values(**values))
            return self.view(
                db.execute(select(table).where(table.c.id == delivery_id)).mappings().one()
            )

    def list(self, principal: Principal, limit: int = 100) -> list[Delivery]:
        with self.db() as db:
            table = self._table(db)
            statement = select(table)
            if not principal.admin:
                statement = statement.where(table.c.user_id == principal.user_id)
            rows = db.execute(statement.order_by(table.c.created_at.desc()).limit(limit))
            return [self.view(row) for row in rows.mappings()]

    def owned(self, principal: Principal, delivery_id: str) -> dict:
        with self.db() as db:
            table = self._table(db)
            row = db.execute(select(table).where(table.c.id == delivery_id)).mappings().fetchone()
        if row is None:
            raise NotFound("delivery not found")
        if principal.user_id != row["user_id"]:
            raise Forbidden("delivery retry requires its owner")
        return dict(row)

    def retry(self, principal: Principal, delivery_id: str) -> Delivery:
        with self.db() as db, self.persistence.transaction(db):
            table = self._table(db)
            row = db.execute(select(table).where(table.c.id == delivery_id)).mappings().fetchone()
            if row is None:
                raise NotFound("delivery not found")
            if principal.user_id != row["user_id"]:
                raise Forbidden("delivery access denied")
            if row["pruned"]:
                raise Forbidden("Retained delivery identities cannot be retried")
            if row["state"] in {"blocked", "pending"}:
                db.execute(
                    table.update()
                    .where(table.c.id == delivery_id)
                    .values(
                        state="pending",
                        next_attempt=0,
                        error=None,
                        credential_id=principal.credential_id,
                        principal=principal.model_dump_json(),
                    )
                )
            return self.view(
                db.execute(select(table).where(table.c.id == delivery_id)).mappings().one()
            )

    @staticmethod
    def lease(row) -> WorkerLease:
        return WorkerLease(
            f"outbox:{row['id']}", row["lease_owner"], row["lease_fence"], row["lease_expires_at"]
        )

    def claim(self) -> dict | None:
        # An expired in-flight envelope is replayable because its remote source
        # identity is immutable. A live worker is never reset by replica startup.
        with self.db() as db:
            table = self._table(db)
            now = self.persistence.database_time(db)
            rows = (
                db.execute(
                    select(table)
                    .where(
                        or_(
                            and_(table.c.state == "pending", table.c.next_attempt <= now),
                            and_(table.c.state == "processing", table.c.lease_expires_at <= now),
                        )
                    )
                    .order_by(table.c.created_at)
                    .limit(100)
                )
                .mappings()
                .all()
            )
        for row in rows:
            lease = self.persistence.acquire_lease(f"outbox:{row['id']}", self.worker_id)
            if lease is None:
                continue
            try:
                with (
                    self.persistence.fenced(lease),
                    self.db() as db,
                    self.persistence.transaction(db),
                ):
                    table = self._table(db)
                    current = db.execute(select(table).where(table.c.id == row["id"]))
                    current = current.mappings().one()
                    if current["state"] not in {"pending", "processing"}:
                        self.persistence.release_lease(lease)
                        continue
                    db.execute(
                        table.update()
                        .where(table.c.id == row["id"])
                        .values(
                            state="processing",
                            attempts=table.c.attempts + 1,
                            lease_owner=lease.owner,
                            lease_fence=lease.fence,
                            lease_expires_at=lease.expires_at,
                        )
                    )
                    return {
                        **dict(current),
                        "lease_owner": lease.owner,
                        "lease_fence": lease.fence,
                        "lease_expires_at": lease.expires_at,
                    }
            except BaseException:
                self.persistence.release_lease(lease)
                raise
            self.persistence.release_lease(lease)
        return None

    def finish(
        self,
        delivery_id: str,
        state: str,
        error: str | None = None,
        episode_id: str | None = None,
        *,
        lease: WorkerLease,
    ):
        with self.persistence.fenced(lease), self.db() as db, self.persistence.transaction(db):
            table = self._table(db)
            row = db.execute(select(table.c.attempts).where(table.c.id == delivery_id)).fetchone()
            if row is None:
                raise NotFound("delivery not found")
            delay = min(3600, 2 ** min(row[0], 12)) if state == "pending" else 0
            updated = db.execute(
                table.update()
                .where(
                    table.c.id == delivery_id,
                    table.c.state == "processing",
                    table.c.lease_owner == lease.owner,
                    table.c.lease_fence == lease.fence,
                )
                .values(
                    state=state,
                    error=error,
                    next_attempt=self.persistence.database_time(db) + delay,
                    delivered_at=datetime.now(UTC).isoformat() if state == "delivered" else None,
                    remote_episode_id=episode_id,
                )
            )
            if not updated.rowcount:
                raise LeaseLost("Delivery ownership changed before completion")
        self.persistence.release_lease(lease)
