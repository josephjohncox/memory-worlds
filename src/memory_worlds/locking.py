import fcntl
import os
from pathlib import Path

from .errors import Conflict


class DaemonLock:
    """Advisory lifetime lock: one daemon process per local data directory."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "daemon.lock"
        self.fd: int | None = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
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
