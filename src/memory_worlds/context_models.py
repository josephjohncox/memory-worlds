"""Typed surfaced context, recoverable coverage and guarded worker artifacts."""

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import Identifier, Model


class ContextSourceRef(Model):
    world_id: Identifier
    scope_id: UUID
    episode_id: str = Field(min_length=1, max_length=100)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContextEvent(Model):
    id: str = Field(min_length=1, max_length=200)
    parent_id: str | None = Field(default=None, max_length=200)
    type: Literal["message", "tool-call", "tool-result", "custom"]
    role: str | None = Field(default=None, max_length=40)
    payload: dict[str, Any]


class ContextOmission(Model):
    entry_id: str = Field(min_length=1, max_length=200)
    reason: Literal["excluded", "unsupported", "unavailable", "redacted"]


class ContextBundleInput(Model):
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    entries: list[ContextEvent] = Field(min_length=1, max_length=200)
    omissions: list[ContextOmission] = Field(default_factory=list, max_length=200)
    source_refs: list[ContextSourceRef] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def bounded_events(self):
        if len({item.id for item in self.entries}) != len(self.entries):
            raise ValueError("Context entry identifiers must be unique")
        try:
            for event in self.entries:
                json.dumps(event.payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Context event payloads must contain finite JSON values") from None
        value = self.model_dump(mode="json")
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode()) > 768_000:
            raise ValueError("Context bundle exceeds 750 KiB")
        calls = set()
        for event in self.entries:
            if event.type == "tool-call":
                call = event.payload.get("call_id")
                if not isinstance(call, str) or not call or call in calls:
                    raise ValueError("Tool calls require unique call_id values")
                calls.add(call)
            elif event.type == "tool-result":
                call = event.payload.get("call_id")
                if not isinstance(call, str) or call not in calls:
                    raise ValueError("Tool results must follow their captured call")
        return self


class ContextBundleReceipt(Model):
    id: UUID
    bundle_id: UUID
    thread_id: UUID
    revision: int
    digest: str
    archive_state: Literal["ready"] = "ready"
    coverage: Literal["complete", "partial"]
    source_refs: list[ContextSourceRef]
    uncompressed_bytes: int = 0
    stored_bytes: int = 0


class ContextBundle(ContextBundleReceipt):
    entries: list[ContextEvent]
    omissions: list[ContextOmission]


class ArtifactReceipt(Model):
    id: UUID
    artifact_id: UUID
    thread_id: UUID
    job_id: UUID
    kind: str
    content: str
    metadata: dict[str, Any]
    source_refs: list[ContextSourceRef]
