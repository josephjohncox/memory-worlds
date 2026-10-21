from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorldKind(StrEnum):
    PERSONAL = "personal"
    PRIVATE_WORK = "private-work"
    TEAM = "team"
    COMPANY = "company"
    AD_HOC = "ad-hoc"


class Role(StrEnum):
    READER = "reader"
    WRITER = "writer"
    MANAGER = "manager"


class Principal(Model):
    credential_id: str
    user_id: str
    agent_id: str
    admin: bool = False


class MembershipUpdate(Model):
    role: Role | None


class UserUpdate(Model):
    enabled: bool = Field(strict=True)


class World(Model):
    id: Identifier
    authority_id: str
    name: str
    kind: WorldKind
    owner_id: str
    created_at: datetime
    generation: str | None = None
    projection_state: Literal["disabled", "ready", "degraded"] = "disabled"


class CreateWorld(Model):
    name: str = Field(min_length=1, max_length=200)
    kind: WorldKind = WorldKind.PERSONAL
    owner_id: Identifier | None = None


class CreateToken(Model):
    user_id: Identifier
    agent_id: Identifier
    grants: dict[Identifier, Role] = Field(default_factory=dict, max_length=100)
    admin: bool = False


class EpisodeInput(Model):
    source_id: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    occurred_at: AwareDatetime
    source_type: Literal["text", "chatgpt", "pi", "publication"] = "text"
    session_id: str | None = Field(default=None, max_length=500)
    source_entry_id: str | None = Field(default=None, max_length=500)
    parent_entry_id: str | None = Field(default=None, max_length=500)
    role: str | None = Field(default=None, max_length=100)
    source_ref: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    project: bool = False

    @field_validator("content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content cannot be blank")
        return value


class Episode(EpisodeInput):
    id: str
    world_id: str
    sequence: int
    digest: str
    received_at: datetime
    actor_id: str
    agent_id: str
    projection_state: Literal["disabled", "pending", "processing", "projected", "unknown"]


class Receipt(Model):
    episode: Episode
    replayed: bool = False


class SearchRequest(Model):
    world_ids: list[Identifier] = Field(min_length=1, max_length=50)
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=100)
    mode: Literal["source", "graph"] = "source"


class Publication(Model):
    source_id: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    occurred_at: AwareDatetime
    project: bool = False
