"""Operator-configured backup storage, exposed only to live administrators."""

import os
import stat
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from .errors import NotFound, Unavailable
from .graph import GraphConfig
from .graph_snapshots import FalkorSnapshots
from .models import Model
from .recovery import backup_authority


class BackupRequest(Model):
    graph_mode: Literal["native", "rebuild"] = "native"


class BackupService:
    def __init__(self, store, directory: Path | None = None, key_file: Path | None = None):
        self.store, self.directory, self.key_file = store, directory, key_file

    @classmethod
    def from_env(cls, store):
        directory = os.environ.get("MEMORY_WORLDS_BACKUP_DIRECTORY")
        key = os.environ.get("MEMORY_WORLDS_BACKUP_KEY_FILE")
        return cls(store, Path(directory) if directory else None, Path(key) if key else None)

    def authorize(self, principal):
        with self.store._control() as db:
            self.store._admin(db, principal)
        if self.directory is None or self.key_file is None:
            raise Unavailable("Authority backups require an operator-configured directory and key")
        if not self.directory.is_absolute() or self.directory.is_symlink():
            raise Unavailable("Authority backup directory must be an absolute regular directory")
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not self.directory.is_dir():
            raise Unavailable("Authority backup directory is unavailable")

    def create(self, principal, request: BackupRequest):
        self.authorize(principal)
        identity = uuid4()
        destination = self.directory / f"{identity}.mwauthority"
        graph = None
        try:
            if request.graph_mode == "native":
                try:
                    config = GraphConfig.from_env()
                except ValueError:
                    raise Unavailable("Native backup requires valid graph configuration") from None
                if config is None:
                    raise Unavailable("Native backup requires a configured FalkorDB backend")
                graph = FalkorSnapshots(config)
            manifest = backup_authority(
                self.store, principal, destination, self.key_file, graph=graph
            )
        finally:
            if graph is not None:
                graph.close()
        return {
            "artifact_id": str(identity),
            "backup_id": str(manifest.backup_id),
            "authority_id": manifest.authority_id.hex,
            "graph_mode": manifest.graph_mode,
            "created_at": manifest.created_at.isoformat(),
            "bytes": destination.stat().st_size,
        }

    def list(self, principal):
        self.authorize(principal)
        result = []
        for path in self.directory.iterdir():
            if path.suffix != ".mwauthority":
                continue
            try:
                identity = UUID(path.stem)
            except ValueError:
                continue
            info = path.lstat()
            if str(identity) != path.stem or not stat.S_ISREG(info.st_mode):
                continue
            result.append(
                {
                    "artifact_id": str(identity),
                    "bytes": info.st_size,
                    "completed_at_unix": info.st_mtime,
                }
            )
            if len(result) > 1000:
                raise Unavailable(
                    "Backup catalog exceeds its listing limit; archive older artifacts"
                )
        return sorted(result, key=lambda item: item["completed_at_unix"], reverse=True)

    def open(self, principal, artifact_id: UUID):
        self.authorize(principal)
        path = self.directory / f"{artifact_id}.mwauthority"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (FileNotFoundError, OSError):
            raise NotFound("Backup artifact not found") from None
        source = os.fdopen(descriptor, "rb")
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            source.close()
            raise NotFound("Backup artifact not found")
        return source, info.st_size
