"""Operator-configured remote authorities. Agent requests never choose URLs or credentials."""

import ipaddress
import json
import os
import stat
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .errors import Forbidden
from .errors import MemoryError as InvalidRequest
from .models import Identifier, Model, Principal


class MountRole(StrEnum):
    READER = "reader"
    PUBLISHER = "publisher"


class Mount(Model):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(max_length=2000)
    authority_id: Identifier
    world_ids: list[Identifier] = Field(min_length=1, max_length=50)
    token_file: Path = Field(repr=False, exclude=True)
    user_grants: dict[Identifier, MountRole] = Field(default_factory=dict)
    credential_grants: dict[Identifier, MountRole] = Field(default_factory=dict)
    publish: bool = False
    allow_insecure_http: bool = False

    @field_validator("url")
    @classmethod
    def endpoint(cls, value: str):
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("mount URL must be an HTTP(S) origin without credentials")
        return value.rstrip("/")

    def validate_transport(self):
        parsed = urlsplit(self.url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme == "http" and not loopback and not self.allow_insecure_http:
            raise InvalidRequest(
                "non-loopback mount URLs require HTTPS or explicit insecure opt-in"
            )

    def role(self, user: Principal) -> MountRole | None:
        # Scope delegation cannot recover broader federation access through the
        # originating user's mount grants. Remote credentials need independent
        # explicit delegation at their issuing authority.
        if user.restricted:
            return None
        if user.admin:
            return MountRole.PUBLISHER
        return self.credential_grants.get(user.credential_id, self.user_grants.get(user.user_id))

    def authorize(self, user: Principal, publish: bool = False):
        role = self.role(user)
        if role is None or (publish and (not self.publish or role != MountRole.PUBLISHER)):
            raise Forbidden("mount access denied")

    def token(self) -> str:
        info = self.token_file.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o027:
            raise InvalidRequest("mount credential must be a private regular file")
        if info.st_size > 32768:
            raise InvalidRequest("mount credential file exceeds size limit")
        token = self.token_file.read_text().strip()
        if not token or len(token) > 32768 or any(c.isspace() for c in token):
            raise InvalidRequest("mount credential file contains an invalid bearer token")
        return token


class MountView(Model):
    id: str
    name: str
    url: str
    authority_id: str
    world_ids: list[str]
    can_publish: bool


class MountConfig(Model):
    version: int = Field(default=1, ge=1, le=1)
    mounts: list[Mount] = Field(default_factory=list, max_length=50)

    @classmethod
    def from_env(cls):
        source = os.getenv("MEMORY_WORLDS_MOUNTS_FILE")
        if not source:
            return cls()
        path = Path(source).expanduser()
        if path.stat().st_size > 262144:
            raise InvalidRequest("mount configuration exceeds size limit")
        config = cls.model_validate(json.loads(path.read_text()))
        for mount in config.mounts:
            if not mount.token_file.is_absolute():
                mount.token_file = (path.parent / mount.token_file).resolve()
        return config.validated()

    def validated(self):
        if len({mount.id for mount in self.mounts}) != len(self.mounts):
            raise InvalidRequest("mount identifiers must be unique")
        for mount in self.mounts:
            mount.validate_transport()
            if len(set(mount.world_ids)) != len(mount.world_ids):
                raise InvalidRequest("mount worlds must be unique")
        return self
