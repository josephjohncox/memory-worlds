"""The single TOML configuration contract for local and hybrid setup."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import MemoryError
from .setup_files import check_path, private_read, write_private


def _url(value: str, *, origin: bool = False, secure: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (origin and parsed.path not in ("", "/"))
            or any(c.isspace() or ord(c) < 32 for c in value)
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError
        loopback = parsed.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if secure and parsed.scheme != "https" and not loopback:
            raise ValueError
    except ValueError:
        raise ValueError(
            "URL must have a permitted HTTP(S) destination and no credentials"
        ) from None
    return value.rstrip("/")


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai", "openai-compatible"]
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = None
    api_key_file: Path | None = None

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("model name must be nonblank without control characters")
        return value

    @field_validator("base_url")
    @classmethod
    def provider_url(cls, value: str | None) -> str | None:
        return _url(value) if value is not None else None

    @model_validator(mode="after")
    def provider_contract(self):
        if self.provider == "openai-compatible" and not self.base_url:
            raise ValueError("openai-compatible providers require an explicit base_url")
        if self.provider == "openai" and self.api_key_file is None:
            raise ValueError("OpenAI providers require an api_key_file")
        return self


class EmbeddingSettings(ProviderSettings):
    batch_size: int = Field(default=1, ge=1, le=2048, strict=True)


class CorporateSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    issuer: str | None = None
    client_id: str | None = Field(default=None, min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["openid", "memory-worlds"], max_length=20)
    token_file: Path | None = None
    world_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("url")
    @classmethod
    def service_url(cls, value: str) -> str:
        return _url(value, origin=True, secure=True)

    @field_validator("issuer")
    @classmethod
    def issuer_url(cls, value: str | None) -> str | None:
        return _url(value, secure=True) if value is not None else None

    @field_validator("scopes", "world_ids")
    @classmethod
    def identifiers(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", value) for value in values
        ):
            raise ValueError("selections must be unique identifiers")
        if "offline_access" in values:
            raise ValueError("setup does not request refresh credentials")
        return values

    @field_validator("world_ids")
    @classmethod
    def world_identifiers(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value) for value in values):
            raise ValueError("world selections must use canonical world identifiers")
        return values

    @model_validator(mode="after")
    def authentication(self):
        if self.token_file is None and not (self.issuer and self.client_id):
            raise ValueError("corporate connection requires a token file or issuer and client_id")
        return self


class SetupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance: str = Field(default="default", pattern=r"^[a-z][a-z0-9-]{0,39}$")
    mode: Literal["local", "hybrid"] = "local"
    owner: str = Field(default="owner", pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    data_dir: Path | None = None
    state_dir: Path | None = None
    adopt_admin_file: Path | None = None
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535, strict=True)
    graph_port: int = Field(default=16379, ge=1024, le=65535, strict=True)
    extraction: ProviderSettings
    embedding: EmbeddingSettings
    embedding_dimensions: int = Field(default=1536, ge=1, le=65536, strict=True)
    pi_profiles: bool = Field(default=True, strict=True)
    mcp_profiles: bool = Field(default=False, strict=True)
    corporate: CorporateSettings | None = None

    @model_validator(mode="after")
    def paths_and_mode(self):
        self.data_dir = check_path(
            self.data_dir or Path.home() / ".local/share/memory-worlds" / self.instance
        )
        self.state_dir = check_path(
            self.state_dir or Path.home() / ".local/state/memory-worlds" / self.instance
        )
        if self.adopt_admin_file is not None:
            # A per-operation override can supersede a stale external reference.
            # Keep the saved value in the digest; inspect only the selected file
            # when validate/apply consumes it.
            self.adopt_admin_file = self.adopt_admin_file.expanduser().absolute()
            if ".." in self.adopt_admin_file.parts:
                raise MemoryError("setup paths must not contain parent traversal")
        for path in (self.data_dir, self.state_dir):
            if path in (Path("/"), Path.home(), Path("/tmp")):
                raise ValueError("setup requires dedicated data and state directories")
            if path.exists() and not path.is_dir():
                raise ValueError("setup data and state paths must be directories")
        if (
            self.data_dir == self.state_dir
            or self.data_dir in self.state_dir.parents
            or self.state_dir in self.data_dir.parents
        ):
            raise ValueError("setup data and state directories must be separate")
        if self.port == self.graph_port:
            raise ValueError("API and graph ports must differ")
        if (self.mode == "hybrid") != (self.corporate is not None):
            raise ValueError("only hybrid setup requires a corporate section")
        for settings in (self.extraction, self.embedding, self.corporate):
            if settings is None:
                continue
            field = "token_file" if isinstance(settings, CorporateSettings) else "api_key_file"
            path = getattr(settings, field)
            if path is not None:
                setattr(settings, field, check_path(path))
        return self

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
            raise MemoryError("setup file name is invalid")
        return self.state_dir / name


def load_config(path: Path) -> SetupConfig:
    path = check_path(path)
    try:
        values = tomllib.loads(private_read(path, private=False, managed=True).decode())
        for key in ("data_dir", "state_dir", "adopt_admin_file"):
            if key in values and isinstance(values[key], str):
                target = Path(values[key]).expanduser()
                values[key] = target if target.is_absolute() else path.parent / target
        for section, key in (
            ("extraction", "api_key_file"),
            ("embedding", "api_key_file"),
            ("corporate", "token_file"),
        ):
            table = values.get(section)
            if isinstance(table, dict) and isinstance(table.get(key), str):
                target = Path(table[key]).expanduser()
                table[key] = target if target.is_absolute() else path.parent / target
        return SetupConfig.model_validate(values)
    except (ValueError, UnicodeError, ValidationError, RecursionError):
        raise MemoryError(
            "setup TOML does not satisfy the current setup configuration contract"
        ) from None


def config_bytes(config: SetupConfig) -> bytes:
    """Render the single current TOML format without publishing a file."""
    values = config.model_dump(mode="json", exclude_none=True)
    lines = ["# Memory Worlds setup; credentials are referenced by private file path."]

    def assignment(key, value):
        return f"{key} = {json.dumps(value, ensure_ascii=False)}"

    for key, value in values.items():
        if not isinstance(value, dict):
            lines.append(assignment(key, value))
    for key, value in values.items():
        if isinstance(value, dict):
            lines.extend(["", f"[{key}]"])
            lines.extend(assignment(field, content) for field, content in value.items())
    return ("\n".join(lines) + "\n").encode()


def save_config(config: SetupConfig, path: Path, *, expected_hash: str | None = None) -> str:
    return write_private(path, config_bytes(config), expected_hash=expected_hash)
