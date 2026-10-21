"""Private, bounded filesystem operations for the guided setup controller."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import Conflict, MemoryError

if TYPE_CHECKING:
    from .setup_config import SetupConfig


def check_path(path: Path) -> Path:
    """Inspect existing components without creating anything; IO also uses dirfds."""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise MemoryError("setup paths must not contain parent traversal")
    for part in reversed((path, *path.parents)):
        try:
            mode = part.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise MemoryError("setup paths must not contain symbolic links")
        if part != path and not stat.S_ISDIR(mode):
            raise MemoryError("setup path parent must be a directory")
    return path


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Walk from / through held descriptors; NOFOLLOW applies to every component."""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise MemoryError("setup paths must not contain parent traversal")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError:
        raise MemoryError("setup path cannot be opened without following symbolic links") from None
    finally:
        os.close(descriptor)


def _private_directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise MemoryError("setup requires an owned private directory with mode 0700")


def ensure_private_dir(path: Path) -> None:
    with _directory(check_path(path), create=True) as descriptor:
        _private_directory(descriptor)


def _read_at(directory: int, name: str, *, maximum: int, private: bool) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise MemoryError("setup input must be a bounded regular file")
        if private and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077):
            raise MemoryError("setup secret and state files must be owned with mode 0600")
        content = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
        if len(content) > maximum or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise Conflict("setup input changed while being read")
        return content


def private_read(path: Path, maximum: int = 1_048_576, *, private: bool = True) -> bytes:
    path = check_path(path)
    try:
        with _directory(path.parent) as directory:
            return _read_at(directory, path.name, maximum=maximum, private=private)
    except OSError:
        raise MemoryError("setup input file cannot be read safely") from None


def write_private(path: Path, data: bytes | str, *, expected_hash: str | None = None) -> str:
    path = check_path(path)
    ensure_private_dir(path.parent)
    content = data.encode() if isinstance(data, str) else data
    digest = hashlib.sha256(content).hexdigest()
    with _directory(path.parent) as directory:
        _private_directory(directory)
        try:
            original = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            original = None
        if original is not None:
            if expected_hash is None:
                raise Conflict("setup refuses to overwrite an unowned file")
            actual = _read_at(directory, path.name, maximum=1_048_576, private=True)
            if hashlib.sha256(actual).hexdigest() != expected_hash:
                raise Conflict("setup managed file was changed outside setup")
            if digest == expected_hash:
                return digest
        elif expected_hash is not None:
            raise Conflict("setup managed file is missing")
        temporary = ".setup-" + secrets.token_hex(16)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if original is None:
                try:
                    os.link(
                        temporary,
                        path.name,
                        src_dir_fd=directory,
                        dst_dir_fd=directory,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise Conflict("setup destination appeared during creation") from None
            else:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (
                    original.st_dev,
                    original.st_ino,
                    original.st_mtime_ns,
                    original.st_ctime_ns,
                ):
                    raise Conflict("setup managed file changed during replacement")
                os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return digest


def unlink_private(path: Path, *, expected_hash: str) -> None:
    """Remove only an unchanged managed file through its held parent descriptor."""
    path = check_path(path)
    with _directory(path.parent) as directory:
        _private_directory(directory)
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        content = _read_at(directory, path.name, maximum=1_048_576, private=True)
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise Conflict("setup refuses to remove a changed managed file")
        after = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise Conflict("setup managed file changed during removal")
        os.unlink(path.name, dir_fd=directory)
        os.fsync(directory)


@contextmanager
def setup_lock(config: SetupConfig):
    ensure_private_dir(config.state_dir)
    path = check_path(config.path("setup.lock"))
    with _directory(path.parent) as directory:
        _private_directory(directory)
        fd = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise MemoryError("setup lock must be an owned private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Conflict("another setup operation is running") from None
        yield
    finally:
        os.close(fd)


def read_json(path: Path) -> dict:
    def invalid_constant(_value):
        raise ValueError

    try:
        result = json.loads(private_read(path), parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise MemoryError("setup state must contain valid JSON") from None
    if not isinstance(result, dict):
        raise MemoryError("setup state must be an object")
    return result


def load_state(config: SetupConfig, *, require_digest: bool = True) -> dict:
    state = read_json(config.path("state.json"))
    if state.get("instance") != config.instance:
        raise Conflict("setup state belongs to another instance")
    if require_digest and state.get("config_digest") != config.digest:
        raise Conflict("setup config changed; review and apply the configuration first")
    if any(
        not isinstance(state.get(key), str) or not state[key]
        for key in ("authority_id", "user_id", "compose_project")
    ) or any(not isinstance(state.get(key), dict) for key in ("resources", "files")):
        raise MemoryError("setup state is incomplete; apply setup to recover it")
    return state
