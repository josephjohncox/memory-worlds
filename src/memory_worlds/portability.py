"""Portable source archives and branch-preserving conversation import.

Importers validate complete inputs before asking the store for one transaction.
Archives carry evidence and attribution, never live credentials or membership.
Restored and imported evidence queues scope-isolated Graphiti extraction by default.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError, field_validator

from .errors import MemoryError as RequestError
from .models import Episode, EpisodeInput, Model, Principal, Scope, ScopeKind, World

MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_EPISODES = 100_000
MAX_DEPENDENCIES = 100_000
_PROJECTION_FIELDS = {
    "backend",
    "graphiti_version",
    "llm_provider",
    "llm_model",
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    "reranking",
}


class PortabilityError(RequestError):
    code = "invalid_portable_data"


class PortableStore(Protocol):
    def snapshot(self, principal: Principal, world_id: str, scope_id=None) -> dict: ...

    def restore(
        self,
        principal: Principal,
        world_id: str,
        episodes: list[EpisodeInput],
        scope_id=None,
        *,
        dependencies=None,
    ) -> dict: ...


class AuditRecord(Model):
    sequence: int = Field(ge=1, strict=True)
    event: str = Field(min_length=1, max_length=100)
    episode_id: str = Field(min_length=1, max_length=500)
    actor_id: str = Field(min_length=1, max_length=500)
    occurred_at: str


class DependencyRecord(Model):
    output_episode_id: str = Field(min_length=1, max_length=100)
    input_episode_id: str = Field(min_length=1, max_length=100)
    input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Manifest(Model):
    format: str = Field(pattern=r"^memory-worlds$")
    version: int = Field(ge=2, le=2, strict=True)
    exported_at: str
    world: World
    scope: Scope
    episode_count: int = Field(ge=0, le=MAX_EPISODES, strict=True)
    episodes_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audit_count: int = Field(ge=0, le=MAX_EPISODES, strict=True)
    audit_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dependency_count: int = Field(ge=0, le=MAX_DEPENDENCIES, strict=True)
    dependencies_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovery: str = Field(pattern=r"^source-only; graph re-extraction required$")
    context_recovery: str = Field(
        pattern=r"^source-evidence-only; thread, bundle and job receipts require authority backup$"
    )
    projection_configuration: dict[str, str | int | None] = Field(default_factory=dict)

    @field_validator("projection_configuration", mode="before")
    @classmethod
    def public_configuration(cls, value: Any) -> dict:
        if not isinstance(value, dict) or not set(value).issubset(_PROJECTION_FIELDS):
            raise ValueError("Unknown portable projection configuration fields")
        for item in value.values():
            if item is not None and (
                isinstance(item, bool)
                or not isinstance(item, (str, int))
                or isinstance(item, str)
                and len(item) > 1000
            ):
                raise ValueError("Invalid portable projection configuration value")
        return value


def _json(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _dependencies(episodes: list[Episode], values) -> list[DependencyRecord]:
    """Validate the complete same-scope derivation graph before export or restore."""
    if not isinstance(values, list) or len(values) > MAX_DEPENDENCIES:
        raise PortabilityError("Portable dependency graph exceeds its edge limit")
    records = [DependencyRecord.model_validate(value) for value in values]
    indexed = {episode.id: episode for episode in episodes}
    parents = {}
    seen = set()
    for record in records:
        output, source = record.output_episode_id, record.input_episode_id
        if output not in indexed or source not in indexed:
            raise PortabilityError("Portable dependencies reference missing source evidence")
        if source == output or (output, source) in seen:
            raise PortabilityError("Portable dependencies contain a self reference or duplicate")
        if indexed[source].digest != record.input_digest:
            raise PortabilityError("Portable dependency source digest mismatch")
        seen.add((output, source))
        parents.setdefault(output, set()).add(source)
    for output, sources in parents.items():
        if any(not parents.get(source, set()).issubset(sources) for source in sources):
            raise PortabilityError("Portable dependencies are missing their transitive closure")
    # A flattened cycle necessarily contains a forbidden self edge. Validate the
    # graph independently so a future closure check cannot admit cyclic evidence.
    degree = {identifier: len(parents.get(identifier, ())) for identifier in indexed}
    children = {}
    for output, sources in parents.items():
        for source in sources:
            children.setdefault(source, []).append(output)
    ready = [identifier for identifier, count in degree.items() if count == 0]
    visited = 0
    while ready:
        source = ready.pop()
        visited += 1
        for output in children.get(source, ()):
            degree[output] -= 1
            if degree[output] == 0:
                ready.append(output)
    if visited != len(indexed):
        raise PortabilityError("Portable dependencies contain a cycle")
    return sorted(records, key=lambda item: (item.output_episode_id, item.input_episode_id))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PortabilityError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PortabilityError(f"Non-finite JSON number: {value}")


def _load_json(data: bytes | str, label: str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PortabilityError(f"Invalid JSON in {label}: {exc}") from exc


def _read_bounded(path: Path, maximum: int = MAX_INPUT_BYTES) -> bytes:
    if path.stat().st_size > maximum:
        raise PortabilityError(f"Input exceeds {maximum} bytes")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise PortabilityError(f"Input exceeds {maximum} bytes")
    return data


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise PortabilityError(f"{label} must be a nonempty string of at most 500 characters")
    return value


def _timestamp(value: Any, label: str, *, milliseconds: bool = False) -> datetime:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean timestamp")
        if isinstance(value, (float, int)):
            if not math.isfinite(value):
                raise ValueError("non-finite timestamp")
            return datetime.fromtimestamp(value / 1000 if milliseconds else value, UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone required")
            return parsed.astimezone(UTC)
        raise ValueError("timestamp is missing or has unsupported type")
    except (ValueError, OverflowError, OSError) as exc:
        raise PortabilityError(f"Invalid {label}: {exc}") from exc


def _episode(value: dict) -> EpisodeInput:
    try:
        episode = EpisodeInput.model_validate(value)
        if len(_json(episode)) > MAX_RECORD_BYTES:
            raise PortabilityError("Episode exceeds the portable record size limit")
        return episode
    except (ValidationError, UnicodeEncodeError, ValueError, RecursionError) as exc:
        raise PortabilityError(f"Invalid episode: {exc}") from exc


class EpisodeBuffer(list[EpisodeInput]):
    """Bound output amplification while a parser builds normalized records."""

    def __init__(self, *, max_episodes=MAX_EPISODES, max_bytes=MAX_UNCOMPRESSED_BYTES):
        super().__init__()
        self.max_episodes = max_episodes
        self.max_bytes = max_bytes
        self.encoded_bytes = 0

    def append(self, episode: EpisodeInput) -> None:
        if len(self) >= self.max_episodes:
            raise PortabilityError("Too many episodes in one import")
        size = len(_json(episode))
        if self.encoded_bytes + size > self.max_bytes:
            raise PortabilityError("Imported episodes exceed the encoded output byte limit")
        self.encoded_bytes += size
        super().append(episode)


def export_world(
    store: PortableStore,
    principal: Principal,
    world_id: str,
    destination: Path,
    *,
    scope_id: UUID | str | None = None,
) -> Path:
    """Publish a consistent, immutable .mworld ZIP beside its staging file.

    The store owns authorization and the consistent snapshot. A hard link makes
    the completed name visible atomically without replacing an existing artifact.
    The staging file and final file are on the same destination filesystem.
    """
    snapshot = store.snapshot(principal, world_id, **({"scope_id": scope_id} if scope_id else {}))
    try:
        world = World.model_validate(snapshot["world"])
        episodes = [Episode.model_validate(item) for item in snapshot["episodes"]]
        audit = [AuditRecord.model_validate(item) for item in snapshot["audit"]]
        scope = Scope.model_validate(snapshot["scope"])
        dependencies = _dependencies(episodes, snapshot["artifact_dependencies"])
    except (KeyError, TypeError, ValidationError) as exc:
        raise PortabilityError(f"Invalid store snapshot: {exc}") from exc
    if len(episodes) > MAX_EPISODES or len(audit) > MAX_EPISODES:
        raise PortabilityError("Too many episodes for one portable archive")
    if any(item.world_id != world.id for item in episodes):
        raise PortabilityError("Snapshot contains episodes from another world")
    if (
        scope.world_id != world.id
        or world.scope_id != scope.id
        or (scope_id is not None and scope.id != UUID(str(scope_id)))
        or any(item.scope_id != scope.id for item in episodes)
    ):
        raise PortabilityError("Snapshot contains evidence from another scope")

    records = [_json(item) + b"\n" for item in episodes]
    if any(len(record) > MAX_RECORD_BYTES for record in records):
        raise PortabilityError("An episode exceeds the portable record size limit")
    body = b"".join(records)
    audit_body = b"".join(_json(item) + b"\n" for item in audit)
    dependency_body = b"".join(_json(item) + b"\n" for item in dependencies)
    manifest = Manifest(
        format="memory-worlds",
        version=2,
        exported_at=datetime.now(UTC).isoformat(),
        world=world,
        scope=scope,
        episode_count=len(episodes),
        episodes_sha256=hashlib.sha256(body).hexdigest(),
        audit_count=len(audit),
        audit_sha256=hashlib.sha256(audit_body).hexdigest(),
        dependency_count=len(dependencies),
        dependencies_sha256=hashlib.sha256(dependency_body).hexdigest(),
        recovery="source-only; graph re-extraction required",
        context_recovery=(
            "source-evidence-only; thread, bundle and job receipts require authority backup"
        ),
        projection_configuration={
            key: value
            for key, value in snapshot.get("projection_configuration", {}).items()
            if key in _PROJECTION_FIELDS
        },
    )
    manifest_bytes = _json(manifest)
    if (
        len(body) + len(audit_body) + len(dependency_body) + len(manifest_bytes)
        > MAX_UNCOMPRESSED_BYTES
    ):
        raise PortabilityError("Portable archive exceeds the uncompressed size limit")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise PortabilityError("Portable archive manifest exceeds the size limit")

    destination = Path(destination)
    if destination.is_dir():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = destination / f"{world.id}-{stamp}.mworld"
    if destination.suffix != ".mworld":
        raise PortabilityError("Portable archive destination must end in .mworld")
    if destination.exists() or destination.is_symlink():
        raise PortabilityError("Portable archive destination already exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".memory-worlds-", suffix=".pending", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                archive.writestr("episodes.jsonl", body)
                archive.writestr("audit.jsonl", audit_body)
                archive.writestr("dependencies.jsonl", dependency_body)
            stream.flush()
            os.fsync(stream.fileno())
            if stream.tell() > MAX_ARCHIVE_BYTES:
                raise PortabilityError("Portable archive exceeds the compressed size limit")
        # Unlike replace(), link() cannot silently clobber another completed export.
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise PortabilityError("Portable archive destination already exists") from exc
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _read_archive(
    path: Path,
) -> tuple[Manifest, list[Episode], list[AuditRecord], list[DependencyRecord]]:
    if path.suffix != ".mworld":
        raise PortabilityError("Only finalized .mworld archives can be restored")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PortabilityError("Portable archive exceeds the compressed size limit")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != 4 or {item.filename for item in entries} != {
                "manifest.json",
                "episodes.jsonl",
                "audit.jsonl",
                "dependencies.jsonl",
            }:
                raise PortabilityError(
                    "Archive must contain manifest, source, audit and dependency records"
                )
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
                raise PortabilityError("Portable archive exceeds the uncompressed size limit")
            for item in entries:
                if (
                    item.flag_bits & 1
                    or stat.S_ISLNK(item.external_attr >> 16)
                    or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                ):
                    raise PortabilityError("Unsupported archive entry type or encryption")
            if archive.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                raise PortabilityError("Portable archive manifest exceeds the size limit")
            manifest = Manifest.model_validate(
                _load_json(archive.read("manifest.json"), "manifest")
            )
            _timestamp(manifest.exported_at, "export timestamp")
            body = archive.read("episodes.jsonl")
            audit_body = archive.read("audit.jsonl")
            dependency_body = archive.read("dependencies.jsonl")
    except (
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
        ValidationError,
        EOFError,
        zlib.error,
    ) as exc:
        raise PortabilityError(f"Invalid portable archive: {exc}") from exc
    if hashlib.sha256(body).hexdigest() != manifest.episodes_sha256:
        raise PortabilityError("Episode checksum mismatch")
    lines = body.splitlines()
    if len(lines) != manifest.episode_count:
        raise PortabilityError("Episode count does not match manifest")
    if manifest.scope.world_id != manifest.world.id:
        raise PortabilityError("Invalid scoped archive identity")
    if manifest.world.scope_id != manifest.scope.id:
        raise PortabilityError("Scoped archive is missing its audience identity")
    episodes = []
    identities = set()
    episode_ids = set()
    for index, line in enumerate(lines, 1):
        if len(line) > MAX_RECORD_BYTES:
            raise PortabilityError(f"Episode {index} exceeds the record size limit")
        try:
            episode = Episode.model_validate(_load_json(line, f"episode {index}"))
        except ValidationError as exc:
            raise PortabilityError(f"Invalid archived episode {index}: {exc}") from exc
        if episode.source_id in identities:
            raise PortabilityError(f"Duplicate archived source identity: {episode.source_id}")
        identities.add(episode.source_id)
        if episode.id in episode_ids:
            raise PortabilityError(f"Duplicate archived episode id: {episode.id}")
        episode_ids.add(episode.id)
        if episode.world_id != manifest.world.id:
            raise PortabilityError("Archive contains an episode from another world")
        if episode.scope_id != manifest.scope.id:
            raise PortabilityError("Archive contains an episode from another scope")
        try:
            canonical = json.dumps(
                episode.model_dump(mode="json", include=set(EpisodeInput.model_fields)),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
            raise PortabilityError(f"Invalid canonical source episode {index}") from exc
        if hashlib.sha256(canonical).hexdigest() != episode.digest:
            raise PortabilityError(f"Episode {index} source digest mismatch")
        episodes.append(episode)
    if hashlib.sha256(audit_body).hexdigest() != manifest.audit_sha256:
        raise PortabilityError("Audit checksum mismatch")
    audit_lines = audit_body.splitlines()
    if len(audit_lines) != manifest.audit_count:
        raise PortabilityError("Audit count does not match manifest")
    audit = []
    audit_sequences = set()
    for index, line in enumerate(audit_lines, 1):
        if len(line) > MAX_RECORD_BYTES:
            raise PortabilityError(f"Audit {index} exceeds the record size limit")
        try:
            record = AuditRecord.model_validate(_load_json(line, f"audit {index}"))
        except ValidationError as exc:
            raise PortabilityError(f"Invalid archived audit {index}: {exc}") from exc
        if record.episode_id not in episode_ids or record.sequence in audit_sequences:
            raise PortabilityError("Audit references a missing episode or duplicates a sequence")
        _timestamp(record.occurred_at, f"audit timestamp {index}")
        audit_sequences.add(record.sequence)
        audit.append(record)
    if hashlib.sha256(dependency_body).hexdigest() != manifest.dependencies_sha256:
        raise PortabilityError("Dependency checksum mismatch")
    dependency_lines = dependency_body.splitlines()
    if len(dependency_lines) != manifest.dependency_count:
        raise PortabilityError("Dependency count does not match manifest")
    if any(len(line) > MAX_RECORD_BYTES for line in dependency_lines):
        raise PortabilityError("A dependency exceeds the portable record size limit")
    try:
        dependencies = _dependencies(
            episodes, [_load_json(line, "dependency") for line in dependency_lines]
        )
    except ValidationError:
        raise PortabilityError("Invalid portable dependency record") from None
    return manifest, episodes, audit, dependencies


def restore_world(
    store: PortableStore,
    principal: Principal,
    world_id: str,
    archive: Path,
    *,
    scope_id: UUID | str | None = None,
) -> dict:
    """Verify the entire archive before one authorized, transactional restore."""
    manifest, records, audit, dependencies = _read_archive(Path(archive))
    if manifest.scope.kind != ScopeKind.WORLD_SHARED and scope_id is None:
        raise PortabilityError("Scoped archives require an explicit private destination scope")
    target = store.snapshot(principal, world_id, scope_id=scope_id)
    destination_scope = Scope.model_validate(target["scope"])
    if manifest.scope.kind != ScopeKind.WORLD_SHARED:
        allowed = {manifest.scope.kind, ScopeKind.INVOCATION_PRIVATE}
        if destination_scope.kind not in allowed:
            raise PortabilityError("Restore cannot broaden the archived scope's audience kind")
    audit_by_episode: dict[str, list[dict]] = {}
    for item in audit:
        audit_by_episode.setdefault(item.episode_id, []).append(item.model_dump(mode="json"))
    episodes = []
    for record in records:
        fields = record.model_dump(mode="json", include=set(EpisodeInput.model_fields))
        origin = {
            "authority_id": manifest.world.authority_id,
            "world_id": record.world_id,
            "episode_id": record.id,
            "actor_id": record.actor_id,
            "agent_id": record.agent_id,
            "received_at": record.received_at.isoformat(),
            "digest": record.digest,
            "projection_state": record.projection_state,
            "audit": audit_by_episode.get(record.id, []),
        }
        origin["scope"] = manifest.scope.model_dump(mode="json")
        origin["context_receipts_restored"] = False
        for key in ("agent_uuid", "invocation_id", "parent_invocation_id", "context_id"):
            value = getattr(record, key, None)
            if value is not None:
                origin[key] = str(value)
        metadata = dict(record.metadata)
        if "memory_worlds_origin" in metadata:
            origin["prior"] = metadata["memory_worlds_origin"]
        metadata["memory_worlds_origin"] = origin
        fields.update(
            metadata=metadata,
            project=True,
            scope_id=destination_scope.id,
            context_id=None,
        )
        episodes.append(_episode(fields))
    return store.restore(
        principal,
        world_id,
        episodes,
        **({"scope_id": scope_id} if scope_id else {}),
        dependencies=dependencies,
    )


def _validate_tree(nodes: dict[str, dict], parent_key: str) -> dict[str, int]:
    """Validate every branch, including branches excluded from this import."""
    parents = {}
    for node_id, node in nodes.items():
        if parent_key not in node:
            raise PortabilityError(f"Missing parent pointer for entry {node_id!r}")
        parent = node.get(parent_key)
        if parent is not None and (not isinstance(parent, str) or parent not in nodes):
            raise PortabilityError(f"Missing parent pointer for entry {node_id!r}")
        parents[node_id] = parent
    depths: dict[str, int] = {}
    for node_id in nodes:
        trail = []
        seen = set()
        current = node_id
        while current is not None and current not in depths:
            if current in seen:
                raise PortabilityError(f"Cycle in branch parent pointers at {current!r}")
            seen.add(current)
            trail.append(current)
            current = parents[current]
        depth = depths[current] + 1 if current is not None else 0
        for item in reversed(trail):
            depths[item] = depth
            depth += 1
    return depths


def _selected_nodes(
    nodes: dict[str, dict],
    depths: dict[str, int],
    parent_key: str,
    leaf: str,
    all_branches: bool,
) -> list[str]:
    if leaf not in nodes:
        raise PortabilityError(f"Missing active branch pointer: {leaf!r}")
    if all_branches:
        return sorted(nodes, key=lambda key: (depths[key], key))
    selected = []
    current: str | None = leaf
    while current is not None:
        selected.append(current)
        current = nodes[current].get(parent_key)
    return list(reversed(selected))


def _content(content: Any, source: str) -> tuple[str, int, dict]:
    """Keep unsupported parts as raw evidence; never fetch referenced assets."""
    raw: dict = {}
    if isinstance(content, str):
        return content, 0, raw
    if source == "chatgpt":
        if not isinstance(content, dict):
            raise PortabilityError("ChatGPT message content must be an object")
        parts = content.get("parts")
        if parts is None:
            return "", 1, {"raw_content": content}
        if not isinstance(parts, list):
            raise PortabilityError("ChatGPT content parts must be an array")
        texts = [part for part in parts if isinstance(part, str)]
        unsupported = sum(not isinstance(part, str) for part in parts)
        if content.get("content_type", "text") != "text" and not unsupported:
            unsupported = 1
        if unsupported or content.get("content_type", "text") != "text":
            raw["raw_content"] = content
        return "\n".join(texts), unsupported, raw
    if not isinstance(content, list):
        raise PortabilityError("Pi message content must be a string or array")
    texts = []
    unsupported = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            if not isinstance(part.get("text"), str):
                raise PortabilityError("Pi text content must contain a text string")
            texts.append(part["text"])
        else:
            unsupported += 1
    if unsupported:
        raw["raw_content"] = content
    return "\n".join(texts), unsupported, raw


def parse_chatgpt(
    data: bytes,
    all_branches: bool = False,
    *,
    max_episodes: int = MAX_EPISODES,
    max_bytes: int = MAX_UNCOMPRESSED_BYTES,
) -> tuple[list[EpisodeInput], dict]:
    """Validate native ChatGPT bytes without writing or flattening branches."""
    if not isinstance(data, bytes) or len(data) > MAX_INPUT_BYTES:
        raise PortabilityError(f"Input must be bytes of at most {MAX_INPUT_BYTES} bytes")
    data = _load_json(data, "ChatGPT export")
    conversations = data if isinstance(data, list) else [data]
    if not conversations or any(not isinstance(item, dict) for item in conversations):
        raise PortabilityError("ChatGPT export must contain conversation objects")
    episodes = EpisodeBuffer(max_episodes=max_episodes, max_bytes=max_bytes)
    conversation_ids = set()
    skipped = unsupported_parts = references_only = 0
    for conversation in conversations:
        conversation_id = _identifier(
            conversation.get("id", conversation.get("conversation_id")), "conversation id"
        )
        if conversation_id in conversation_ids:
            raise PortabilityError(f"Duplicate conversation id: {conversation_id}")
        conversation_ids.add(conversation_id)
        nodes = conversation.get("mapping")
        if not isinstance(nodes, dict) or not nodes:
            raise PortabilityError("Conversation mapping must be a nonempty object")
        for node_id, node in nodes.items():
            _identifier(node_id, "node id")
            if not isinstance(node, dict) or node.get("id", node_id) != node_id:
                raise PortabilityError(f"Invalid ChatGPT node {node_id!r}")
        depths = _validate_tree(nodes, "parent")
        for node_id, node in nodes.items():
            children = node.get("children", [])
            if (
                not isinstance(children, list)
                or any(not isinstance(c, str) for c in children)
                or len(set(children)) != len(children)
            ):
                raise PortabilityError(f"Invalid children for node {node_id!r}")
            for child in children:
                if not isinstance(child, str) or child not in nodes:
                    raise PortabilityError(f"Missing child pointer for node {node_id!r}")
                if nodes[child].get("parent") != node_id:
                    raise PortabilityError(f"Inconsistent child parent for node {child!r}")
        leaf = _identifier(conversation.get("current_node"), "active branch pointer")
        selected = _selected_nodes(nodes, depths, "parent", leaf, all_branches)
        message_ids = set()
        for node_id in sorted(nodes):
            message = nodes[node_id].get("message")
            if message is None:
                continue
            if not isinstance(message, dict):
                raise PortabilityError(f"Invalid message at node {node_id!r}")
            message_id = _identifier(message.get("id", node_id), "message id")
            if message_id in message_ids:
                raise PortabilityError(f"Duplicate message id: {message_id}")
            message_ids.add(message_id)
        for node_id in selected:
            node = nodes[node_id]
            message = node.get("message")
            if message is None:
                skipped += 1
                continue
            author = message.get("author")
            if not isinstance(author, dict):
                raise PortabilityError(f"Invalid author at node {node_id!r}")
            role = _identifier(author.get("role"), "message role")
            text, unsupported, raw = _content(message.get("content"), "chatgpt")
            unsupported_parts += unsupported
            if not text.strip():
                if not unsupported:
                    skipped += 1
                    continue
                text = (
                    "[Unsupported ChatGPT content retained as metadata; assets were not fetched.]"
                )
                references_only += 1
            timestamp = message.get("create_time")
            timestamp_source = "message.create_time"
            if timestamp is None:
                timestamp = conversation.get("create_time")
                timestamp_source = "conversation.create_time"
            metadata = {
                "chatgpt": {
                    "conversation_id": conversation_id,
                    "title": conversation.get("title"),
                    "node_id": node_id,
                    "message_id": message.get("id", node_id),
                    "author": author,
                    "timestamp_source": timestamp_source,
                    "original_create_time": message.get("create_time"),
                    "original_update_time": message.get("update_time"),
                    "branch_depth": depths[node_id],
                    "unsupported_parts": unsupported,
                    **raw,
                }
            }
            episodes.append(
                _episode(
                    {
                        "source_id": f"chatgpt:{conversation_id}:{node_id}",
                        "content": text,
                        "occurred_at": _timestamp(timestamp, f"timestamp for node {node_id!r}"),
                        "source_type": "chatgpt",
                        "session_id": conversation_id,
                        "source_entry_id": node_id,
                        "parent_entry_id": node.get("parent"),
                        "role": role,
                        "source_ref": f"chatgpt:{conversation_id}/{node_id}",
                        "metadata": metadata,
                        "project": True,
                    }
                )
            )
            if len(episodes) > MAX_EPISODES:
                raise PortabilityError("Too many episodes in one import")
    return episodes, {
        "conversations": len(conversations),
        "skipped": skipped,
        "unsupported_parts": unsupported_parts,
        "references_only": references_only,
        "all_branches": all_branches,
    }


def import_chatgpt(
    store: PortableStore,
    principal: Principal,
    world_id: str,
    path: Path,
    all_branches: bool = False,
    *,
    scope_id: UUID | str | None = None,
) -> dict:
    """Import complete native conversations in one administrative transaction."""
    episodes, report = parse_chatgpt(_read_bounded(Path(path)), all_branches)
    if scope_id is not None:
        episodes = [item.model_copy(update={"scope_id": UUID(str(scope_id))}) for item in episodes]
    result = store.restore(
        principal, world_id, episodes, **({"scope_id": scope_id} if scope_id else {})
    )
    return {**result, **report}


def import_pi(
    store: PortableStore,
    principal: Principal,
    world_id: str,
    path: Path,
    all_branches: bool = False,
    leaf_id: str | None = None,
    *,
    scope_id: UUID | str | None = None,
) -> dict:
    """Import Pi JSONL; the last file entry is the default active branch leaf."""
    lines = _read_bounded(Path(path)).splitlines()
    if not lines:
        raise PortabilityError("Pi session is empty")
    records = []
    for index, line in enumerate(lines, 1):
        if len(line) > MAX_RECORD_BYTES:
            raise PortabilityError(f"Pi entry {index} exceeds the record size limit")
        record = _load_json(line, f"Pi line {index}")
        if not isinstance(record, dict):
            raise PortabilityError(f"Pi line {index} must be an object")
        records.append(record)
    header = records[0]
    if header.get("type") != "session":
        raise PortabilityError("First Pi record must be a session header")
    session_id = _identifier(header.get("id"), "Pi session id")
    nodes = {}
    for record in records[1:]:
        if record.get("type") == "session":
            raise PortabilityError("Duplicate Pi session header")
        node_id = _identifier(record.get("id"), "Pi entry id")
        if node_id in nodes:
            raise PortabilityError(f"Duplicate Pi entry id: {node_id}")
        nodes[node_id] = record
    if not nodes:
        if leaf_id is not None:
            raise PortabilityError(f"Missing active branch pointer: {leaf_id!r}")
        return {
            **store.restore(principal, world_id, []),
            "skipped": 0,
            "unsupported_parts": 0,
            "references_only": 0,
            "all_branches": all_branches,
            "leaf_id": None,
        }
    depths = _validate_tree(nodes, "parentId")
    leaf = leaf_id if leaf_id is not None else next(reversed(nodes))
    selected = _selected_nodes(nodes, depths, "parentId", leaf, all_branches)
    episodes = []
    skipped = unsupported_parts = references_only = 0
    for node_id in selected:
        node = nodes[node_id]
        if node.get("type") != "message":
            skipped += 1
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            raise PortabilityError(f"Invalid Pi message at entry {node_id!r}")
        role = _identifier(message.get("role"), "Pi message role")
        if role == "bashExecution":
            if not isinstance(message.get("command"), str) or not isinstance(
                message.get("output"), str
            ):
                raise PortabilityError(f"Invalid Pi shell execution at entry {node_id!r}")
            text = f"$ {message['command']}\n{message['output']}"
            unsupported, raw = 0, {"raw_message": message}
        elif role in {"branchSummary", "compactionSummary"}:
            if not isinstance(message.get("summary"), str):
                raise PortabilityError(f"Invalid Pi summary at entry {node_id!r}")
            text, unsupported, raw = message["summary"], 0, {"raw_message": message}
        elif "content" not in message:
            text, unsupported, raw = "", 1, {"raw_message": message}
        else:
            text, unsupported, raw = _content(message["content"], "pi")
        unsupported_parts += unsupported
        if not text.strip():
            if not unsupported:
                skipped += 1
                continue
            text = "[Unsupported Pi content retained as metadata; assets were not fetched.]"
            references_only += 1
        timestamp = node.get("timestamp")
        timestamp_source = "entry.timestamp"
        milliseconds = False
        if timestamp is None:
            timestamp = message.get("timestamp")
            timestamp_source = "message.timestamp"
            milliseconds = True
        if timestamp is None:
            timestamp = header.get("timestamp")
            timestamp_source = "session.timestamp"
            milliseconds = False
        episodes.append(
            _episode(
                {
                    "source_id": f"pi:{session_id}:{node_id}",
                    "content": text,
                    "occurred_at": _timestamp(
                        timestamp, f"Pi timestamp for entry {node_id!r}", milliseconds=milliseconds
                    ),
                    "source_type": "pi",
                    "session_id": session_id,
                    "source_entry_id": node_id,
                    "parent_entry_id": node.get("parentId"),
                    "role": role,
                    "source_ref": f"pi:{session_id}/{node_id}",
                    "metadata": {
                        "pi": {
                            "session_version": header.get("version"),
                            "entry_id": node_id,
                            "branch_depth": depths[node_id],
                            "timestamp_source": timestamp_source,
                            "original_timestamp": message.get("timestamp"),
                            "unsupported_parts": unsupported,
                            **raw,
                        }
                    },
                    "project": True,
                }
            )
        )
        if len(episodes) > MAX_EPISODES:
            raise PortabilityError("Too many episodes in one import")
    if scope_id is not None:
        episodes = [item.model_copy(update={"scope_id": UUID(str(scope_id))}) for item in episodes]
    result = store.restore(
        principal, world_id, episodes, **({"scope_id": scope_id} if scope_id else {})
    )
    return {
        **result,
        "skipped": skipped,
        "unsupported_parts": unsupported_parts,
        "references_only": references_only,
        "all_branches": all_branches,
        "leaf_id": leaf,
    }
