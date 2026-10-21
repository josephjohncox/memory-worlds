"""Native PostgreSQL engine configuration and namespaced advisory keys."""

from __future__ import annotations

import hashlib

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from .errors import MemoryError as InvalidRequest


def postgres_engine(database_url: str, *, pooled: bool = True) -> Engine:
    try:
        url = make_url(database_url)
    except Exception:
        raise InvalidRequest("Invalid PostgreSQL connection configuration") from None
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise InvalidRequest("Shared storage requires a PostgreSQL psycopg connection URL")
    options = (
        {"pool_pre_ping": True, "pool_size": 10, "max_overflow": 20, "pool_timeout": 0}
        if pooled
        else {"poolclass": NullPool}
    )
    return create_engine(
        url.set(drivername="postgresql+psycopg"),
        hide_parameters=True,
        connect_args={"connect_timeout": 5},
        **options,
    )


def advisory_key(authority_id: str, resource: str) -> int:
    data = hashlib.sha256(f"memory-worlds:{authority_id}:{resource}".encode()).digest()[:8]
    return int.from_bytes(data, "big", signed=True)
