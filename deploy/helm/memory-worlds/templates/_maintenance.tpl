{{- define "memory-worlds.maintenanceStage" -}}
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def copy_secret(source, base):
    # Pin one kubelet projection generation so config and keys cannot mix.
    if source.is_symlink() or base.is_symlink():
        raise ValueError()
    generation = (source / "..data").resolve(strict=True)
    if generation.parent != source.resolve(strict=True) or not generation.is_dir():
        raise ValueError()
    directory = os.open(generation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = sorted(name for name in os.listdir(directory) if not name.startswith("."))
        if "maintenance.toml" not in names or not 1 <= len(names) <= 128:
            raise ValueError()
        files = {}
        total = 0
        for name in names:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", name) is None:
                raise ValueError()
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 262144:
                    raise ValueError()
                value = stream.read(262145)
                after = os.fstat(stream.fileno())
                if len(value) > 262144 or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError()
            total += len(value)
            if total > 1048576:
                raise ValueError()
            files[name] = value
    finally:
        os.close(directory)
    target = base / "maintenance"
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()
        if set(os.listdir(target)) != set(files):
            raise ValueError()
        for name, value in files.items():
            existing = target / name
            info = existing.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != len(value):
                raise ValueError()
            descriptor = os.open(existing, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as current:
                actual = os.fstat(current.fileno())
                if actual.st_ino != info.st_ino or actual.st_dev != info.st_dev or current.read(262145) != value:
                    raise ValueError()
        return
    staging = Path(tempfile.mkdtemp(prefix=".maintenance-", dir=base))
    os.chmod(staging, 0o700)
    try:
        for name, value in files.items():
            descriptor = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


try:
    os.umask(0o077)
    copy_secret(Path(sys.argv[1]), Path(sys.argv[2]))
except Exception:
    raise SystemExit("Maintenance Secret staging failed; verify its files, projection and limits") from None
{{- end -}}
