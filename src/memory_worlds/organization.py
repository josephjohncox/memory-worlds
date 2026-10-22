"""Declarative shared spaces and identity-group access for one authority.

This file configures organization access, never private-agent scopes or automatic
publication. Role bundles compile to the service's existing world roles.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy import select

from .errors import Conflict, MemoryError
from .models import Alias, CreateWorld, Model, Role, WorldKind

MAX_ORGANIZATION_BYTES = 1024 * 1024
_REGISTRY_KEY = "organization:spaces"
_RANK = {Role.READER: 0, Role.WRITER: 1, Role.MANAGER: 2}
_Text = Annotated[str, Field(min_length=1, max_length=1000, strict=True)]
_References = Annotated[list[Alias], Field(min_length=1, max_length=1000)]


def _clean_text(value: str) -> str:
    if value != value.strip() or any(not character.isprintable() for character in value):
        raise ValueError("names must have no surrounding whitespace or control characters")
    return value


class OrganizationIdentity(Model):
    issuer: Annotated[str, Field(min_length=1, max_length=4096, strict=True)]
    audience: Annotated[str, Field(min_length=1, max_length=2000, strict=True)]
    jwks_url: Annotated[str, Field(min_length=1, max_length=4096, strict=True)]
    algorithms: list[_Text] = Field(default_factory=lambda: ["RS256"], min_length=1, max_length=20)
    groups_claim: _Text = "groups"
    client_ids: list[Annotated[str, Field(min_length=1, max_length=2000, strict=True)]] = Field(
        default_factory=list, max_length=100
    )
    client_id_claim: _Text = "cid"
    required_scopes: list[_Text] = Field(default_factory=list, max_length=100)
    scopes_claim: _Text = "scp"
    cache_ttl: float = Field(default=300, ge=1, le=3600, allow_inf_nan=False, strict=True)
    refresh_cooldown: float = Field(default=5, ge=0, le=60, allow_inf_nan=False, strict=True)
    request_timeout: float = Field(default=5, gt=0, le=30, allow_inf_nan=False, strict=True)

    @field_validator("algorithms", "client_ids", "required_scopes")
    @classmethod
    def unique_values(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("identity selections must be unique")
        return values

    def auth_values(self) -> dict:
        values = self.model_dump()
        for key in ("algorithms", "client_ids", "required_scopes"):
            values[key] = tuple(values[key])
        return values

    @model_validator(mode="after")
    def validate_verifier(self):
        # Import locally: AuthConfig loads organization files at its boundary.
        from .auth import AuthConfig

        AuthConfig(**self.auth_values())
        return self


class OrganizationSpace(Model):
    name: Annotated[str, Field(min_length=1, max_length=200, strict=True)]
    kind: Literal["company", "team", "ad-hoc"]
    alias: Alias | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        return _clean_text(value)


class OrganizationRole(Model):
    worlds: dict[Alias, Role] = Field(min_length=1, max_length=1000)


class OrganizationConfig(Model):
    identity: OrganizationIdentity
    spaces: dict[Alias, OrganizationSpace] = Field(default_factory=dict, max_length=1000)
    roles: dict[Alias, OrganizationRole] = Field(default_factory=dict, max_length=1000)
    groups: dict[_Text, _References] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode="after")
    def validate_references(self):
        aliases = set()
        for key, space in self.spaces.items():
            space.alias = space.alias or key
            if space.alias in aliases:
                raise ValueError("organization space aliases must be unique")
            aliases.add(space.alias)
        for role in self.roles.values():
            if role.worlds.keys() - self.spaces.keys():
                raise ValueError("organization roles must reference declared spaces")
        for group, roles in self.groups.items():
            _clean_text(group)
            if len(roles) != len(set(roles)):
                raise ValueError("group role references must be unique")
            if set(roles) - self.roles.keys():
                raise ValueError("organization groups must reference declared roles")
        return self

    @staticmethod
    def space_id(authority_id: str, key: str) -> str:
        return uuid5(UUID(authority_id), "organization:space:" + key).hex

    def auth_config(self):
        from .auth import AuthConfig

        return AuthConfig(**self.identity.auth_values(), organization=self)

    def _mappings(self, world_ids: dict[str, str]) -> dict[str, dict[str, Role]]:
        mappings = {}
        for group, role_names in self.groups.items():
            grants = {}
            for role_name in role_names:
                for key, role in self.roles[role_name].worlds.items():
                    world_id = world_ids[key]
                    if world_id not in grants or _RANK[role] > _RANK[grants[world_id]]:
                        grants[world_id] = role
            mappings[group] = grants
        return mappings

    def plan(self) -> dict:
        """Describe symbolic access without opening storage or contacting an IdP."""
        return {
            "identity": self.identity.model_dump(mode="json"),
            "spaces": {key: space.model_dump(mode="json") for key, space in self.spaces.items()},
            "roles": {key: role.model_dump(mode="json") for key, role in self.roles.items()},
            "groups": self.groups,
            "group_world_roles": {
                group: {key: role.value for key, role in grants.items()}
                for group, grants in self._mappings({key: key for key in self.spaces}).items()
            },
        }

    def reconcile(self, store) -> dict[str, dict[str, Role]]:
        """Create/update owned shared spaces atomically, retaining removed spaces."""
        world_ids = {key: self.space_id(store.authority_id, key) for key in self.spaces}
        with store._control() as db:
            metadata = store._table(db, "metadata")
            worlds = store._table(db, "worlds")
            raw_registry = db.execute(
                select(metadata.c.value).where(metadata.c.key == _REGISTRY_KEY)
            ).scalar_one_or_none()
            try:
                registry = json.loads(raw_registry) if raw_registry is not None else {}
                if not isinstance(registry, dict):
                    raise ValueError
                for key, record in registry.items():
                    if (
                        not isinstance(key, str)
                        or not isinstance(record, dict)
                        or set(record) != {"world_id", "kind"}
                        or record["world_id"] != self.space_id(store.authority_id, key)
                        or record["kind"] not in {"company", "team", "ad-hoc"}
                    ):
                        raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise Conflict("organization space registry is invalid") from None

            existing = {row["id"]: row for row in db.execute(select(worlds)).mappings()}
            # Preflight every record and collision before creating any source files.
            for record in registry.values():
                row = existing.get(record["world_id"])
                if row is None:
                    raise Conflict("a recorded organization space is missing; restore its data")
                if row["owner_id"] != "organization" or row["kind"] != record["kind"]:
                    raise Conflict("a recorded organization space has changed ownership or kind")
            for key, space in self.spaces.items():
                world_id = world_ids[key]
                if key in registry:
                    if registry[key]["kind"] != space.kind:
                        raise Conflict("organization space kind cannot change")
                elif world_id in existing:
                    raise Conflict("organization space ID collides with an unowned world")
                if any(
                    row["id"] != world_id and row["alias"] == space.alias
                    for row in existing.values()
                ):
                    raise Conflict("organization space alias collides with an existing world")

            for key, space in self.spaces.items():
                world_id = world_ids[key]
                if key not in registry:
                    store._new_world(
                        db,
                        CreateWorld(name=space.name, alias=space.alias, kind=WorldKind(space.kind)),
                        "organization",
                        prepared_id=world_id,
                    )
                    registry[key] = {"world_id": world_id, "kind": space.kind}
                else:
                    db.execute(
                        worlds.update()
                        .where(worlds.c.id == world_id)
                        .values(name=space.name, alias=space.alias)
                    )
            value = json.dumps(registry, sort_keys=True, separators=(",", ":"))
            if raw_registry is None:
                db.execute(metadata.insert().values(key=_REGISTRY_KEY, value=value))
            elif value != raw_registry:
                db.execute(
                    metadata.update().where(metadata.c.key == _REGISTRY_KEY).values(value=value)
                )
        return self._mappings(world_ids)


def load_organization(path: Path) -> OrganizationConfig:
    """Load bounded regular TOML, including projected Kubernetes ConfigMap links."""
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ORGANIZATION_BYTES:
                raise MemoryError(
                    "organization configuration must be a regular file of at most 1 MiB"
                )
            content = source.read(MAX_ORGANIZATION_BYTES + 1)
        if len(content) > MAX_ORGANIZATION_BYTES:
            raise MemoryError("organization configuration exceeds 1 MiB")
        return OrganizationConfig.model_validate(tomllib.loads(content.decode("utf-8")))
    except OSError:
        raise MemoryError("organization configuration file could not be read") from None
    except (ValueError, UnicodeError, ValidationError, RecursionError):
        raise MemoryError(
            "organization TOML does not satisfy the organization policy contract"
        ) from None
