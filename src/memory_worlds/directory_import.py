"""Bounded, write-free planning of already extracted conversation directories."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .models import MAX_IMPORT_BYTES, MAX_IMPORT_EPISODES, EpisodeInput
from .portability import MAX_INPUT_BYTES, PortabilityError, _json, _load_json, parse_chatgpt

MAX_DIRECTORY_ENTRIES = 20_000
MAX_DIRECTORY_DEPTH = 16
MAX_REPORT_FILES = 200
FORMATS = ("auto", "chatgpt", "claude", "richlow-md", "documents")
_PREVIEW_SCOPE = UUID(int=0)


@dataclass(frozen=True)
class ImportPlan:
    episodes: list[EpisodeInput]
    report: dict

    def payload(self, scope_id: UUID | str) -> dict:
        scope = UUID(str(scope_id))
        payload = {
            "episodes": [
                item.model_copy(update={"scope_id": scope}).model_dump(mode="json")
                for item in self.episodes
            ]
        }
        if len(_json(payload)) > MAX_IMPORT_BYTES:
            raise PortabilityError("Import payload exceeds 32 MiB; select a smaller directory")
        return payload


def _regular_bytes(directory_fd: int, name: str, expected: os.stat_result, remaining: int) -> bytes:
    """Bind reads to a regular descriptor without following a replaced path."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise PortabilityError("Input changed during directory scan")
        if before.st_size > remaining:
            raise PortabilityError("Selected input exceeds the 128 MiB directory budget")
        chunks = []
        size = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, remaining - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > remaining:
                raise PortabilityError("Selected input exceeds the 128 MiB directory budget")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or size != after.st_size:
            raise PortabilityError("Input changed while being read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _detect(data: bytes, suffix: str) -> str | None:
    if suffix == ".json":
        value = _load_json(data, "directory JSON")
        records = value if isinstance(value, list) else [value]
        known = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            if "mapping" in record or "current_node" in record:
                known.add("chatgpt")
            if "chat_messages" in record:
                known.add("claude")
        if len(known) > 1:
            raise PortabilityError("A JSON file mixes ChatGPT and Claude conversation schemas")
        return next(iter(known), None)
    if suffix == ".md":
        # Recognition is deliberately narrower than arbitrary Markdown transcripts.
        prefix = data[:8192].decode("utf-8", errors="replace").splitlines()
        if (
            prefix
            and prefix[0].startswith("# ")
            and any(line.startswith("**Created:**") for line in prefix[1:5])
        ):
            return "richlow-md"
    return None


def _parse(data: bytes, kind: str, *, all_branches, timezone, fallback_timestamp):
    from .chat_formats import parse_claude, parse_document, parse_richlow

    if kind == "chatgpt":
        return parse_chatgpt(
            data,
            all_branches=all_branches,
            max_episodes=MAX_IMPORT_EPISODES,
            max_bytes=MAX_IMPORT_BYTES,
        )
    if kind == "claude":
        return parse_claude(data, max_episodes=MAX_IMPORT_EPISODES, max_bytes=MAX_IMPORT_BYTES)
    if kind == "richlow-md":
        return parse_richlow(
            data,
            timezone=timezone,
            fallback_timestamp=fallback_timestamp,
            max_episodes=MAX_IMPORT_EPISODES,
            max_bytes=MAX_IMPORT_BYTES,
        )
    return parse_document(
        data,
        fallback_timestamp=fallback_timestamp,
        max_episodes=MAX_IMPORT_EPISODES,
        max_bytes=MAX_IMPORT_BYTES,
    )


def _diagnostic(error: PortabilityError) -> str:
    """Keep actionable date failures without echoing values from imported text."""
    message = str(error)
    if "explicit IANA timezone" in message:
        return "Rendered dates require --source-timezone with an explicit IANA timezone"
    if "ambiguous in the supplied timezone" in message:
        return "Rendered date is ambiguous in the supplied timezone"
    if "nonexistent in the supplied timezone" in message:
        return "Rendered date is nonexistent in the supplied timezone"
    if "fallback timestamp" in message:
        return "An explicit aware --fallback-timestamp is required for undated documents"
    if "timezone or date" in message:
        return "Invalid source timezone or rendered date"
    if "Too many episodes" in message:
        return "File exceeds the episode count limit"
    if "encoded output byte limit" in message:
        return "Import exceeds 32 MiB; select a smaller directory"
    return "Format validation failed; check identifiers, structure, content limits and timestamps"


def plan_directory(
    directory: Path,
    *,
    format: str = "auto",
    all_branches: bool = False,
    timezone: str | None = None,
    fallback_timestamp: str | None = None,
) -> ImportPlan:
    """Parse immutable file bytes before any service calls or memory writes.

    Hidden entries are excluded. All other links and special files are rejected,
    even when their extension would not be imported. Paths appear only in this
    local report; native message identities and payloads do not depend on paths.
    """
    if format not in FORMATS:
        raise PortabilityError("Unknown import format")
    episodes: dict[str, EpisodeInput] = {}
    digests: dict[str, bytes] = {}
    report = {
        "files": [],
        "omitted_files": 0,
        "scanned_files": 0,
        "skipped_files": 0,
        "selected_files": 0,
        "episodes": 0,
        "deduplicated": 0,
        "formats": {},
        "conversations": 0,
        "unsupported_parts": 0,
        "references_only": 0,
        "skipped": 0,
        "warnings": [],
    }
    entries_seen = total_bytes = output_bytes = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK

    def visit(fd: int, relative: Path, depth: int):
        nonlocal entries_seen, total_bytes, output_bytes
        if depth > MAX_DIRECTORY_DEPTH:
            raise PortabilityError("Directory nesting exceeds 16 levels")
        entries = []
        with os.scandir(fd) as iterator:
            for entry in iterator:
                entries_seen += 1
                if entries_seen > MAX_DIRECTORY_ENTRIES:
                    raise PortabilityError("Directory exceeds 20,000 entries")
                entries.append(entry.name)
        for name in sorted(entries):
            if name.startswith("."):
                report["skipped_files"] += 1
                continue
            path = relative / name
            label = path.as_posix()
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, directory_flags, dir_fd=fd)
                try:
                    child_info = os.fstat(child_fd)
                    if (info.st_dev, info.st_ino) != (child_info.st_dev, child_info.st_ino):
                        raise PortabilityError("Directory changed during scan")
                    visit(child_fd, path, depth + 1)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise PortabilityError(f"Links and special files are not importable: {label!r}")
            report["scanned_files"] += 1
            suffix = path.suffix.casefold()
            candidate = suffix in ((".md", ".txt") if format == "documents" else (".json", ".md"))
            if (
                not candidate
                or (format in {"chatgpt", "claude"} and suffix != ".json")
                or (format == "richlow-md" and suffix != ".md")
            ):
                report["skipped_files"] += 1
                continue
            data = _regular_bytes(fd, name, info, MAX_INPUT_BYTES - total_bytes)
            total_bytes += len(data)
            try:
                kind = _detect(data, suffix) if format == "auto" else format
                if kind is None:
                    report["skipped_files"] += 1
                    continue
                records, stats = _parse(
                    data,
                    kind,
                    all_branches=all_branches,
                    timezone=timezone,
                    fallback_timestamp=fallback_timestamp,
                )
            except PortabilityError as exc:
                raise PortabilityError(
                    f"Invalid import file {label!r}: {_diagnostic(exc)}"
                ) from None
            report["selected_files"] += 1
            report["formats"][kind] = report["formats"].get(kind, 0) + 1
            for key in ("conversations", "unsupported_parts", "references_only", "skipped"):
                report[key] += stats.get(key, 0)
            for warning in stats.get("warnings", []):
                if warning not in report["warnings"]:
                    report["warnings"].append(warning)
            if len(report["files"]) < MAX_REPORT_FILES:
                report["files"].append(
                    {
                        "path": label,
                        "format": kind,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "episodes": len(records),
                    }
                )
            else:
                report["omitted_files"] += 1
            for episode in records:
                encoded = _json(episode)
                digest = hashlib.sha256(encoded).digest()
                if episode.source_id in episodes:
                    if digests[episode.source_id] != digest:
                        raise PortabilityError(
                            "Conflicting records share one native source identity"
                        )
                    report["deduplicated"] += 1
                    continue
                episodes[episode.source_id] = episode
                digests[episode.source_id] = digest
                output_bytes += len(encoded)
                if output_bytes > MAX_IMPORT_BYTES:
                    raise PortabilityError("Import exceeds 32 MiB; select a smaller directory")
                if len(episodes) > MAX_IMPORT_EPISODES:
                    raise PortabilityError(
                        "Import exceeds 10,000 episodes; select a smaller directory"
                    )

    try:
        root_fd = os.open(Path(directory), directory_flags)
        try:
            visit(root_fd, Path(), 0)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise PortabilityError(
            f"Directory cannot be read safely ({exc.strerror}); check regular files and permissions"
        ) from None
    if not episodes:
        raise PortabilityError("Directory contains no importable conversations or documents")
    if {"chatgpt", "claude"}.intersection(report["formats"]) and {
        "richlow-md",
        "documents",
    }.intersection(report["formats"]):
        report["warnings"].append(
            "Native messages and rendered snapshots may overlap; "
            "select --format to avoid duplicates."
        )
    report["episodes"] = len(episodes)
    plan = ImportPlan(episodes=list(episodes.values()), report=report)
    report["payload_bytes"] = len(_json(plan.payload(_PREVIEW_SCOPE)))
    return plan
