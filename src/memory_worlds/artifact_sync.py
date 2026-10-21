"""Copy authenticated immutable backup artifacts through an operator rclone remote."""

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from pydantic import Field, field_validator

from .errors import MemoryError as InvalidRequest
from .errors import Unavailable
from .models import Model
from .recovery_archive import MAX_ENCRYPTED_BACKUP_BYTES, read_backup


class ArtifactSyncConfig(Model):
    remote: str = Field(min_length=3, max_length=2000)
    rclone_config: Path

    @field_validator("remote")
    @classmethod
    def named_remote(cls, value):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*:[^\r\n\x00]*", value):
            raise ValueError("Use an explicitly configured named rclone remote")
        return value.rstrip("/")


def _run(binary, arguments, config):
    # Provider credentials come only from the selected private rclone config.
    # Preserve the optional config decryption password, never service tokens or
    # ambient RCLONE_CONFIG_* remote overrides.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "TMPDIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "RCLONE_CONFIG_PASS",
            "SYSTEMROOT",
        }
    }
    try:
        result = subprocess.run(
            [
                binary,
                *arguments,
                "--config",
                str(config),
                "--log-level",
                "ERROR",
                "--retries",
                "1",
                "--low-level-retries",
                "1",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Unavailable("Artifact transport could not complete") from None
    if result.returncode:
        raise Unavailable("Artifact transport or downloaded-byte verification failed")


def sync_artifact(
    archive: Path, key_file: Path, config: ArtifactSyncConfig, *, binary="rclone"
) -> dict:
    """Authenticate a private snapshot, copy without overwrite, verify downloaded bytes."""
    descriptor = os.open(config.rclone_config, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise InvalidRequest("rclone configuration must be a private regular file")
    finally:
        os.close(descriptor)
    with tempfile.TemporaryDirectory(prefix="memory-worlds-artifact-sync-") as temporary:
        folder = Path(temporary)
        staged = folder / "artifact.pending"
        descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENCRYPTED_BACKUP_BYTES:
                raise InvalidRequest("Artifact must be a bounded completed authority backup")
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while chunk := source.read(65536):
                    size += len(chunk)
                    if size > MAX_ENCRYPTED_BACKUP_BYTES:
                        raise InvalidRequest("Artifact exceeds its transport limit")
                    output.write(chunk)
                    digest.update(chunk)
            after = os.fstat(source.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise InvalidRequest("Artifact changed while preparing its transfer")
        with read_backup(staged, key_file) as (manifest, _verified):
            identity = f"{manifest.backup_id}-{digest.hexdigest()}.mwauthority"
            destination = f"{config.remote}/{manifest.authority_id.hex}"
            staged.rename(folder / identity)
            _run(
                binary,
                [
                    "copyto",
                    str(folder / identity),
                    f"{destination}/{identity}",
                    "--immutable",
                    "--ignore-existing",
                    "--checksum",
                    "--no-traverse",
                ],
                config.rclone_config,
            )
            # Download comparison verifies backends that expose no common hash.
            # one-way checks this artifact and leaves all other remote files alone.
            _run(
                binary,
                ["check", str(folder), destination, "--download", "--one-way", "--checkers", "1"],
                config.rclone_config,
            )
            return {
                "authority_id": manifest.authority_id.hex,
                "backup_id": str(manifest.backup_id),
                "artifact": identity,
                "bytes": size,
                "sha256": digest.hexdigest(),
                "verified": "downloaded-bytes",
                "remote": config.remote.split(":", 1)[0],
            }
