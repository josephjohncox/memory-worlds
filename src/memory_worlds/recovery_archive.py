"""Authenticated, bounded whole-authority archives for operator recovery.

Authentication completes before any archive member is parsed or restored.
The archive deliberately contains credential/grant state and is always encrypted.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import AwareDatetime, Field, ValidationError

from .errors import Conflict
from .errors import MemoryError as InvalidRequest
from .models import Model
from .portability import _json, _load_json

MAGIC = b"MEMORY-WORLDS-AUTHORITY\x00\x01"
MAX_BACKUP_BYTES = 8 * 1024**3
MAX_ENCRYPTED_BACKUP_BYTES = MAX_BACKUP_BYTES + len(MAGIC) + 12 + 16
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 2 * 1024**2
MAX_MEMBERS = 10_000
_CHUNK = 64 * 1024


class BackupMember(Model):
    name: str = Field(min_length=1, max_length=500)
    bytes: int = Field(ge=0, le=MAX_MEMBER_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class AuthorityManifest(Model):
    format: Literal["memory-worlds-authority"] = "memory-worlds-authority"
    version: Literal[1] = 1
    backup_id: UUID
    authority_id: UUID
    created_at: AwareDatetime
    source_backend: Literal["sqlite", "postgres"]
    graph_mode: Literal["native", "rebuild"]
    metadata: dict[str, Any] = Field(default_factory=dict)
    members: list[BackupMember] = Field(min_length=1, max_length=MAX_MEMBERS)


def generate_key(path: Path) -> None:
    """Create an exclusive raw 256-bit key; never return it in CLI output."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise


def _key(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        # Dedicated group-readable Kubernetes Secret volumes are permitted.
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o137:
            raise InvalidRequest("Backup key must be a regular private file")
        value = stream.read(33)
    if len(value) != 32:
        raise InvalidRequest("Backup key must contain exactly 32 raw bytes")
    return value


def _name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or path.is_absolute()
        or str(path) != name
        or ".." in path.parts
        or "\\" in name
        or any(ord(char) < 32 for char in name)
        or name == "manifest.json"
        or len(name) > 500
    ):
        raise InvalidRequest("Unsafe backup member name")
    return path


def _regular(path: Path, maximum: int = MAX_MEMBER_BYTES):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(descriptor, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        stream.close()
        raise InvalidRequest("Backup members must be bounded regular files")
    return stream, info


def _fingerprint(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _member(name: str, path: Path) -> BackupMember:
    _name(name)
    stream, before = _regular(path)
    digest = hashlib.sha256()
    size = 0
    with stream:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > before.st_size:
                raise Conflict("Backup member grew during hashing")
            digest.update(chunk)
        if _fingerprint(before) != _fingerprint(os.fstat(stream.fileno())):
            raise Conflict("Backup member changed during hashing")
    return BackupMember(name=name, bytes=before.st_size, sha256=digest.hexdigest())


class _EncryptingWriter:
    def __init__(self, stream, cipher):
        self.stream, self.cipher = stream, cipher
        self.bytes = 0

    def write(self, data):
        self.bytes += len(data)
        if self.bytes > MAX_BACKUP_BYTES:
            raise InvalidRequest("Backup archive exceeds its byte limit")
        self.stream.write(self.cipher.update(data))
        return len(data)


class _HashingReader:
    def __init__(self, stream):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def write_backup(
    destination: Path,
    key_file: Path,
    members: dict[str, Path],
    *,
    authority_id: str | UUID,
    source_backend: Literal["sqlite", "postgres"],
    graph_mode: Literal["native", "rebuild"],
    metadata: dict | None = None,
) -> AuthorityManifest:
    """Encrypt a previously quiesced snapshot and atomically publish its completed name."""
    if not 1 <= len(members) <= MAX_MEMBERS:
        raise InvalidRequest("Invalid backup member count")
    descriptions = [_member(name, Path(members[name])) for name in sorted(members)]
    if sum(item.bytes for item in descriptions) > MAX_BACKUP_BYTES:
        raise InvalidRequest("Backup members exceed their aggregate byte limit")
    manifest = AuthorityManifest(
        backup_id=uuid4(),
        authority_id=UUID(str(authority_id)),
        created_at=datetime.now(UTC),
        source_backend=source_backend,
        graph_mode=graph_mode,
        metadata=metadata or {},
        members=descriptions,
    )
    encoded = _json(manifest)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise InvalidRequest("Backup manifest exceeds its byte limit")
    key = _key(Path(key_file))
    nonce = os.urandom(12)
    header = MAGIC + nonce
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    destination = Path(destination)
    if destination.suffix != ".mwauthority":
        raise InvalidRequest("Authority backups must end in .mwauthority")
    if destination.exists() or destination.is_symlink():
        raise Conflict("Backup destination already exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, pending = tempfile.mkstemp(
        prefix=".authority-", suffix=".pending", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(header)
            sink = _EncryptingWriter(output, encryptor)
            with tarfile.open(fileobj=sink, mode="w|gz", format=tarfile.USTAR_FORMAT) as archive:
                entry = tarfile.TarInfo("manifest.json")
                entry.size, entry.mode = len(encoded), 0o600
                archive.addfile(entry, io.BytesIO(encoded))
                for description in descriptions:
                    stream, before = _regular(Path(members[description.name]))
                    with stream:
                        if before.st_size != description.bytes:
                            raise Conflict("Backup member changed after planning")
                        reader = _HashingReader(stream)
                        entry = tarfile.TarInfo(description.name)
                        entry.size, entry.mode = description.bytes, 0o600
                        archive.addfile(entry, reader)
                        if reader.digest.hexdigest() != description.sha256 or (
                            _fingerprint(before) != _fingerprint(os.fstat(stream.fileno()))
                        ):
                            raise Conflict("Backup member changed during archival")
            output.write(encryptor.finalize())
            output.write(encryptor.tag)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(pending, destination)
        except FileExistsError:
            raise Conflict("Backup destination already exists") from None
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return manifest
    finally:
        Path(pending).unlink(missing_ok=True)


def _decrypt(source: Path, key_file: Path, destination: Path) -> None:
    stream, before = _regular(source, MAX_ENCRYPTED_BACKUP_BYTES)
    try:
        # Encrypted archives may exceed the individual plaintext-member limit.
        if before.st_size > MAX_ENCRYPTED_BACKUP_BYTES:
            raise InvalidRequest("Encrypted backup exceeds its byte limit")
        header = stream.read(len(MAGIC) + 12)
        if len(header) != len(MAGIC) + 12 or not header.startswith(MAGIC):
            raise InvalidRequest("Invalid authority backup header")
        remaining = before.st_size - len(header) - 16
        if remaining < 1:
            raise InvalidRequest("Truncated authority backup")
        decryptor = Cipher(algorithms.AES(_key(key_file)), modes.GCM(header[-12:])).decryptor()
        decryptor.authenticate_additional_data(header)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            while remaining:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise InvalidRequest("Truncated authority backup")
                remaining -= len(chunk)
                output.write(decryptor.update(chunk))
            tag = stream.read(16)
            try:
                output.write(decryptor.finalize_with_tag(tag))
            except (InvalidTag, ValueError):
                raise InvalidRequest("Authority backup authentication failed") from None
            if _fingerprint(before) != _fingerprint(os.fstat(stream.fileno())):
                raise Conflict("Encrypted backup changed while being read")
    finally:
        stream.close()


@contextmanager
def read_backup(source: Path, key_file: Path):
    """Yield a verified private staging directory; clean it on every exit path."""
    with tempfile.TemporaryDirectory(prefix="memory-worlds-recovery-") as temporary:
        root = Path(temporary)
        plaintext = root / "authenticated.tar.gz"
        _decrypt(Path(source), Path(key_file), plaintext)
        target = root / "verified"
        target.mkdir(mode=0o700)
        try:
            with tarfile.open(plaintext, mode="r|gz") as archive:
                first = archive.next()
                if (
                    first is None
                    or first.name != "manifest.json"
                    or not first.isfile()
                    or (first.size > MAX_MANIFEST_BYTES)
                ):
                    raise InvalidRequest("Authority backup lacks its bounded manifest")
                with archive.extractfile(first) as stream:
                    manifest = AuthorityManifest.model_validate(
                        _load_json(
                            stream.read(MAX_MANIFEST_BYTES + 1),
                            "authority manifest",
                        )
                    )
                expected = {item.name: item for item in manifest.members}
                if len(expected) != len(manifest.members):
                    raise InvalidRequest("Duplicate backup member identity")
                if sum(item.bytes for item in manifest.members) > MAX_BACKUP_BYTES:
                    raise InvalidRequest("Backup expansion exceeds its byte limit")
                seen = set()
                while (entry := archive.next()) is not None:
                    relative = _name(entry.name)
                    description = expected.get(entry.name)
                    if (
                        not entry.isfile()
                        or description is None
                        or entry.name in seen
                        or (entry.size != description.bytes)
                    ):
                        raise InvalidRequest("Unexpected, duplicate or invalid backup member")
                    seen.add(entry.name)
                    path = target.joinpath(*relative.parts)
                    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with (
                        os.fdopen(descriptor, "wb") as output,
                        archive.extractfile(entry) as stream,
                    ):
                        while chunk := stream.read(_CHUNK):
                            size += len(chunk)
                            if size > description.bytes:
                                raise InvalidRequest("Backup member exceeds its declared size")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != description.bytes or digest.hexdigest() != description.sha256:
                        raise InvalidRequest("Backup member checksum mismatch")
                if seen != set(expected):
                    raise InvalidRequest("Backup members are missing")
            yield manifest, target
        except (ValidationError, tarfile.TarError, EOFError, UnicodeError, ValueError):
            raise InvalidRequest("Invalid authority backup data") from None
