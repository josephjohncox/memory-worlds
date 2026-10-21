import hashlib
import json
import zipfile
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from memory_worlds.models import CreateScope, EpisodeInput, ScopeKind
from memory_worlds.portability import PortabilityError, export_world, restore_world
from memory_worlds.store import MemoryStore


def authority(path):
    store = MemoryStore(path)
    world, token = store.bootstrap()
    owner = store.authenticate(token)
    scope = store.create_scope(
        owner, world.id, CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Private scratch")
    )
    return store, world, owner, scope


def test_scope_export_restore_keeps_private_evidence_out_of_shared_memory(tmp_path):
    store, world, owner, scope = authority(tmp_path / "source")
    original = store.capture(
        owner,
        world.id,
        EpisodeInput(
            scope_id=scope.id,
            source_id="private-source",
            content="Private scope evidence",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ).episode
    public = export_world(store, owner, world.id, tmp_path / "shared.mworld")
    archive = export_world(store, owner, world.id, tmp_path / "private.mworld", scope_id=scope.id)
    with zipfile.ZipFile(public) as source:
        assert not source.read("episodes.jsonl")
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("manifest.json"))
        assert manifest["version"] == 2 and manifest["scope"]["id"] == str(scope.id)
    target, target_world, target_owner, target_scope = authority(tmp_path / "destination")
    with pytest.raises(PortabilityError, match="explicit private destination"):
        restore_world(target, target_owner, target_world.id, archive)
    assert restore_world(
        target, target_owner, target_world.id, archive, scope_id=target_scope.id
    ) == {"imported": 1, "replayed": 0}
    assert restore_world(
        target, target_owner, target_world.id, archive, scope_id=target_scope.id
    ) == {"imported": 0, "replayed": 1}
    assert not target.list_episodes(target_owner, target_world.id)
    restored = target.list_episodes(target_owner, target_world.id, scope_id=target_scope.id)[0]
    assert restored.scope_id == target_scope.id
    origin = restored.metadata["memory_worlds_origin"]
    assert origin["episode_id"] == original.id and origin["scope"]["id"] == str(scope.id)


def test_restore_rejects_broader_scope_kind_and_mixed_scope_archive(tmp_path):
    store, world, owner, scope = authority(tmp_path / "source")
    store.capture(
        owner,
        world.id,
        EpisodeInput(
            scope_id=scope.id,
            source_id="private-source",
            content="Private scope evidence",
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    archive = export_world(store, owner, world.id, tmp_path / "private.mworld", scope_id=scope.id)
    run = store.create_scope(owner, world.id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Task"))
    with pytest.raises(PortabilityError, match="broaden"):
        restore_world(store, owner, world.id, archive, scope_id=run.id)
    with zipfile.ZipFile(archive) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    record = json.loads(entries["episodes.jsonl"])
    record["scope_id"] = str(uuid4())
    entries["episodes.jsonl"] = (json.dumps(record) + "\n").encode()
    manifest = json.loads(entries["manifest.json"])
    manifest["episodes_sha256"] = hashlib.sha256(entries["episodes.jsonl"]).hexdigest()
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, "w") as target:
        for name, data in entries.items():
            target.writestr(name, data)
    with pytest.raises(PortabilityError, match="another scope"):
        restore_world(store, owner, world.id, archive, scope_id=scope.id)
