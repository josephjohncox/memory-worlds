"""Current typed contracts for bounded maintenance proposals and durable jobs."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .models import Model

ProfileName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
JobKind = Literal["summarize", "assess-enrichment"]
JobState = Literal["queued", "running", "succeeded", "failed", "cancelled", "blocked", "stale"]


class MaintenanceRequest(Model):
    operation_id: UUID
    thread_id: UUID
    bundle_ids: list[UUID] = Field(min_length=1, max_length=100)
    profile: ProfileName = "lightweight"
    kind: JobKind = "summarize"

    @field_validator("bundle_ids")
    @classmethod
    def unique_bundles(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Maintenance input bundle IDs must be unique")
        return value


class MaintenanceReceipt(Model):
    id: UUID
    operation_id: UUID
    thread_id: UUID
    kind: JobKind
    profile: ProfileName
    state: JobState
    attempts: int
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    result_artifact_id: UUID | None = None
    result: dict[str, Any] | None = None
    usage_uncertain: bool = False
    unresolved_effects: list[str] = Field(default_factory=list)
    replayed: bool = False


class EnrichmentFlag(Model):
    reason: Literal[
        "missing-evidence",
        "missing-source",
        "ambiguous-entity",
        "conflicting-sources",
        "stale-source",
        "missing-context",
        "needs-validation",
    ]
    bundle_id: UUID
    question: str = Field(min_length=1, max_length=1000)


class MaintenanceOutput(Model):
    """A model proposes content; it never supplies a write destination or action."""

    kind: Literal["summary", "enrichment-assessment"]
    content: str = Field(min_length=1, max_length=12_000)
    source_bundle_ids: list[UUID] = Field(min_length=1, max_length=100)
    unresolved_questions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=20
    )
    contradictions: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=20
    )
    flags: list[EnrichmentFlag] = Field(default_factory=list, max_length=20)

    @field_validator("content")
    @classmethod
    def nonblank_content(cls, value):
        if not value.strip():
            raise ValueError("Maintenance content must not be blank")
        return value

    @model_validator(mode="after")
    def kind_contract(self):
        if len(set(self.source_bundle_ids)) != len(self.source_bundle_ids):
            raise ValueError("Source bundle IDs must be unique")
        if self.kind == "summary" and self.flags:
            raise ValueError("A summary cannot contain enrichment actions")
        if any(flag.bundle_id not in self.source_bundle_ids for flag in self.flags):
            raise ValueError("Enrichment flags must refer to selected source bundles")
        return self
