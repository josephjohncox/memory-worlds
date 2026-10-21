"""Durable publication envelopes, with a receipt independent from remote delivery."""

import hashlib
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from .errors import Conflict, Forbidden, NotFound
from .models import Identifier, Model, Principal, Publication


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
    mode: Literal["source", "graph"] = "source"
    limit: int = Field(default=10, ge=1, le=100)


class PublicationOutbox:
    def __init__(self, data_dir: Path, authority_id: str):
        self.path = Path(data_dir) / "router.sqlite3"
        self.authority_id = authority_id
        # Reuse the local store's restrictive file creation and SQLite durability policy.
        from .store import MemoryStore

        self._connect = MemoryStore._connect
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    credential_id TEXT NOT NULL, principal TEXT NOT NULL,
                    mount_id TEXT NOT NULL, authority_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL, world_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, delivered_at TEXT, error TEXT,
                    remote_episode_id TEXT,
                    UNIQUE(user_id,mount_id,world_id,source_id)
                );
            """)

    @contextmanager
    def db(self):
        connection = self._connect(self.path)
        try:
            yield connection
        finally:
            connection.close()

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
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute(
                    "SELECT * FROM deliveries WHERE user_id=? AND mount_id=? AND world_id=? "
                    "AND source_id=?",
                    (principal.user_id, mount.id, request.world_id, request.source_id),
                ).fetchone()
                if old:
                    if (
                        old["digest"] != digest
                        or old["endpoint"] != mount.url
                        or old["authority_id"] != mount.authority_id
                    ):
                        raise Conflict("publication identity already exists with different input")
                    db.commit()
                    return self.view(old)
                delivery_id = uuid4().hex
                # A destination-scoped stable ID survives lost acknowledgments and restarts.
                payload["source_id"] = f"publication:{self.authority_id}:{delivery_id}"
                db.execute(
                    "INSERT INTO deliveries (id,user_id,agent_id,credential_id,principal,mount_id,"
                    "authority_id,endpoint,world_id,source_id,payload,digest,state,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        delivery_id,
                        principal.user_id,
                        principal.agent_id,
                        principal.credential_id,
                        principal.model_dump_json(),
                        mount.id,
                        mount.authority_id,
                        mount.url,
                        request.world_id,
                        request.source_id,
                        json.dumps(payload),
                        digest,
                        "pending",
                        datetime.now(UTC).isoformat(),
                    ),
                )
                row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
                db.commit()
                return self.view(row)
            except BaseException:
                db.rollback()
                raise

    def list(self, principal: Principal, limit: int = 100) -> list[Delivery]:
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM deliveries "
                + ("" if principal.admin else "WHERE user_id=? ")
                + "ORDER BY created_at DESC LIMIT ?",
                (limit,) if principal.admin else (principal.user_id, limit),
            ).fetchall()
        return [self.view(row) for row in rows]

    def owned(self, principal: Principal, delivery_id: str) -> dict:
        """Only the owner can retry; administrator status does not permit impersonation."""
        with self.db() as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        if row is None:
            raise NotFound("delivery not found")
        if principal.user_id != row["user_id"]:
            raise Forbidden("delivery retry requires its owner")
        return dict(row)

    def retry(self, principal: Principal, delivery_id: str) -> Delivery:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None:
                db.rollback()
                raise NotFound("delivery not found")
            if principal.user_id != row["user_id"]:
                db.rollback()
                raise Forbidden("delivery access denied")
            if row["state"] in {"blocked", "pending"}:
                # Explicit owner retry authorizes dispatch with the current credential.
                # The original user_id and agent_id remain the attribution on the receipt.
                db.execute(
                    "UPDATE deliveries SET state='pending',next_attempt=0,error=NULL,"
                    "credential_id=?,principal=? WHERE id=?",
                    (principal.credential_id, principal.model_dump_json(), delivery_id),
                )
            db.commit()
            return self.view(
                db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            )

    def recover(self):
        with self.db() as db:
            db.execute(
                "UPDATE deliveries SET state='pending',next_attempt=0 WHERE state='processing'"
            )

    def claim(self) -> dict | None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM deliveries WHERE state='pending' AND next_attempt<=? "
                "ORDER BY created_at LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                "UPDATE deliveries SET state='processing',attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            db.commit()
            return dict(row)

    def finish(
        self, delivery_id: str, state: str, error: str | None = None, episode_id: str | None = None
    ):
        with self.db() as db:
            row = db.execute(
                "SELECT attempts FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            delay = min(3600, 2 ** min(row[0], 12)) if state == "pending" else 0
            db.execute(
                "UPDATE deliveries SET state=?,error=?,next_attempt=?,delivered_at=?,"
                "remote_episode_id=? WHERE id=?",
                (
                    state,
                    error,
                    time.time() + delay,
                    datetime.now(UTC).isoformat() if state == "delivered" else None,
                    episode_id,
                    delivery_id,
                ),
            )
