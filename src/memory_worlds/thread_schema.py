"""Thread table declarations without importing the persistence implementation."""

from sqlalchemy import Column, ForeignKey, Integer, Table, Text, UniqueConstraint


def register(metadata):
    if "memory_threads" in metadata.tables:
        return
    Table(
        "memory_threads",
        metadata,
        Column("id", Text, primary_key=True),
        Column("world_id", Text, ForeignKey("worlds.id"), nullable=False),
        Column("scope_id", Text, ForeignKey("scopes.id"), nullable=False, unique=True),
        Column("owner_id", Text, nullable=False),
        Column("creator_credential_id", Text, nullable=False),
        Column("alias", Text),
        Column("revision", Integer, nullable=False),
        Column("payload", Text, nullable=False),
        UniqueConstraint("owner_id", "world_id", "alias"),
    )
    Table(
        "thread_operations",
        metadata,
        Column("id", Text, primary_key=True),
        Column("credential_id", Text, nullable=False),
        Column("kind", Text, nullable=False),
        Column("request_digest", Text, nullable=False),
        Column("payload", Text, nullable=False),
        Column("state", Text, nullable=False),
        Column("receipt", Text),
    )
    Table(
        "thread_revisions",
        metadata,
        Column("thread_id", Text, ForeignKey("memory_threads.id"), primary_key=True),
        Column("revision", Integer, primary_key=True),
        Column("payload", Text, nullable=False),
    )
