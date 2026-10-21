import shutil
from pathlib import Path

import pytest

from memory_worlds.artifact_sync import ArtifactSyncConfig, sync_artifact
from memory_worlds.errors import MemoryError, Unavailable
from memory_worlds.recovery import backup_authority
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore


def test_transport_authenticates_before_invoking_remote(tmp_path, monkeypatch):
    import memory_worlds.artifact_sync as transport

    config = tmp_path / "rclone.conf"
    config.write_text("[fixture]\ntype=local\n")
    config.chmod(0o600)
    key, incomplete = tmp_path / "key", tmp_path / "incomplete.mwauthority"
    generate_key(key)
    incomplete.write_bytes(b"incomplete or invalid encrypted artifact")
    calls = []
    monkeypatch.setattr(transport, "_run", lambda *_args: calls.append(True))
    with pytest.raises(MemoryError):
        sync_artifact(
            incomplete,
            key,
            ArtifactSyncConfig(remote=f"fixture:{tmp_path / 'remote'}", rclone_config=config),
        )
    assert not calls


def test_real_rclone_copy_verifies_bytes_replays_and_preserves_conflicting_remote(tmp_path):
    binary = shutil.which("rclone")
    if binary is None:
        pytest.skip("requires the pinned optional rclone tool")
    store = MemoryStore(tmp_path / "authority")
    _, token = store.bootstrap()
    key, archive, config = (
        tmp_path / "key",
        tmp_path / "backup.mwauthority",
        tmp_path / "rclone.conf",
    )
    generate_key(key)
    backup_authority(store, store.authenticate(token), archive, key)
    config.write_text("[fixture]\ntype=local\n")
    config.chmod(0o600)
    remote = tmp_path / "cloud"
    settings = ArtifactSyncConfig(remote=f"fixture:{remote}", rclone_config=config)
    receipt = sync_artifact(archive, key, settings, binary=binary)
    assert receipt["verified"] == "downloaded-bytes"
    copied = remote / store.authority_id / receipt["artifact"]
    assert copied.read_bytes() == archive.read_bytes()
    unrelated = copied.parent / "unrelated"
    unrelated.write_text("retain this file")
    assert sync_artifact(archive, key, settings, binary=binary) == receipt
    assert unrelated.read_text() == "retain this file"
    corrupted = bytearray(copied.read_bytes())
    corrupted[-1] ^= 1
    copied.write_bytes(corrupted)
    with pytest.raises(Unavailable):
        sync_artifact(archive, key, settings, binary=binary)
    assert copied.read_bytes() == corrupted


def test_remote_configuration_cannot_be_inline_or_public_readable(tmp_path):
    from pydantic import ValidationError

    for remote in (":s3:bucket", "--config=bad", "fixture:x\n--bad"):
        with pytest.raises(ValidationError):
            ArtifactSyncConfig(remote=remote, rclone_config=Path("unused"))
    config = tmp_path / "rclone.conf"
    config.write_text("private OAuth configuration")
    config.chmod(0o644)
    with pytest.raises(MemoryError, match="private regular file"):
        sync_artifact(
            tmp_path / "missing",
            tmp_path / "key",
            ArtifactSyncConfig(remote="fixture:backups", rclone_config=config),
        )
