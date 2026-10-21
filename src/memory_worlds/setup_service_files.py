"""Publish owned service definitions without changing shared directory permissions."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from pathlib import Path

from .errors import Conflict, MemoryError
from .setup_files import _directory, _read_at, _rename_new, check_path


def _owned_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise MemoryError("service directory must be owned and not writable by other users")


def read_definition(path: Path) -> bytes | None:
    check_path(path)
    if not path.parent.exists():
        return None
    with _directory(path.parent) as directory:
        _owned_directory(directory)
        try:
            return _read_at(directory, path.name, maximum=65536, private=True, managed=True)
        except FileNotFoundError:
            return None


def publish_definition(path: Path, content: bytes, *, recorded: bool) -> None:
    """Only a pre-recorded intent permits adopting identical interrupted output."""
    check_path(path)
    with _directory(path.parent, create=True) as directory:
        _owned_directory(directory)
        try:
            actual = _read_at(directory, path.name, maximum=65536, private=True, managed=True)
        except FileNotFoundError:
            actual = None
        if actual is not None:
            if not recorded or actual != content:
                raise Conflict("service definition is unowned or was changed outside setup")
            return
        temporary = ".memory-worlds-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                _rename_new(directory, temporary, path.name)
            except FileExistsError:
                raise Conflict("service destination appeared during installation") from None
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def remove_definition(path: Path, expected: str) -> None:
    check_path(path)
    with _directory(path.parent) as directory:
        _owned_directory(directory)
        try:
            before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        content = _read_at(directory, path.name, maximum=65536, private=True, managed=True)
        if hashlib.sha256(content).hexdigest() != expected:
            raise Conflict("service definition was changed outside setup")
        after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (before.st_ino, before.st_dev, before.st_ctime_ns) != (
            after.st_ino,
            after.st_dev,
            after.st_ctime_ns,
        ):
            raise Conflict("service definition changed during removal")
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)
