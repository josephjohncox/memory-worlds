import fcntl
import os
import stat
from pathlib import Path

from .errors import Conflict, MemoryError


class DaemonLock:
    """Advisory lifetime lock: one daemon process per local data directory."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "daemon.lock"
        self.fd: int | None = None

    def __enter__(self):
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            raise MemoryError("daemon lock cannot be opened safely") from None
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(fd)
            raise MemoryError("daemon lock must be an owned private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise Conflict("a daemon already owns this data directory") from None
        self.fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
