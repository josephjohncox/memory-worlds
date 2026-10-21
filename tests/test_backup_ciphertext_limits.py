"""A valid encrypted artifact can exceed its compressed-payload cap by its framing."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from memory_worlds import artifact_sync, recovery_archive
from memory_worlds.cli import download_backup
from memory_worlds.errors import MemoryError as InvalidRequest


def test_writer_reader_download_and_sync_share_the_encrypted_file_ceiling(tmp_path, monkeypatch):
    key, source = tmp_path / "key", tmp_path / "source"
    recovery_archive.generate_key(key)
    source.write_bytes(os.urandom(4096))
    authority_id = uuid4()
    first = tmp_path / "size-probe.mwauthority"
    recovery_archive.write_backup(
        first,
        key,
        {"evidence.bin": source},
        authority_id=authority_id,
        source_backend="sqlite",
        graph_mode="rebuild",
    )
    framing = len(recovery_archive.MAGIC) + 12 + 16
    payload_limit = first.stat().st_size - framing // 2
    ciphertext_limit = payload_limit + framing
    monkeypatch.setattr(recovery_archive, "MAX_BACKUP_BYTES", payload_limit)
    monkeypatch.setattr(recovery_archive, "MAX_ENCRYPTED_BACKUP_BYTES", ciphertext_limit)
    monkeypatch.setattr(artifact_sync, "MAX_ENCRYPTED_BACKUP_BYTES", ciphertext_limit)
    archive = tmp_path / "boundary.mwauthority"
    recovery_archive.write_backup(
        archive,
        key,
        {"evidence.bin": source},
        authority_id=authority_id,
        source_backend="sqlite",
        graph_mode="rebuild",
    )
    assert payload_limit < archive.stat().st_size <= ciphertext_limit
    with recovery_archive.read_backup(archive, key) as (_, verified):
        assert (verified / "evidence.bin").read_bytes() == source.read_bytes()

    payload = archive.read_bytes()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=payload, headers={"content-length": str(len(payload))}
            ),
        ),
        base_url="http://memory.example.test",
    ) as http:
        downloaded = tmp_path / "downloaded.mwauthority"
        receipt = download_backup(SimpleNamespace(http=http), str(uuid4()), downloaded)
        assert receipt["bytes"] == len(payload) and downloaded.read_bytes() == payload

    config_file = tmp_path / "rclone.conf"
    config_file.write_text("[archive]\ntype = local\n")
    config_file.chmod(0o600)
    remote = tmp_path / "remote"
    remote.mkdir()

    def transport(_binary, arguments, _config):
        if arguments[0] == "copyto":
            shutil.copyfile(arguments[1], remote / Path(arguments[2]).name)
        else:
            assert arguments[0] == "check"
            for item in Path(arguments[1]).iterdir():
                assert item.read_bytes() == (remote / item.name).read_bytes()

    monkeypatch.setattr(artifact_sync, "_run", transport)
    config = artifact_sync.ArtifactSyncConfig(remote="archive:authority", rclone_config=config_file)
    result = artifact_sync.sync_artifact(archive, key, config)
    assert result["bytes"] == len(payload)
    assert (remote / result["artifact"]).read_bytes() == payload

    oversized = tmp_path / "oversized.mwauthority"
    oversized.write_bytes(payload.ljust(ciphertext_limit + 1, b"x"))
    with pytest.raises(InvalidRequest):
        with recovery_archive.read_backup(oversized, key):
            pytest.fail("an oversized artifact must not yield verified contents")
    with pytest.raises(InvalidRequest, match="bounded"):
        artifact_sync.sync_artifact(oversized, key, config)
