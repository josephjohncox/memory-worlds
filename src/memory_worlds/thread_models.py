"""Current source-backed thread contracts; thread identifiers confer no access."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from .models import Alias, Identifier, Model

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CreateThread(Model):
    operation_id: UUID
    world_id: Identifier
    name: str = Field(min_length=1, max_length=200)
    alias: Alias | None = None
    thread_id: UUID | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None
    expires_at: AwareDatetime | None = None

    @field_validator("name")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Thread name cannot be blank")
        return value


class Thread(Model):
    id: UUID
    authority_id: str
    world_id: Identifier
    scope_id: UUID
    owner_id: str
    creator_credential_id: str
    name: str
    alias: Alias | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None
    revision: int = Field(ge=0)
    head_digest: Digest
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    state: Literal["open", "closed", "expired", "retracted"]


class ThreadSource(Model):
    episode_id: str = Field(min_length=1, max_length=100)
    digest: Digest


class ThreadRevision(Model):
    thread_id: UUID
    revision: int = Field(ge=0)
    sources: list[ThreadSource] = Field(default_factory=list, max_length=1000)
    digest: Digest
    created_at: AwareDatetime


class AppendThread(Model):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    sources: list[ThreadSource] = Field(min_length=1, max_length=100)
