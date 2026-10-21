from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")]
Alias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
MAX_IMPORT_BYTES = 32 * 1024 * 1024
MAX_IMPORT_EPISODES = 10_000


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
    restricted: bool = False
    agent_uuid: UUID | None = None
    invocation_id: UUID | None = None
    parent_invocation_id: UUID | None = None


class MembershipUpdate(Model):
    role: Role | None


class UserUpdate(Model):
    enabled: bool = Field(strict=True)


class World(Model):
    id: Identifier
    authority_id: str
    name: str
    alias: Alias | None = None
    kind: WorldKind
    owner_id: str
    created_at: datetime
    generation: str | None = None
    projection_state: Literal["disabled", "ready", "degraded"] = "disabled"
    scope_id: UUID


class CreateWorld(Model):
    name: str = Field(min_length=1, max_length=200)
    alias: Alias | None = None
    kind: WorldKind = WorldKind.PERSONAL
    owner_id: Identifier | None = None


class UpdateAlias(Model):
    alias: Alias | None


class CreateToken(Model):
    user_id: Identifier
    agent_id: Identifier
    grants: dict[Identifier, Role] = Field(default_factory=dict, max_length=100)
    admin: bool = False


class EpisodeInput(Model):
    scope_id: UUID | None = None
    context_id: UUID | None = None
    source_id: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    occurred_at: AwareDatetime
    source_type: Literal["text", "chatgpt", "claude", "pi", "publication"] = "text"
    session_id: str | None = Field(default=None, max_length=500)
    source_entry_id: str | None = Field(default=None, max_length=500)
    parent_entry_id: str | None = Field(default=None, max_length=500)
    role: str | None = Field(default=None, max_length=100)
    source_ref: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    project: bool = True

    @field_validator("content")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content cannot be blank")
        return value


class ImportBatch(Model):
    episodes: list[EpisodeInput] = Field(min_length=1, max_length=MAX_IMPORT_EPISODES)


class Episode(EpisodeInput):
    scope_id: UUID
    id: str
    world_id: str
    sequence: int
    digest: str
    received_at: datetime
    actor_id: str
    agent_id: str
    agent_uuid: UUID | None = None
    invocation_id: UUID | None = None
    parent_invocation_id: UUID | None = None
    projection_state: Literal["disabled", "pending", "processing", "projected", "unknown"]


class Receipt(Model):
    episode: Episode
    replayed: bool = False


class SearchRequest(Model):
    world_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=100)
    mode: Literal["source", "graph"] = "graph"
    include_entities: bool = False
    scope_id: UUID | None = None
    scopes: list["ScopeSelection"] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def selected_scope(self):
        if not self.world_ids and not self.scopes:
            raise ValueError("Select at least one world or scope")
        if self.scope_id is not None and self.scopes:
            raise ValueError("Use scope_id or scopes, not both")
        return self


class Publication(Model):
    context_id: UUID | None = None
    source_id: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=200_000)
    occurred_at: AwareDatetime
    project: bool = True


class ScopeKind(StrEnum):
    WORLD_SHARED = "world-shared"
    RUN_SHARED = "run-shared"
    AGENT_PERSISTENT = "agent-persistent"
    INVOCATION_PRIVATE = "invocation-private"


class ScopeOperation(StrEnum):
    READ = "read"
    WRITE = "write"
    PUBLISH = "publish"
    DELEGATE = "delegate"
    MANAGE = "manage"


class ScopeSelection(Model):
    world_id: Identifier
    scope_id: UUID


class ScopeFlow(Model):
    read_scopes: list[ScopeSelection] = Field(default_factory=list, max_length=50)
    write_scope: ScopeSelection


class Scope(Model):
    id: UUID
    world_id: Identifier
    kind: ScopeKind
    name: str
    alias: Alias | None = None
    created_at: AwareDatetime
    creator_credential_id: str | None = None
    coordinator_credential_id: str | None = None
    run_id: UUID | None = None
    agent_id: UUID | None = None
    invocation_id: UUID | None = None
    parent_invocation_id: UUID | None = None
    generation: str | None = None
    projection_state: Literal["disabled", "ready", "degraded"] = "disabled"


class CreateScope(Model):
    kind: ScopeKind
    name: str = Field(min_length=1, max_length=200)
    alias: Alias | None = None
    run_id: UUID | None = None
    agent_id: UUID | None = None
    invocation_id: UUID | None = None


class ScopeGrant(ScopeSelection):
    operations: list[ScopeOperation] = Field(min_length=1, max_length=5)


class UpdateScopeGrant(Model):
    credential_id: str = Field(min_length=1, max_length=100)
    operations: list[ScopeOperation] = Field(default_factory=list, max_length=5)


class BindContext(Model):
    read_scopes: list[ScopeSelection] = Field(default_factory=list, max_length=50)
    write_scope: ScopeSelection | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None


class AgentContext(BindContext):
    id: UUID
    credential_id: str
    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    created_at: AwareDatetime


class DelegateCredential(Model):
    grants: list[ScopeGrant] = Field(default_factory=list, max_length=50)
    private_world_id: Identifier | None = None
    agent_id: UUID | None = None
    run_id: UUID | None = None
    expires_at: AwareDatetime | None = None
    allow_delegation: bool = False
    parent_context_id: UUID | None = None
    coordinator_read: bool = False


class ScopedPublication(Publication):
    source_scope: ScopeSelection | None = None
    source_episode_id: str | None = Field(default=None, max_length=100)


SearchRequest.model_rebuild()
