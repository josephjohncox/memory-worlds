import io
import json
import os
import stat
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from memory_worlds import recovery_archive as recovery
from memory_worlds.errors import Conflict, MemoryError


@pytest.fixture
def archive(tmp_path):
    key = tmp_path / "authority.key"
    recovery.generate_key(key)
    source = tmp_path / "control.snapshot"
    source.write_bytes(b"synthetic-private-grants-and-identities")
    private = tmp_path / "private.snapshot"
    private.write_bytes(b"synthetic-private-invocation-evidence")
    destination = tmp_path / "authority.mwauthority"
    members = {"journals/control.sqlite3": source, "journals/private/source.sqlite3": private}
    manifest = recovery.write_backup(
        destination,
        key,
        members,
        authority_id=uuid4(),
        source_backend="sqlite",
        graph_mode="rebuild",
        metadata={"synthetic_fixture": True},
    )
    return destination, key, members, manifest


def test_authenticated_private_archive_roundtrip_and_staging_cleanup(archive):
    source, key, members, expected = archive
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    for path in members.values():
        assert path.read_bytes() not in source.read_bytes()
    with recovery.read_backup(source, key) as (manifest, staging):
        assert manifest == expected
        assert stat.S_IMODE(staging.stat().st_mode) == 0o700
        for name, original in members.items():
            restored = staging / name
            assert restored.read_bytes() == original.read_bytes()
            assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert not staging.exists()


@pytest.mark.parametrize("where", ["header", "ciphertext", "tag", "truncated", "wrong-key"])
def test_authentication_completes_before_any_archive_parsing(archive, monkeypatch, where):
    source, key, _, _ = archive
    data = bytearray(source.read_bytes())
    if where == "header":
        data[0] ^= 1
    elif where == "ciphertext":
        data[len(recovery.MAGIC) + 20] ^= 1
    elif where == "tag":
        data[-1] ^= 1
    elif where == "truncated":
        del data[-10:]
    else:
        key.unlink()
        recovery.generate_key(key)
    source.write_bytes(data)
    parsed = False

    def forbidden(*_args, **_kwargs):
        nonlocal parsed
        parsed = True
        raise AssertionError("Unauthenticated plaintext reached archive parser")

    monkeypatch.setattr(recovery.tarfile, "open", forbidden)
    with pytest.raises(MemoryError):
        with recovery.read_backup(source, key):
            raise AssertionError("Invalid backup was yielded")
    assert not parsed


def test_key_generation_never_overwrites_or_exposes_key_material(tmp_path):
    path = tmp_path / "key"
    recovery.generate_key(path)
    value = path.read_bytes()
    assert len(value) == 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        recovery.generate_key(path)
    assert path.read_bytes() == value


def test_public_key_file_permissions_are_rejected(archive, tmp_path):
    source, key, _, _ = archive
    key.chmod(0o644)
    with pytest.raises(MemoryError, match="private file"):
        with recovery.read_backup(source, key):
            pass
    key.chmod(0o440)
    with recovery.read_backup(source, key):
        pass


def test_completed_backup_is_immutable(archive):
    source, key, members, manifest = archive
    before = source.read_bytes()
    with pytest.raises(Conflict):
        recovery.write_backup(
            source,
            key,
            members,
            authority_id=manifest.authority_id,
            source_backend="sqlite",
            graph_mode="rebuild",
        )
    assert source.read_bytes() == before


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../b", ".", "a//b", "a\\b"])
def test_unsafe_member_paths_are_rejected_before_publication(tmp_path, name):
    key, source, destination = tmp_path / "key", tmp_path / "source", tmp_path / "b.mwauthority"
    recovery.generate_key(key)
    source.write_bytes(b"synthetic")
    with pytest.raises(MemoryError, match="Unsafe"):
        recovery.write_backup(
            destination,
            key,
            {name: source},
            authority_id=uuid4(),
            source_backend="sqlite",
            graph_mode="rebuild",
        )
    assert not destination.exists()


def test_special_files_are_never_followed_or_opened_blocking(tmp_path):
    key = tmp_path / "key"
    recovery.generate_key(key)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(MemoryError, match="regular"):
        recovery.write_backup(
            tmp_path / "b.mwauthority",
            key,
            {"journals/source": fifo},
            authority_id=uuid4(),
            source_backend="sqlite",
            graph_mode="rebuild",
        )


def test_changed_snapshot_cannot_publish_a_completed_archive(archive, monkeypatch, tmp_path):
    _, key, members, manifest = archive
    original = recovery._regular
    opened = 0

    def changing(path, *args):
        nonlocal opened
        opened += 1
        if opened == len(members) + 1:
            Path(path).write_bytes(b"mutated synthetic snapshot")
        return original(path, *args)

    monkeypatch.setattr(recovery, "_regular", changing)
    destination = tmp_path / "changed.mwauthority"
    with pytest.raises(Conflict, match="changed"):
        recovery.write_backup(
            destination,
            key,
            members,
            authority_id=manifest.authority_id,
            source_backend="sqlite",
            graph_mode="rebuild",
        )
    assert not destination.exists() and not list(tmp_path.glob("*.pending"))


def test_aggregate_snapshot_budget_is_checked_before_encryption(archive, monkeypatch, tmp_path):
    _, key, members, manifest = archive
    monkeypatch.setattr(recovery, "MAX_BACKUP_BYTES", 10)
    with pytest.raises(MemoryError, match="aggregate"):
        recovery.write_backup(
            tmp_path / "too-big.mwauthority",
            key,
            members,
            authority_id=manifest.authority_id,
            source_backend="sqlite",
            graph_mode="rebuild",
        )


@pytest.mark.parametrize("attack", ["symlink", "extra", "missing", "checksum", "traversal"])
def test_authenticated_but_malformed_archive_never_yields_a_restore_target(archive, attack):
    source, key, members, manifest = archive
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as output:
        encoded = json.dumps(manifest.model_dump(mode="json")).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(encoded)
        output.addfile(info, io.BytesIO(encoded))
        for index, (name, path) in enumerate(sorted(members.items())):
            if attack == "missing" and index == 1:
                continue
            info = tarfile.TarInfo("../outside" if attack == "traversal" else name)
            content = path.read_bytes()
            if attack == "checksum" and index == 0:
                content = b"x" * len(content)
            if attack == "symlink" and index == 0:
                info.type, info.linkname = tarfile.SYMTYPE, "/outside"
                output.addfile(info)
            else:
                info.size = len(content)
                output.addfile(info, io.BytesIO(content))
        if attack == "extra":
            info = tarfile.TarInfo("extra")
            info.size = 0
            output.addfile(info, io.BytesIO())
    nonce = os.urandom(12)
    header = recovery.MAGIC + nonce
    source.write_bytes(header + AESGCM(key.read_bytes()).encrypt(nonce, raw.getvalue(), header))
    with pytest.raises(MemoryError):
        with recovery.read_backup(source, key):
            raise AssertionError("Malformed archive reached restore")
