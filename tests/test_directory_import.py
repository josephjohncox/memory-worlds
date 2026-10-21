import json
import os
from uuid import uuid4

import pytest

import memory_worlds.directory_import as importer
from memory_worlds.directory_import import plan_directory
from memory_worlds.portability import PortabilityError


def conversation(text="Synthetic content", *, conversation_id="conversation", node_id="node"):
    return {
        "id": conversation_id,
        "title": "Synthetic test",
        "create_time": 1767312000,
        "current_node": node_id,
        "mapping": {
            node_id: {
                "id": node_id,
                "parent": None,
                "children": [],
                "message": {
                    "id": node_id,
                    "author": {"role": "user"},
                    "create_time": 1767312000,
                    "content": {"content_type": "text", "parts": [text]},
                },
            }
        },
    }


def write_chat(path, **changes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([conversation(**changes)]))


def test_plan_is_write_free_path_independent_and_deduplicates_copies(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    write_chat(first / "project" / "chat.json")
    write_chat(first / "copy.json")
    write_chat(second / "renamed.json")
    left, right = plan_directory(first), plan_directory(second)
    assert left.episodes == right.episodes
    assert left.report["deduplicated"] == 1
    assert left.report["selected_files"] == 2
    assert left.report["episodes"] == 1
    assert left.report["files"][0]["path"] == "copy.json"
    report = json.dumps(left.report)
    assert str(tmp_path) not in report
    assert "Synthetic content" not in report
    assert "sha256" in report
    assert left.episodes[0].project is True
    assert left.episodes[0].scope_id is None
    scope = uuid4()
    assert left.payload(scope)["episodes"][0]["scope_id"] == str(scope)


def test_append_native_message_retains_prior_payload(tmp_path):
    path = tmp_path / "chat.json"
    write_chat(path)
    original = plan_directory(tmp_path).episodes[0]
    data = conversation()
    data["current_node"] = "answer"
    data["mapping"]["node"]["children"] = ["answer"]
    data["mapping"]["answer"] = {
        "id": "answer",
        "parent": "node",
        "children": [],
        "message": {
            "id": "answer",
            "author": {"role": "assistant"},
            "create_time": 1767312001,
            "content": {"parts": ["New answer"]},
        },
    }
    path.write_text(json.dumps([data]))
    updated = plan_directory(tmp_path)
    assert len(updated.episodes) == 2
    assert updated.episodes[0] == original


def test_conflicting_native_ids_fail_before_plan_is_returned(tmp_path):
    write_chat(tmp_path / "a.json")
    write_chat(tmp_path / "b.json", text="Conflicting synthetic text")
    with pytest.raises(PortabilityError, match="Conflicting records"):
        plan_directory(tmp_path)


def test_malformed_final_file_aborts_complete_plan(tmp_path):
    write_chat(tmp_path / "a.json")
    (tmp_path / "z.json").write_text('{"mapping":')
    with pytest.raises(PortabilityError, match="z.json"):
        plan_directory(tmp_path)


def test_unknown_files_and_hidden_entries_are_not_imported(tmp_path):
    write_chat(tmp_path / "chat.json")
    (tmp_path / "settings.json").write_text('{"setting": 1}')
    (tmp_path / "notes.md").write_text("# Arbitrary Markdown\nNot a transcript")
    (tmp_path / "image.png").write_bytes(b"synthetic asset")
    (tmp_path / ".env").write_bytes(b"must not read")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "loop").symlink_to(tmp_path)
    plan = plan_directory(tmp_path)
    assert plan.report["selected_files"] == 1
    assert plan.report["skipped_files"] == 5
    assert plan.report["scanned_files"] == 4


@pytest.mark.parametrize("kind", ["file", "directory", "fifo"])
def test_symlinks_and_special_files_are_rejected_without_following(tmp_path, kind):
    root = tmp_path / "input"
    root.mkdir()
    write_chat(root / "a.json")
    if kind == "file":
        (root / "link.json").symlink_to(root / "a.json")
    elif kind == "directory":
        (root / "link").symlink_to(root, target_is_directory=True)
    else:
        os.mkfifo(root / "pipe.json")
    with pytest.raises(PortabilityError, match="Links and special files"):
        plan_directory(root)


def test_root_symlink_is_rejected(tmp_path):
    root = tmp_path / "input"
    write_chat(root / "chat.json")
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(PortabilityError, match="cannot be read safely"):
        plan_directory(link)


def test_selected_file_replaced_by_symlink_is_not_followed(tmp_path, monkeypatch):
    write_chat(tmp_path / "chat.json")
    original_open = importer.os.open

    def racing_open(path, flags, *args, **kwargs):
        if path == "chat.json":
            (tmp_path / "chat.json").unlink()
            (tmp_path / "chat.json").symlink_to("/dev/zero")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(importer.os, "open", racing_open)
    with pytest.raises(PortabilityError, match="cannot be read safely"):
        plan_directory(tmp_path)


def test_input_modified_during_read_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "chat.json"
    write_chat(path)
    original_read = importer.os.read
    changed = False

    def racing_read(fd, size):
        nonlocal changed
        data = original_read(fd, size)
        if not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b" ")
        return data

    monkeypatch.setattr(importer.os, "read", racing_read)
    with pytest.raises(PortabilityError, match="changed while being read"):
        plan_directory(tmp_path)


def test_total_input_bytes_are_bounded(tmp_path, monkeypatch):
    write_chat(tmp_path / "a.json")
    write_chat(tmp_path / "b.json", conversation_id="second")
    monkeypatch.setattr(importer, "MAX_INPUT_BYTES", (tmp_path / "a.json").stat().st_size + 1)
    with pytest.raises(PortabilityError, match="directory budget"):
        plan_directory(tmp_path)


def test_entry_and_depth_budgets(tmp_path, monkeypatch):
    write_chat(tmp_path / "nested" / "chat.json")
    monkeypatch.setattr(importer, "MAX_DIRECTORY_DEPTH", 0)
    with pytest.raises(PortabilityError, match="nesting"):
        plan_directory(tmp_path)
    monkeypatch.setattr(importer, "MAX_DIRECTORY_DEPTH", 16)
    monkeypatch.setattr(importer, "MAX_DIRECTORY_ENTRIES", 1)
    with pytest.raises(PortabilityError, match="entries"):
        plan_directory(tmp_path)


def test_record_and_encoded_payload_budgets(tmp_path, monkeypatch):
    write_chat(tmp_path / "a.json")
    write_chat(tmp_path / "b.json", conversation_id="second")
    monkeypatch.setattr(importer, "MAX_IMPORT_EPISODES", 1)
    with pytest.raises(PortabilityError, match="episodes"):
        plan_directory(tmp_path)
    monkeypatch.setattr(importer, "MAX_IMPORT_EPISODES", 10_000)
    monkeypatch.setattr(importer, "MAX_IMPORT_BYTES", 10)
    with pytest.raises(PortabilityError, match="32 MiB"):
        plan_directory(tmp_path)


def test_report_file_list_is_bounded(tmp_path, monkeypatch):
    write_chat(tmp_path / "a.json")
    write_chat(tmp_path / "b.json", conversation_id="second")
    monkeypatch.setattr(importer, "MAX_REPORT_FILES", 1)
    plan = plan_directory(tmp_path)
    assert len(plan.report["files"]) == 1
    assert plan.report["omitted_files"] == 1


def test_documents_mode_is_explicit_and_content_addressed(tmp_path):
    (tmp_path / "notes.md").write_text("# Synthetic notes\nHuman: body remains opaque")
    with pytest.raises(PortabilityError, match="no importable"):
        plan_directory(tmp_path)
    with pytest.raises(PortabilityError):
        plan_directory(tmp_path, format="documents")
    plan = plan_directory(tmp_path, format="documents", fallback_timestamp="2026-01-02T00:00:00Z")
    assert len(plan.episodes) == 1
    assert plan.episodes[0].content.startswith("# Synthetic notes")
    assert plan.episodes[0].source_id.startswith("document:")


def test_episode_validation_diagnostics_do_not_echo_transcripts(tmp_path):
    write_chat(tmp_path / "chat.json", text="sensitive-synthetic-" * 20_000)
    with pytest.raises(PortabilityError) as error:
        plan_directory(tmp_path)
    assert "sensitive-synthetic-" not in str(error.value)


def test_plan_parses_same_bytes_identified_by_file_hash(tmp_path):
    import hashlib

    path = tmp_path / "chat.json"
    write_chat(path)
    original = path.read_bytes()
    plan = plan_directory(tmp_path)
    write_chat(path, text="Subsequently changed file")
    assert plan.report["files"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert plan.episodes[0].content == "Synthetic content"


@pytest.mark.parametrize("field", ["timestamp", "duplicate_key", "node_id"])
def test_parser_diagnostics_do_not_echo_imported_values(tmp_path, field):
    marker = "PRIVATE_TRANSCRIPT_MARKER"
    value = conversation()
    if field == "timestamp":
        value["mapping"]["node"]["message"]["create_time"] = marker
        data = json.dumps([value])
    elif field == "node_id":
        value["current_node"] = marker
        data = json.dumps([value])
    else:
        data = '{"' + marker + '":1,"' + marker + '":2}'
    (tmp_path / "test.json").write_text(data)
    with pytest.raises(PortabilityError) as error:
        plan_directory(tmp_path)
    assert "test.json" in str(error.value)
    assert marker not in str(error.value)


def test_native_parser_stops_metadata_amplification_during_construction(monkeypatch):
    from memory_worlds import portability

    value = conversation()
    value["title"] = "Synthetic title " * 100
    for index in range(1, 50):
        node = f"node-{index}"
        parent = "node" if index == 1 else f"node-{index - 1}"
        value["mapping"][parent]["children"] = [node]
        value["mapping"][node] = {
            "id": node,
            "parent": parent,
            "children": [],
            "message": {
                "id": node,
                "author": {"role": "user"},
                "create_time": 1767312000,
                "content": {"parts": ["Short synthetic turn"]},
            },
        }
        value["current_node"] = node
    original = portability._episode
    calls = 0

    def counted(fields):
        nonlocal calls
        calls += 1
        return original(fields)

    monkeypatch.setattr(portability, "_episode", counted)
    with pytest.raises(PortabilityError, match="encoded output byte limit"):
        portability.parse_chatgpt(json.dumps([value]).encode(), max_bytes=5000)
    assert calls <= 3
    calls = 0
    with pytest.raises(PortabilityError, match="Too many episodes"):
        portability.parse_chatgpt(json.dumps([value]).encode(), max_episodes=1)
    assert calls == 2
