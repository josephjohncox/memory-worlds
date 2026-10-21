"""Synthetic fixtures derived from pinned format writers/readers, never account exports."""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from memory_worlds import chat_formats
from memory_worlds.chat_formats import parse_claude, parse_document, parse_richlow
from memory_worlds.portability import PortabilityError

STAMP = "2026-01-02T03:04:05Z"


def _claude():
    # Observed field names: khive 7671729 mirror/parse.rs; all values are synthetic.
    return {
        "uuid": "synthetic-conversation",
        "name": "Synthetic export",
        "created_at": STAMP,
        "chat_messages": [
            {
                "uuid": "synthetic-human",
                "sender": "human",
                "created_at": "2026-01-02T04:04:05+01:00",
                "text": "Use a queue.",
                "content": [{"type": "text", "text": "Use a queue."}],
            },
            {
                "uuid": "synthetic-assistant",
                "sender": "assistant",
                "text": "The queue buffers bursts.",
            },
        ],
    }


def _json(value):
    return json.dumps(value, ensure_ascii=False).encode()


def _richlow(created="2026-01-02 03:04", updated="2026-01-03 05:06", body=None):
    # Exact writer shape: richlow c6d6096 conversation_to_markdown; no copied chat.
    return (
        "# Synthetic export\n\n"
        f"**Created:** {created}  \n"
        f"**Last updated:** {updated}\n\n---\n\n"
        + (body or "🧑 **You** _2026-01-02 03:04_\n\nUse a queue.\n\n---\n\n")
    ).encode()


def test_native_claude_identity_roles_timestamp_and_provenance():
    episodes, stats = parse_claude(_json([_claude()]))
    assert stats["conversations"] == 1 and stats["unsupported_parts"] == 0
    assert [episode.role for episode in episodes] == ["user", "assistant"]
    assert episodes[0].content == "Use a queue."
    assert episodes[1].content == "The queue buffers bursts."
    assert episodes[0].source_id == "claude:synthetic-conversation:synthetic-human"
    assert episodes[0].source_ref == episodes[0].source_id
    assert episodes[0].source_type == "claude"
    assert episodes[0].session_id == "synthetic-conversation"
    assert episodes[0].source_entry_id == "synthetic-human"
    assert all(episode.parent_entry_id is None for episode in episodes)
    assert episodes[0].occurred_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert episodes[0].metadata["claude"]["timestamp_source"] == "message.created_at"
    assert episodes[1].metadata["claude"]["timestamp_source"] == "conversation.created_at"
    # Reordering an export does not change an identified message's source payload.
    reverse = _claude()
    reverse["chat_messages"].reverse()
    reordered, _ = parse_claude(_json(reverse))
    assert reordered[::-1] == episodes


def test_claude_distinct_final_text_and_unsupported_evidence_are_retained():
    data = _claude()
    message = data["chat_messages"][0]
    message["content"] += [
        {"type": "text", "text": "Second text block."},
        {"type": "image", "source": {"url": "https://example.invalid/private-image"}},
        {"type": "thinking", "thinking": "Synthetic internal block"},
    ]
    message["text"] = "Separate final answer."
    message["attachments"] = [
        {"file_name": "synthetic.pdf", "extracted_content": "Original upload"}
    ]
    message["files"] = [{"uuid": "synthetic-file", "file_name": "diagram.png"}]
    episodes, stats = parse_claude(_json(data))
    assert episodes[0].content == "Use a queue.\nSecond text block.\nSeparate final answer."
    metadata = episodes[0].metadata["claude"]
    assert metadata["raw_content"] == message["content"]
    assert metadata["attachments"] == message["attachments"]
    assert metadata["files"] == message["files"]
    assert stats["unsupported_parts"] == 4
    assert stats["references_only"] == 0


def test_claude_nontext_only_and_empty_messages_have_explicit_stats():
    data = _claude()
    data["chat_messages"][0].update(text="", content=[{"type": "tool_use", "name": "synthetic"}])
    data["chat_messages"][1].update(text="", content=[])
    episodes, stats = parse_claude(_json(data))
    assert len(episodes) == 1
    assert "retained as metadata" in episodes[0].content
    assert stats["skipped"] == stats["references_only"] == stats["unsupported_parts"] == 1


def test_claude_top_level_aggregated_text_does_not_duplicate_text_blocks():
    data = _claude()
    data["chat_messages"][0].update(
        content=[{"type": "text", "text": "First."}, {"type": "text", "text": "Second."}],
        text="First.\nSecond.",
    )
    assert parse_claude(_json(data))[0][0].content == "First.\nSecond."


def test_claude_topology_is_evidence_without_an_invented_chain_or_selection():
    data = _claude()
    data["current_leaf_message_uuid"] = "synthetic-assistant"
    data["chat_messages"][1].update(parent_message_uuid="synthetic-human", index=1)
    alternative = deepcopy(data["chat_messages"][1])
    alternative.update(uuid="synthetic-alternative", text="Alternative answer.", index=2)
    data["chat_messages"].append(alternative)
    episodes, stats = parse_claude(_json(data))
    assert len(episodes) == 3 and all(episode.parent_entry_id is None for episode in episodes)
    assert episodes[1].metadata["claude"]["parent_message_uuid"] == "synthetic-human"
    assert "current_leaf_message_uuid" not in episodes[2].metadata["claude"]
    assert "retained only as metadata" in stats["warnings"][0]


def test_claude_append_and_active_leaf_change_preserve_existing_message_payloads():
    data = _claude()
    data["current_leaf_message_uuid"] = "synthetic-human"
    episodes, _ = parse_claude(_json(data))
    data["current_leaf_message_uuid"] = "synthetic-new"
    data["chat_messages"].append(
        {"uuid": "synthetic-new", "sender": "human", "created_at": STAMP, "text": "Next."}
    )
    appended, _ = parse_claude(_json(data))
    assert appended[:-1] == episodes


@pytest.mark.parametrize("field", ["uuid", "chat_messages"])
def test_claude_missing_conversation_fields_fail(field):
    data = _claude()
    del data[field]
    with pytest.raises(PortabilityError):
        parse_claude(_json(data))


@pytest.mark.parametrize("field", ["uuid", "sender"])
def test_claude_missing_message_fields_fail_even_for_empty_messages(field):
    data = _claude()
    data["chat_messages"][0].update(text="", content=[])
    del data["chat_messages"][0][field]
    with pytest.raises(PortabilityError):
        parse_claude(_json(data))


@pytest.mark.parametrize(
    ("field", "value"),
    [("sender", "user"), ("text", None), ("content", {}), ("attachments", {}), ("files", {})],
)
def test_claude_malformed_known_fields_do_not_fall_back(field, value):
    data = _claude()
    data["chat_messages"][0][field] = value
    with pytest.raises(PortabilityError):
        parse_claude(_json(data))


@pytest.mark.parametrize(
    "date", ["not-a-date", "2026-01-02", "2026-01-02T03:04:05", 1767312000, True]
)
def test_claude_invalid_present_message_timestamp_never_uses_valid_conversation_date(date):
    data = _claude()
    data["chat_messages"][0]["created_at"] = date
    with pytest.raises(PortabilityError):
        parse_claude(_json(data))


def test_claude_null_message_timestamp_uses_conversation_date_but_invalid_conversation_fails():
    data = _claude()
    data["chat_messages"][0]["created_at"] = None
    assert (
        parse_claude(_json(data))[0][0].metadata["claude"]["timestamp_source"]
        == "conversation.created_at"
    )
    data["created_at"] = "bad"
    with pytest.raises(PortabilityError):
        parse_claude(_json(data))


def test_claude_duplicate_conversation_message_and_json_keys_fail():
    data = _claude()
    with pytest.raises(PortabilityError, match="Duplicate Claude conversation"):
        parse_claude(_json([data, data]))
    data["chat_messages"].append(deepcopy(data["chat_messages"][0]))
    with pytest.raises(PortabilityError, match="Duplicate Claude message"):
        parse_claude(_json(data))
    with pytest.raises(PortabilityError, match="Duplicate JSON key"):
        parse_claude(b'{"uuid":"one","uuid":"two","chat_messages":[]}')


def test_claude_repeated_metadata_is_bounded_while_parsing(monkeypatch):
    data = _claude()
    data["name"] = "Synthetic long title " + "x" * 4096
    template = data["chat_messages"][0]
    data["chat_messages"] = [{**template, "uuid": f"synthetic-{index}"} for index in range(100)]
    constructed = 0
    original = chat_formats._episode

    def counted(value):
        nonlocal constructed
        constructed += 1
        return original(value)

    monkeypatch.setattr(chat_formats, "_episode", counted)
    with pytest.raises(PortabilityError, match="budget|exceed"):
        parse_claude(_json(data), max_bytes=12_000)
    assert constructed <= 3, "Repeated metadata must not accumulate the whole amplified export"


def test_claude_explicit_episode_budget_is_enforced_during_append():
    with pytest.raises(PortabilityError, match="episodes|budget"):
        parse_claude(_json(_claude()), max_episodes=1)


@pytest.mark.parametrize("data", [b"[]", b"null", b"{}", b"[5]", b'{"messages":[]}', b"not json"])
def test_claude_unknown_shapes_are_rejected(data):
    with pytest.raises(PortabilityError):
        parse_claude(data)


def test_richlow_is_one_opaque_document_including_fenced_role_lookalikes():
    body = (
        "🧑 **You** _2026-01-02 03:04_\n\nRetain this example:\n\n```markdown\n"
        "---\n\n🤖 **ChatGPT** _2026-01-02 03:05_\n\nThis is quoted code.\n```\n\n---\n\n"
    )
    raw = _richlow(body=body)
    episodes, stats = parse_richlow(raw, timezone="America/Los_Angeles", fallback_timestamp=STAMP)
    assert len(episodes) == 1 and episodes[0].content == raw.decode()
    assert episodes[0].occurred_at == datetime(2026, 1, 2, 11, 4, tzinfo=UTC)
    assert episodes[0].source_id == f"richlow-md:{hashlib.sha256(raw).hexdigest()}"
    assert episodes[0].source_ref == episodes[0].source_id
    assert episodes[0].role is episodes[0].session_id is episodes[0].source_entry_id is None
    metadata = episodes[0].metadata["richlow"]
    assert set(metadata) == {"title", "fidelity", "timezone", "timestamp_source"}
    assert metadata["timestamp_source"] == "header.Created"
    assert "first child" in stats["warnings"][0]


@pytest.mark.parametrize("date", ["2026-11-01 01:30", "2026-03-08 02:30"])
def test_richlow_dst_ambiguity_and_nonexistent_time_fail_even_with_fallback(date):
    with pytest.raises(PortabilityError, match="ambiguous|nonexistent"):
        parse_richlow(
            _richlow(created=date), timezone="America/Los_Angeles", fallback_timestamp=STAMP
        )


@pytest.mark.parametrize("timezone", [None, "", "Invented/Zone", "../UTC"])
def test_richlow_naive_timestamp_requires_valid_explicit_zone(timezone):
    with pytest.raises(PortabilityError):
        parse_richlow(_richlow(), timezone=timezone, fallback_timestamp=STAMP)


@pytest.mark.parametrize("created", ["Unknown", ""])
def test_richlow_unknown_or_missing_created_requires_explicit_aware_fallback(created):
    raw = _richlow(created=created, updated="Unknown")
    with pytest.raises(PortabilityError):
        parse_richlow(raw)
    episodes, _ = parse_richlow(raw, fallback_timestamp=STAMP)
    assert episodes[0].occurred_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert episodes[0].metadata["richlow"]["timestamp_source"] == "fallback_timestamp"
    assert episodes[0].metadata["richlow"]["timezone"] is None
    assert parse_richlow(raw, timezone="Asia/Tokyo", fallback_timestamp=STAMP)[0] == episodes


@pytest.mark.parametrize("created", ["2026-02-30 01:00", "2026-01-02T03:04:05Z", "not a date"])
def test_richlow_malformed_header_date_never_falls_back(created):
    with pytest.raises(PortabilityError):
        parse_richlow(_richlow(created=created), timezone="UTC", fallback_timestamp=STAMP)


def test_richlow_invalid_updated_or_missing_roles_fail():
    with pytest.raises(PortabilityError):
        parse_richlow(_richlow(updated="bad"), timezone="UTC")
    with pytest.raises(PortabilityError):
        parse_richlow(_richlow(body="Ordinary Markdown without exporter roles."), timezone="UTC")


def test_document_identity_is_bytes_only_and_text_is_preserved_without_inference():
    raw = b"# Synthetic chat\r\n\r\n**User:** this is document text.\r\n"
    episodes, stats = parse_document(raw, fallback_timestamp=STAMP)
    episode = episodes[0]
    assert episode.source_id == f"document:{hashlib.sha256(raw).hexdigest()}"
    assert episode.source_ref == episode.source_id and episode.content == raw.decode()
    assert episode.role is episode.session_id is episode.parent_entry_id is None
    assert "document-snapshot" in episode.metadata["document"]["fidelity"]
    assert stats["conversations"] == 1
    assert (
        parse_document(raw + b"\n", fallback_timestamp=STAMP)[0][0].source_id != episode.source_id
    )


@pytest.mark.parametrize("kind", ["richlow", "document"])
def test_long_snapshot_chunks_are_lossless_stable_and_have_no_turn_topology(kind):
    body = "🧑 **You**\n\n" + "A synthetic long answer. 🧪\n" * 20_000 + "\n---\n\n"
    raw = _richlow(body=body) if kind == "richlow" else body.encode()
    parse = parse_richlow if kind == "richlow" else parse_document
    options = {"timezone": "UTC"} if kind == "richlow" else {"fallback_timestamp": STAMP}
    episodes, stats = parse(raw, **options)
    assert len(episodes) > 1
    assert "".join(episode.content for episode in episodes) == raw.decode()
    prefix = "richlow-md" if kind == "richlow" else "document"
    identity = f"{prefix}:{hashlib.sha256(raw).hexdigest()}"
    for index, episode in enumerate(episodes, 1):
        assert 0 < len(episode.content) <= 200_000
        assert episode.source_id == f"{identity}:chunk:{index}"
        assert episode.source_ref == episode.session_id == identity
        assert episode.source_entry_id == f"chunk:{index}"
        assert episode.parent_entry_id is episode.role is None
        assert episode.metadata["document_chunk"] == {"index": index, "count": len(episodes)}
    assert parse(raw, **options)[0] == episodes
    assert any("not message turns" in warning for warning in stats["warnings"])


def test_document_short_identity_unchanged_and_blank_tail_rebalanced_losslessly():
    raw = ("x" * 200_000 + "\n").encode()
    episodes, _ = parse_document(raw, fallback_timestamp=STAMP)
    assert len(episodes) == 2
    assert "".join(item.content for item in episodes) == raw.decode()
    assert all(item.content.strip() and len(item.content) <= 200_000 for item in episodes)
    short, _ = parse_document(b"x", fallback_timestamp=STAMP)
    assert "document_chunk" not in short[0].metadata
    assert short[0].session_id is short[0].source_entry_id is None


def test_document_unrepresentable_whitespace_run_fails_without_invented_content():
    raw = ("x" + " " * 400_000 + "y").encode()
    with pytest.raises(PortabilityError, match="whitespace run"):
        parse_document(raw, fallback_timestamp=STAMP)


def test_richlow_newline_heavy_body_is_opaque_and_crlf_is_preserved():
    raw = _richlow(body="🧑 **You**\n\n" + "x\n" * 500_000).replace(b"\n", b"\r\n")
    episodes, _ = parse_richlow(raw, timezone="UTC")
    assert "".join(item.content for item in episodes) == raw.decode()
    assert len(episodes) > 1


@pytest.mark.parametrize("kind", ["richlow", "document"])
def test_opaque_snapshot_output_budget_stops_during_episode_construction(kind, monkeypatch):
    body = "🧑 **You**\n\n" + "x" * 500_000
    raw = _richlow(body=body) if kind == "richlow" else body.encode()
    parse = parse_richlow if kind == "richlow" else parse_document
    options = {"timezone": "UTC"} if kind == "richlow" else {"fallback_timestamp": STAMP}
    constructed = 0
    original = chat_formats._episode

    def counted(value):
        nonlocal constructed
        constructed += 1
        return original(value)

    monkeypatch.setattr(chat_formats, "_episode", counted)
    with pytest.raises(PortabilityError, match="exceed"):
        parse(raw, max_bytes=250_000, **options)
    assert constructed == 2
    with pytest.raises(PortabilityError, match="chunks|episodes"):
        parse(raw, max_episodes=1, **options)


@pytest.mark.parametrize(
    "fallback", [None, "", "2026-01-02", "2026-01-02T03:04:05", "bad", 1767312000]
)
def test_document_requires_explicit_aware_iso_fallback(fallback):
    with pytest.raises(PortabilityError):
        parse_document(b"Synthetic text", fallback_timestamp=fallback)


@pytest.mark.parametrize("parse", [parse_document, parse_richlow])
@pytest.mark.parametrize("raw", [b"", b" \n", b"\xff"])
def test_text_formats_reject_empty_or_non_utf8(parse, raw):
    with pytest.raises(PortabilityError):
        parse(raw, fallback_timestamp=STAMP)


def test_adapters_obey_input_episode_and_record_limits(monkeypatch):
    monkeypatch.setattr(chat_formats, "MAX_INPUT_BYTES", 5)
    for parse, raw in [
        (parse_claude, _json(_claude())),
        (parse_richlow, _richlow()),
        (parse_document, b"Too long"),
    ]:
        with pytest.raises(PortabilityError, match="at most"):
            parse(raw)
    monkeypatch.setattr(chat_formats, "MAX_INPUT_BYTES", 128 * 1024 * 1024)
    monkeypatch.setattr(chat_formats, "MAX_EPISODES", 1)
    with pytest.raises(PortabilityError, match="episodes"):
        parse_claude(_json(_claude()), max_episodes=1)
    monkeypatch.setattr("memory_worlds.portability.MAX_RECORD_BYTES", 5)
    with pytest.raises(PortabilityError, match="record size limit"):
        parse_document(b"Synthetic text", fallback_timestamp=STAMP)
