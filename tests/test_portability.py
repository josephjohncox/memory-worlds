import hashlib
import json
import stat
import threading
import zipfile
from datetime import UTC, datetime

import pytest

from memory_worlds.errors import Conflict, Forbidden
from memory_worlds.models import CreateToken, CreateWorld, EpisodeInput, WorldKind
from memory_worlds.portability import (
    PortabilityError,
    export_world,
    import_chatgpt,
    import_pi,
    restore_world,
)
from memory_worlds.store import MemoryStore


@pytest.fixture
def authority(tmp_path):
    store = MemoryStore(tmp_path / "source")
    world, token = store.bootstrap("alice")
    return store, store.authenticate(token), world


def _input(source_id="synthetic-1", content="A synthetic source record", **changes):
    return EpisodeInput(
        source_id=source_id,
        content=content,
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
        **changes,
    )


def _target(tmp_path):
    store = MemoryStore(tmp_path / "target")
    world, token = store.bootstrap("bob")
    return store, store.authenticate(token), world


def _rewrite_archive(path, mutate):
    with zipfile.ZipFile(path) as archive:
        entries = {item.filename: archive.read(item) for item in archive.infolist()}
    mutate(entries)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def _chatgpt():
    def message(node_id, role, text):
        return {
            "id": node_id,
            "author": {"role": role},
            "create_time": 1767312000,
            "content": {"content_type": "text", "parts": [text]},
        }

    return {
        "id": "synthetic-conversation",
        "title": "Synthetic branches",
        "create_time": 1767311900,
        "current_node": "chosen",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["question"], "message": None},
            "question": {
                "id": "question",
                "parent": "root",
                "children": ["old", "chosen"],
                "message": message("question", "user", "Choose a material"),
            },
            "old": {
                "id": "old",
                "parent": "question",
                "children": [],
                "message": message("old", "assistant", "Abandoned alternative"),
            },
            "chosen": {
                "id": "chosen",
                "parent": "question",
                "children": [],
                "message": message("chosen", "assistant", "Selected alternative"),
            },
        },
    }


def _write_chatgpt(tmp_path, data=None):
    path = tmp_path / "synthetic-chatgpt.json"
    path.write_text(json.dumps([_chatgpt()] if data is None else data))
    return path


def _pi():
    def message(node_id, parent, role, text):
        return {
            "type": "message",
            "id": node_id,
            "parentId": parent,
            "timestamp": "2026-01-02T00:00:00Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }

    return [
        {
            "type": "session",
            "version": 3,
            "id": "synthetic-session",
            "timestamp": "2026-01-01T00:00:00Z",
        },
        message("question", None, "user", "Choose a process"),
        message("old", "question", "assistant", "Abandoned process"),
        message("chosen", "question", "assistant", "Selected process"),
    ]


def _write_pi(tmp_path, records=None):
    path = tmp_path / "synthetic-pi.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in (_pi() if records is None else records)))
    return path


def test_archive_roundtrip_rebinds_importer_without_credentials_or_grants(authority, tmp_path):
    store, principal, world = authority
    original = store.capture(principal, world.id, _input(project=True)).episode
    store.set_projection_configuration(
        world.id,
        {
            "backend": "falkordb",
            "llm_model": "synthetic-model",
            "llm_base_url": "http://private-provider.example",
            "api_key": "synthetic-secret",
        },
    )
    destination = export_world(store, principal, world.id, tmp_path / "sync" / "test.mworld")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(destination.parent.glob("*.pending"))
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {"manifest.json", "episodes.jsonl", "audit.jsonl"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["episode_count"] == 1
        assert manifest["audit_count"] == 1
        assert "credentials" not in manifest and "grants" not in manifest
        assert manifest["projection_configuration"] == {
            "backend": "falkordb",
            "llm_model": "synthetic-model",
        }
        assert "synthetic-secret" not in archive.read("manifest.json").decode()
        assert manifest["recovery"] == "source-only; graph re-extraction required"
    target, importer, target_world = _target(tmp_path)
    assert restore_world(target, importer, target_world.id, destination) == {
        "imported": 1,
        "replayed": 0,
    }
    assert restore_world(target, importer, target_world.id, destination) == {
        "imported": 0,
        "replayed": 1,
    }
    restored = target.list_episodes(importer, target_world.id)[0]
    assert restored.world_id == target_world.id
    assert restored.actor_id == "bob" and restored.agent_id == importer.agent_id
    assert restored.source_id == original.source_id
    assert restored.content == original.content and restored.occurred_at == original.occurred_at
    assert restored.project is False and restored.projection_state == "disabled"
    origin = restored.metadata["memory_worlds_origin"]
    assert origin["actor_id"] == "alice" and origin["world_id"] == world.id
    assert origin["authority_id"] == store.authority_id
    assert origin["audit"][0]["event"] == "capture"
    assert target.pending_jobs(target_world.id) == []


def test_archive_export_does_not_overwrite_completed_artifact(authority, tmp_path):
    store, principal, world = authority
    destination = export_world(store, principal, world.id, tmp_path / "snapshot.mworld")
    before = destination.read_bytes()
    with pytest.raises(PortabilityError, match="already exists"):
        export_world(store, principal, world.id, destination)
    assert destination.read_bytes() == before


def test_restore_rejects_staging_even_when_archive_is_complete(authority, tmp_path):
    store, principal, world = authority
    destination = export_world(store, principal, world.id, tmp_path / "snapshot.mworld")
    pending = destination.with_suffix(".pending")
    pending.write_bytes(destination.read_bytes())
    with pytest.raises(PortabilityError, match="Only finalized"):
        restore_world(store, principal, world.id, pending)


def test_archive_export_requires_world_access(authority, tmp_path):
    store, principal, world = authority
    outsider = store.authenticate(
        store.issue_token(principal, CreateToken(user_id="bob", agent_id="agent"))
    )
    with pytest.raises(Forbidden):
        export_world(store, outsider, world.id, tmp_path / "forbidden.mworld")
    assert not (tmp_path / "forbidden.mworld").exists()


@pytest.mark.parametrize("damage", ["checksum", "count", "path", "version", "unknown_field"])
def test_corrupt_archive_rejected_before_writes(authority, tmp_path, damage):
    store, principal, world = authority
    store.capture(principal, world.id, _input())
    destination = export_world(store, principal, world.id, tmp_path / "broken.mworld")

    def mutate(entries):
        manifest = json.loads(entries["manifest.json"])
        if damage == "checksum":
            entries["episodes.jsonl"] += b" "
        elif damage == "count":
            manifest["episode_count"] += 1
        elif damage == "path":
            entries["../../escaped"] = b"must never be extracted"
        elif damage == "version":
            manifest["version"] = 2
        else:
            manifest["grants"] = {"bob": "manager"}
        entries["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_archive(destination, mutate)
    target, importer, target_world = _target(tmp_path)
    with pytest.raises(PortabilityError):
        restore_world(target, importer, target_world.id, destination)
    assert target.list_episodes(importer, target_world.id) == []
    assert not (tmp_path / "escaped").exists()


def test_archive_rejects_malformed_final_record_before_importing_first(authority, tmp_path):
    store, principal, world = authority
    store.capture(principal, world.id, _input())
    destination = export_world(store, principal, world.id, tmp_path / "broken.mworld")

    def mutate(entries):
        entries["episodes.jsonl"] += b'{"source_id":"invalid"}\n'
        manifest = json.loads(entries["manifest.json"])
        manifest["episode_count"] = 2
        manifest["episodes_sha256"] = hashlib.sha256(entries["episodes.jsonl"]).hexdigest()
        entries["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_archive(destination, mutate)
    target, importer, target_world = _target(tmp_path)
    with pytest.raises(PortabilityError, match="Invalid archived episode 2"):
        restore_world(target, importer, target_world.id, destination)
    assert target.list_episodes(importer, target_world.id) == []


def test_restore_conflict_rolls_back_earlier_records(authority, tmp_path):
    store, principal, world = authority
    store.capture(principal, world.id, _input("first"))
    store.capture(principal, world.id, _input("conflict"))
    destination = export_world(store, principal, world.id, tmp_path / "conflict.mworld")
    target, importer, target_world = _target(tmp_path)
    existing = target.capture(importer, target_world.id, _input("conflict", "Different")).episode
    with pytest.raises(Conflict):
        restore_world(target, importer, target_world.id, destination)
    assert [item.id for item in target.list_episodes(importer, target_world.id)] == [existing.id]


def test_export_during_writes_is_a_consistent_importable_snapshot(authority, tmp_path):
    store, principal, world = authority
    errors = []

    def write():
        try:
            for number in range(30):
                store.capture(principal, world.id, _input(f"concurrent-{number}"))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=write)
    thread.start()
    destination = export_world(store, principal, world.id, tmp_path / "concurrent.mworld")
    thread.join()
    assert errors == []
    target, importer, target_world = _target(tmp_path)
    counts = restore_world(target, importer, target_world.id, destination)
    restored = target.list_episodes(importer, target_world.id)
    assert counts["imported"] == len(restored)
    assert len(store.list_episodes(principal, world.id)) == 30
    assert [item.source_id for item in restored] == [
        f"concurrent-{n}" for n in range(len(restored))
    ]


def test_chatgpt_active_branch_dates_roles_and_all_branches_replay(authority, tmp_path):
    store, principal, world = authority
    path = _write_chatgpt(tmp_path)
    result = import_chatgpt(store, principal, world.id, path)
    assert result["imported"] == 2 and result["skipped"] == 1
    episodes = store.list_episodes(principal, world.id)
    assert [item.source_entry_id for item in episodes] == ["question", "chosen"]
    assert [item.role for item in episodes] == ["user", "assistant"]
    assert episodes[1].parent_entry_id == "question"
    assert episodes[0].occurred_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert all(not item.project for item in episodes)
    result = import_chatgpt(store, principal, world.id, path, all_branches=True)
    assert result["imported"] == 1 and result["replayed"] == 2
    assert {item.source_entry_id for item in store.list_episodes(principal, world.id)} == {
        "question",
        "old",
        "chosen",
    }


def test_chatgpt_unsupported_content_retained_and_reported(authority, tmp_path):
    store, principal, world = authority
    data = _chatgpt()
    content = {"content_type": "multimodal_text", "parts": [{"asset_pointer": "synthetic://image"}]}
    data["mapping"]["chosen"]["message"]["content"] = content
    path = _write_chatgpt(tmp_path, [data])
    result = import_chatgpt(store, principal, world.id, path)
    assert result["unsupported_parts"] == 1 and result["references_only"] == 1
    episode = store.list_episodes(principal, world.id)[1]
    assert "assets were not fetched" in episode.content
    assert episode.metadata["chatgpt"]["raw_content"] == content


@pytest.mark.parametrize(
    "damage",
    [
        "cycle",
        "missing_parent",
        "missing_active",
        "bad_children",
        "duplicate_id",
        "malformed_last",
        "missing_date",
    ],
)
def test_chatgpt_invalid_input_never_partially_imports(authority, tmp_path, damage):
    store, principal, world = authority
    data = _chatgpt()
    if damage == "cycle":
        data["mapping"]["root"]["parent"] = "chosen"
    elif damage == "missing_parent":
        data["mapping"]["chosen"]["parent"] = "absent"
    elif damage == "missing_active":
        data["current_node"] = "absent"
    elif damage == "bad_children":
        data["mapping"]["root"]["children"] = [{}]
    elif damage == "duplicate_id":
        data["mapping"]["old"]["message"]["id"] = "chosen"
    elif damage == "malformed_last":
        data["mapping"]["chosen"]["message"]["content"] = {"parts": "bad"}
    else:
        data.pop("create_time")
        data["mapping"]["chosen"]["message"].pop("create_time")
    path = _write_chatgpt(tmp_path, [data])
    with pytest.raises(PortabilityError):
        import_chatgpt(store, principal, world.id, path)
    assert store.list_episodes(principal, world.id) == []


def test_duplicate_json_keys_rejected(authority, tmp_path):
    store, principal, world = authority
    path = tmp_path / "duplicate.json"
    path.write_text('{"id":"one","id":"two"}')
    with pytest.raises(PortabilityError, match="Duplicate JSON key"):
        import_chatgpt(store, principal, world.id, path)
    assert store.list_episodes(principal, world.id) == []


def test_chatgpt_transactional_conflict_preserves_existing_episode(authority, tmp_path):
    store, principal, world = authority
    store.capture(
        principal, world.id, _input("chatgpt:synthetic-conversation:chosen", "Conflicting")
    )
    with pytest.raises(Conflict):
        import_chatgpt(store, principal, world.id, _write_chatgpt(tmp_path))
    episodes = store.list_episodes(principal, world.id)
    assert len(episodes) == 1 and episodes[0].content == "Conflicting"


def test_pi_branch_import_and_explicit_alternative_leaf(authority, tmp_path):
    store, principal, world = authority
    path = _write_pi(tmp_path)
    assert import_pi(store, principal, world.id, path)["imported"] == 2
    episodes = store.list_episodes(principal, world.id)
    assert [item.source_entry_id for item in episodes] == ["question", "chosen"]
    assert episodes[1].parent_entry_id == "question"
    assert all(item.session_id == "synthetic-session" for item in episodes)
    assert episodes[0].occurred_at == datetime(2026, 1, 2, tzinfo=UTC)
    result = import_pi(store, principal, world.id, path, leaf_id="old")
    assert result["imported"] == 1 and result["replayed"] == 1
    assert import_pi(store, principal, world.id, path, all_branches=True)["replayed"] == 3


def test_pi_unsupported_parts_and_millisecond_timestamp(authority, tmp_path):
    store, principal, world = authority
    data = _pi()
    data[3].pop("timestamp")
    data[3]["message"]["timestamp"] = 1767312000000
    data[3]["message"]["content"] = [{"type": "image", "reference": "synthetic://image"}]
    result = import_pi(store, principal, world.id, _write_pi(tmp_path, data))
    assert result["unsupported_parts"] == 1 and result["references_only"] == 1
    episode = store.list_episodes(principal, world.id)[1]
    assert episode.occurred_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert episode.metadata["pi"]["raw_content"] == data[3]["message"]["content"]


@pytest.mark.parametrize(
    "role", ["bashExecution", "branchSummary", "compactionSummary", "futureRole"]
)
def test_pi_extended_messages_preserve_original_role_and_evidence(authority, tmp_path, role):
    store, principal, world = authority
    data = _pi()
    message = {"role": role, "timestamp": 1767312000000}
    if role == "bashExecution":
        message.update(command="echo synthetic", output="synthetic", exitCode=0)
    elif role in {"branchSummary", "compactionSummary"}:
        message["summary"] = "Synthetic summary"
    else:
        message["reference"] = "synthetic://unsupported-message"
    data[3]["message"] = message
    result = import_pi(store, principal, world.id, _write_pi(tmp_path, data))
    assert result["imported"] == 2
    episode = store.list_episodes(principal, world.id)[1]
    assert episode.role == role
    assert episode.metadata["pi"]["raw_message"] == message
    assert result["references_only"] == int(role == "futureRole")


def test_input_and_archive_limits_reject_before_mutation(authority, tmp_path, monkeypatch):
    import memory_worlds.portability as portability

    store, principal, world = authority
    path = _write_pi(tmp_path)
    monkeypatch.setattr(portability, "MAX_RECORD_BYTES", 5)
    with pytest.raises(PortabilityError, match="record size"):
        import_pi(store, principal, world.id, path)
    assert store.list_episodes(principal, world.id) == []
    monkeypatch.setattr(portability, "MAX_RECORD_BYTES", 2 * 1024 * 1024)
    destination = export_world(store, principal, world.id, tmp_path / "bounded.mworld")
    monkeypatch.setattr(portability, "MAX_UNCOMPRESSED_BYTES", 5)
    with pytest.raises(PortabilityError, match="uncompressed size"):
        restore_world(store, principal, world.id, destination)
    assert store.list_episodes(principal, world.id) == []


def test_audit_checksum_and_references_are_verified(authority, tmp_path):
    store, principal, world = authority
    store.capture(principal, world.id, _input())
    destination = export_world(store, principal, world.id, tmp_path / "audit.mworld")

    def mutate(entries):
        audit = json.loads(entries["audit.jsonl"])
        audit["episode_id"] = "missing-episode"
        entries["audit.jsonl"] = json.dumps(audit).encode() + b"\n"
        manifest = json.loads(entries["manifest.json"])
        manifest["audit_sha256"] = hashlib.sha256(entries["audit.jsonl"]).hexdigest()
        entries["manifest.json"] = json.dumps(manifest).encode()

    _rewrite_archive(destination, mutate)
    target, importer, target_world = _target(tmp_path)
    with pytest.raises(PortabilityError, match="Audit references"):
        restore_world(target, importer, target_world.id, destination)
    assert target.list_episodes(importer, target_world.id) == []


@pytest.mark.parametrize(
    "damage",
    ["cycle", "missing_parent", "duplicate_id", "bad_content", "bad_header", "missing_leaf"],
)
def test_pi_invalid_input_never_partially_imports(authority, tmp_path, damage):
    store, principal, world = authority
    data = _pi()
    if damage == "cycle":
        data[1]["parentId"] = "chosen"
    elif damage == "missing_parent":
        data[3]["parentId"] = "absent"
    elif damage == "duplicate_id":
        data[2]["id"] = "chosen"
    elif damage == "bad_content":
        data[3]["message"]["content"] = [{"type": "text", "text": False}]
    elif damage == "bad_header":
        data[0]["type"] = "message"
    path = _write_pi(tmp_path, data)
    with pytest.raises(PortabilityError):
        import_pi(
            store, principal, world.id, path, leaf_id="absent" if damage == "missing_leaf" else None
        )
    assert store.list_episodes(principal, world.id) == []


def test_import_to_shared_world_is_explicit_admin_restore(authority, tmp_path):
    store, principal, _ = authority
    world = store.create_world(principal, CreateWorld(name="Synthetic team", kind=WorldKind.TEAM))
    result = import_chatgpt(store, principal, world.id, _write_chatgpt(tmp_path))
    assert result["imported"] == 2
    assert all(
        item.actor_id == principal.user_id for item in store.list_episodes(principal, world.id)
    )
