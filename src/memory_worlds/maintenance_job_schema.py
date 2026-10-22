"""Register current maintenance control tables without importing persistence."""

from sqlalchemy import BigInteger, Column, Float, Integer, Table, Text


def register(metadata):
    if "maintenance_jobs" in metadata.tables:
        return
    Table(
        "maintenance_jobs",
        metadata,
        Column("id", Text, primary_key=True),
        Column("credential_id", Text, nullable=False),
        Column("user_id", Text, nullable=False),
        Column("principal", Text, nullable=False),
        Column("request", Text, nullable=False),
        Column("request_digest", Text, nullable=False),
        Column("frozen_input", Text, nullable=False),
        Column("profile", Text),
        Column("profile_digest", Text),
        Column("state", Text, nullable=False),
        Column("attempts", Integer, nullable=False, default=0),
        Column("cancel_requested", Integer, nullable=False, default=0),
        Column("effect_dispatched", Integer, nullable=False, default=0),
        Column("result_artifact_id", Text),
        Column("error", Text),
        Column("next_attempt", Float, nullable=False, default=0),
        Column("created_at", Text, nullable=False),
        Column("updated_at", Text, nullable=False),
        Column("lease_owner", Text),
        Column("lease_fence", BigInteger),
        Column("lease_expires_at", Float),
    )
    Table(
        "maintenance_attempts",
        metadata,
        Column("job_id", Text, primary_key=True),
        Column("number", Integer, primary_key=True),
        Column("state", Text, nullable=False),
        Column("reserved_tokens", Integer, nullable=False),
        Column("dispatched_at", Text, nullable=False),
        Column("error", Text),
    )
    Table(
        "maintenance_budgets",
        metadata,
        Column("profile_digest", Text, primary_key=True),
        Column("day", Text, primary_key=True),
        Column("reserved_tokens", Integer, nullable=False, default=0),
    )
