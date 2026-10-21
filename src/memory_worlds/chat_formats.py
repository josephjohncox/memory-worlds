"""Strict adapters for observed Claude exports and opaque transcript documents.

Claude's observed consumer shape is documented by khive's current parser
(7671729911951d42f8d16f236dd705dcedc8bacc, mirror/parse.rs). Richlow's
Markdown writer is pinned at c6d6096a214d64ad563b382c034ad2f765c3682e.
Neither provider publishes these file formats as a versioned schema. These
adapters preserve evidence without claiming to reconstruct discarded topology.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import EpisodeInput
from .portability import (
    MAX_EPISODES,
    MAX_INPUT_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    EpisodeBuffer,
    PortabilityError,
    _episode,
    _identifier,
    _load_json,
    _timestamp,
)

_MINUTE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}\Z")
_DOCUMENT_CHARS = 200_000
_RICHLOW_ROLE = re.compile(
    r"^(?:🧑 \*\*You\*\*|🤖 \*\*ChatGPT\*\*)(?: _\d{4}-\d{2}-\d{2} \d{2}:\d{2}_)?\r?$",
    re.MULTILINE,
)
_RICHLOW_WARNING = (
    "Markdown document snapshot: original message IDs, timezone, attachments and branch "
    "topology are unavailable; the exporter follows the first child, not the active branch."
)


def _bounded(data: bytes) -> None:
    if not isinstance(data, bytes) or len(data) > MAX_INPUT_BYTES:
        raise PortabilityError(f"Input must be bytes of at most {MAX_INPUT_BYTES} bytes")


def _text(data: bytes) -> str:
    _bounded(data)
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PortabilityError("Document must be UTF-8 text") from exc
    if not value.strip():
        raise PortabilityError("Document cannot be empty")
    return value


def _aware_iso(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise PortabilityError(f"Invalid {label}: an aware ISO timestamp is required")
    return _timestamp(value, label)


def _stats(conversations: int = 1) -> dict:
    return {
        "conversations": conversations,
        "skipped": 0,
        "unsupported_parts": 0,
        "references_only": 0,
    }


def _snapshot(
    text: str,
    source_id: str,
    occurred_at: datetime,
    metadata: dict,
    *,
    max_episodes: int,
    max_bytes: int,
) -> tuple[list[EpisodeInput], list[str]]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _DOCUMENT_CHARS, len(text))
        part = text[start:end]
        if not part.strip():
            raise PortabilityError(
                "Document whitespace run cannot fit losslessly in nonblank snapshot chunks"
            )
        if end < len(text) and not text[end : end + _DOCUMENT_CHARS].strip():
            # Move the last visible character into the next segment rather than
            # manufacturing text for a whitespace-only tail or segment.
            end = start + len(part.rstrip()) - 1
            part = text[start:end]
            if not part.strip():
                raise PortabilityError(
                    "Document whitespace run cannot fit losslessly in nonblank snapshot chunks"
                )
        chunks.append(part)
        if len(chunks) > max_episodes:
            raise PortabilityError("Too many document chunks in one import")
        start = end
    multiple = len(chunks) > 1
    episodes = EpisodeBuffer(max_episodes=max_episodes, max_bytes=max_bytes)
    for index, content in enumerate(chunks, 1):
        episodes.append(
            _episode(
                {
                    "source_id": f"{source_id}:chunk:{index}" if multiple else source_id,
                    "source_ref": source_id,
                    "content": content,
                    "occurred_at": occurred_at,
                    "source_type": "text",
                    "metadata": {
                        **metadata,
                        **(
                            {"document_chunk": {"index": index, "count": len(chunks)}}
                            if multiple
                            else {}
                        ),
                    },
                    **(
                        {"session_id": source_id, "source_entry_id": f"chunk:{index}"}
                        if multiple
                        else {}
                    ),
                }
            )
        )
    warnings = (
        [
            f"Document split into {len(chunks)} contiguous snapshot chunks; "
            "chunks are not message turns."
        ]
        if multiple
        else []
    )
    return episodes, warnings


def _claude_content(message: dict) -> tuple[str, int, dict]:
    parts: list[str] = []
    unsupported = 0
    raw: dict = {}
    if "content" in message:
        blocks = message["content"]
        if not isinstance(blocks, list):
            raise PortabilityError("Claude content must be an array of blocks")
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise PortabilityError("Claude text block must contain a text string")
                parts.append(block["text"])
            else:
                unsupported += 1
        if unsupported:
            raw["raw_content"] = blocks
    if "text" in message:
        text = message["text"]
        if not isinstance(text, str):
            raise PortabilityError("Claude message text must be a string")
        if text and text not in parts and text != "\n".join(parts):
            parts.append(text)
    for key in ("attachments", "files"):
        if key in message:
            if not isinstance(message[key], list):
                raise PortabilityError(f"Claude {key} must be an array")
            if message[key]:
                raw[key] = message[key]
                unsupported += len(message[key])
    return "\n".join(parts), unsupported, raw


def parse_claude(
    data: bytes,
    *,
    max_episodes: int = MAX_EPISODES,
    max_bytes: int = MAX_UNCOMPRESSED_BYTES,
) -> tuple[list[EpisodeInput], dict]:
    """Read observed native Claude fields; keep messages independent of array order."""
    _bounded(data)
    value = _load_json(data, "Claude export")
    conversations = value if isinstance(value, list) else [value]
    if not conversations or any(not isinstance(item, dict) for item in conversations):
        raise PortabilityError("Claude export must contain conversation objects")
    episodes = EpisodeBuffer(max_episodes=max_episodes, max_bytes=max_bytes)
    stats = _stats(len(conversations))
    conversation_ids: set[str] = set()
    for conversation in conversations:
        conversation_id = _identifier(conversation.get("uuid"), "Claude conversation uuid")
        if conversation_id in conversation_ids:
            raise PortabilityError(f"Duplicate Claude conversation uuid: {conversation_id}")
        conversation_ids.add(conversation_id)
        title = conversation.get("name")
        if title is not None and not isinstance(title, str):
            raise PortabilityError("Claude conversation name must be a string")
        messages = conversation.get("chat_messages")
        if not isinstance(messages, list):
            raise PortabilityError("Claude chat_messages must be an array")
        created = conversation.get("created_at")
        if created is not None:
            _aware_iso(created, "Claude conversation.created_at")
        message_ids: set[str] = set()
        for message in messages:
            if not isinstance(message, dict):
                raise PortabilityError("Claude messages must be objects")
            message_id = _identifier(message.get("uuid"), "Claude message uuid")
            if message_id in message_ids:
                raise PortabilityError(f"Duplicate Claude message uuid: {message_id}")
            message_ids.add(message_id)
            sender = message.get("sender")
            if sender not in ("human", "assistant"):
                raise PortabilityError("Claude sender must be human or assistant")
            timestamp = message.get("created_at")
            timestamp_source = "message.created_at"
            if timestamp is None:
                timestamp, timestamp_source = created, "conversation.created_at"
            occurred_at = _aware_iso(timestamp, f"Claude timestamp for message {message_id!r}")
            text, unsupported, raw = _claude_content(message)
            stats["unsupported_parts"] += unsupported
            if not text.strip():
                if not unsupported:
                    stats["skipped"] += 1
                    continue
                text = "[Unsupported Claude content retained as metadata; assets were not fetched.]"
                stats["references_only"] += 1
            metadata = {
                "conversation_id": conversation_id,
                "title": title,
                "message_id": message_id,
                "timestamp_source": timestamp_source,
                "original_create_time": message.get("created_at"),
                "fidelity": "source-messages; branch selection unavailable",
                "unsupported_parts": unsupported,
                **raw,
            }
            for key in ("parent_message_uuid", "index"):
                if key in message:
                    metadata[key] = message[key]
            source_id = f"claude:{conversation_id}:{message_id}"
            episodes.append(
                _episode(
                    {
                        "source_id": source_id,
                        "content": text,
                        "occurred_at": occurred_at,
                        "source_type": "claude",
                        "session_id": conversation_id,
                        "source_entry_id": message_id,
                        "role": "user" if sender == "human" else "assistant",
                        "source_ref": source_id,
                        "metadata": {"claude": metadata},
                    }
                )
            )
    stats["warnings"] = [
        "Claude messages imported individually; no linear turn chain or active branch was inferred."
        " Message parent/index fields are retained only as metadata where supplied."
    ]
    return episodes, stats


def _minute(value: str, label: str) -> datetime | None:
    if value in ("", "Unknown"):
        return None
    if not _MINUTE.fullmatch(value):
        raise PortabilityError(f"Invalid richlow {label}: expected YYYY-MM-DD HH:MM or Unknown")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise PortabilityError(f"Invalid richlow {label}: {exc}") from exc


def _zoned_minute(value: datetime, timezone: str | None) -> datetime:
    if not isinstance(timezone, str) or not timezone:
        raise PortabilityError("Richlow naive timestamps require an explicit IANA timezone")
    try:
        zone = ZoneInfo(timezone)
        choices = set()
        for fold in (0, 1):
            candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == value:
                choices.add(candidate)
    except (ZoneInfoNotFoundError, ValueError, OverflowError) as exc:
        raise PortabilityError(f"Invalid richlow timezone or date: {exc}") from exc
    if not choices:
        raise PortabilityError("Richlow timestamp is nonexistent in the supplied timezone")
    if len(choices) != 1:
        raise PortabilityError("Richlow timestamp is ambiguous in the supplied timezone")
    return choices.pop()


def parse_richlow(
    data: bytes,
    *,
    timezone: str | None = None,
    fallback_timestamp: str | None = None,
    max_episodes: int = MAX_EPISODES,
    max_bytes: int = MAX_UNCOMPRESSED_BYTES,
) -> tuple[list[EpisodeInput], dict]:
    """Preserve one richlow Markdown file whole: its unescaped body has no safe turn parser."""
    text = _text(data)
    # Only the seven-line writer envelope needs line objects. A transcript may
    # contain millions of newlines; its body remains one opaque bounded string.
    envelope = text.split("\n", 7)
    lines = [line.removesuffix("\r") for line in envelope[:7]]
    if (
        len(envelope) < 8
        or not lines[0].startswith("# ")
        or not lines[0][2:].strip()
        or lines[1] != ""
        or not lines[2].startswith("**Created:** ")
        or not lines[3].startswith("**Last updated:** ")
        or lines[4:7] != ["", "---", ""]
        or not _RICHLOW_ROLE.search(envelope[7])
    ):
        raise PortabilityError("Invalid richlow Markdown transcript header or role labels")
    created = _minute(lines[2].removeprefix("**Created:** ").rstrip(), "Created")
    _minute(lines[3].removeprefix("**Last updated:** ").rstrip(), "Last updated")
    if created is None:
        occurred_at = _aware_iso(fallback_timestamp, "richlow fallback timestamp")
        timestamp_source = "fallback_timestamp"
    else:
        occurred_at = _zoned_minute(created, timezone)
        timestamp_source = "header.Created"
    source_id = f"richlow-md:{hashlib.sha256(data).hexdigest()}"
    episodes, warnings = _snapshot(
        text,
        source_id,
        occurred_at,
        {
            "richlow": {
                "title": lines[0][2:],
                "fidelity": "document-snapshot; upstream message boundaries and topology lost",
                "timezone": timezone if created is not None else None,
                "timestamp_source": timestamp_source,
            }
        },
        max_episodes=max_episodes,
        max_bytes=max_bytes,
    )
    return episodes, {**_stats(), "warnings": [_RICHLOW_WARNING, *warnings]}


def parse_document(
    data: bytes,
    *,
    fallback_timestamp: str | None = None,
    max_episodes: int = MAX_EPISODES,
    max_bytes: int = MAX_UNCOMPRESSED_BYTES,
) -> tuple[list[EpisodeInput], dict]:
    """Import an explicitly selected UTF-8 text/Markdown document without inventing turns."""
    text = _text(data)
    source_id = f"document:{hashlib.sha256(data).hexdigest()}"
    episodes, warnings = _snapshot(
        text,
        source_id,
        _aware_iso(fallback_timestamp, "document fallback timestamp"),
        {"document": {"fidelity": "document-snapshot", "timestamp_source": "fallback_timestamp"}},
        max_episodes=max_episodes,
        max_bytes=max_bytes,
    )
    return episodes, {
        **_stats(),
        "warnings": ["Opaque document; no message roles or topology inferred.", *warnings],
    }
