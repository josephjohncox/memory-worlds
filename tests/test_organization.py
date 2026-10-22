"""Organization policy names spaces without weakening existing scope authority."""

import copy
import json
import os
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from memory_worlds.errors import Conflict, MemoryError
from memory_worlds.models import CreateWorld, EpisodeInput, Role, WorldKind
from memory_worlds.organization import (
    MAX_ORGANIZATION_BYTES,
    OrganizationConfig,
    load_organization,
)
from memory_worlds.store import MemoryStore


def policy_values():
    return {
        "identity": {
            "issuer": "https://identity.example.test/oauth2/memory",
            "audience": "api://memory-worlds",
            "jwks_url": "https://identity.example.test/oauth2/memory/keys",
            "client_ids": ["native-client"],
            "required_scopes": ["memory-worlds"],
        },
        "spaces": {
            "company": {"name": "Company", "kind": "company"},
            "data": {"name": "Data team", "kind": "team"},
            "incident": {"name": "Incident review", "kind": "ad-hoc"},
        },
        "roles": {
            "employee": {"worlds": {"company": "reader"}},
            "data-member": {"worlds": {"company": "reader", "data": "writer"}},
            "data-lead": {"worlds": {"data": "manager"}},
            "responder": {"worlds": {"incident": "writer"}},
        },
        "groups": {
            "All employees": ["employee"],
            "Data engineering": ["employee", "data-member"],
            "Data leads": ["data-member", "data-lead"],
            "Incident 42": ["responder"],
        },
    }


@pytest.fixture
def authority(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    _, token = store.bootstrap("operator")
    return store, store.authenticate(token)


def test_first_startup_and_restart_keep_world_ids_and_source_journals(authority):
    store, admin = authority
    config = OrganizationConfig.model_validate(policy_values())
    mappings = config.reconcile(store)
    data_id = config.space_id(store.authority_id, "data")
    company_id = config.space_id(store.authority_id, "company")
    assert mappings["Data leads"] == {company_id: Role.READER, data_id: Role.MANAGER}
    world = store.get_world(admin, data_id)
    assert (world.alias, world.kind, world.owner_id) == ("data", WorldKind.TEAM, "organization")
    receipt = store.capture(
        admin,
        data_id,
        EpisodeInput(
            source_id="review-42", content="A verified finding", occurred_at=datetime.now(UTC)
        ),
        shared=True,
    )
    reopened = MemoryStore(store.data_dir)
    assert config.reconcile(reopened) == mappings
    assert reopened.get_world(admin, data_id).created_at == world.created_at
    assert reopened.get_episode(admin, data_id, receipt.episode.id) == receipt.episode
    assert len(reopened.list_worlds(admin)) == 4


def test_names_and_aliases_can_change_without_changing_identity(authority):
    store, admin = authority
    config = OrganizationConfig.model_validate(policy_values())
    config.reconcile(store)
    data_id = config.space_id(store.authority_id, "data")
    scope_id = store.get_world(admin, data_id).scope_id
    values = policy_values()
    values["spaces"]["data"].update(name="Data platform", alias="platform")
    OrganizationConfig.model_validate(values).reconcile(store)
    world = store.get_world(admin, data_id)
    assert (world.name, world.alias, world.scope_id) == ("Data platform", "platform", scope_id)


def test_removed_declarations_retain_data_and_manual_memberships(authority):
    store, admin = authority
    config = OrganizationConfig.model_validate(policy_values())
    config.reconcile(store)
    data_id = config.space_id(store.authority_id, "data")
    store.set_membership(admin, data_id, "invited-user", Role.READER)
    receipt = store.capture(
        admin,
        data_id,
        EpisodeInput(source_id="keep", content="Keep this evidence", occurred_at=datetime.now(UTC)),
        shared=True,
    )
    values = policy_values()
    values.update(spaces={}, roles={}, groups={})
    assert OrganizationConfig.model_validate(values).reconcile(store) == {}
    assert store.get_episode(admin, data_id, receipt.episode.id) == receipt.episode
    assert store.list_memberships(admin, data_id) == [
        {"user_id": "invited-user", "world_id": data_id, "role": "reader", "source": "manual"}
    ]
    # Reintroducing the same stable key recovers its original space.
    config.reconcile(store)
    assert store.get_episode(admin, data_id, receipt.episode.id) == receipt.episode


@pytest.mark.parametrize("collision", ["alias", "id"])
def test_collision_preflight_does_not_partially_create_spaces(authority, collision):
    store, admin = authority
    config = OrganizationConfig.model_validate(policy_values())
    if collision == "alias":
        store.create_world(
            admin, CreateWorld(name="Unmanaged private data", alias="data", kind=WorldKind.PERSONAL)
        )
    else:
        with store._control() as db:
            store._new_world(
                db,
                CreateWorld(name="Unmanaged team", kind=WorldKind.TEAM),
                "organization",
                prepared_id=config.space_id(store.authority_id, "data"),
            )
    before = store.list_worlds(admin)
    with pytest.raises(Conflict, match="collides"):
        config.reconcile(store)
    assert store.list_worlds(admin) == before
    with store._control() as db:
        metadata = store._table(db, "metadata")
        assert (
            db.execute(
                select(metadata.c.value).where(metadata.c.key == "organization:spaces")
            ).scalar_one_or_none()
            is None
        )


def test_kind_cannot_change_and_failure_rolls_back_renames(authority):
    store, admin = authority
    config = OrganizationConfig.model_validate(policy_values())
    config.reconcile(store)
    values = policy_values()
    values["spaces"]["company"]["name"] = "Must not persist"
    values["spaces"]["data"]["kind"] = "company"
    with pytest.raises(Conflict, match="kind cannot change"):
        OrganizationConfig.model_validate(values).reconcile(store)
    company_id = config.space_id(store.authority_id, "company")
    assert store.get_world(admin, company_id).name == "Company"


def test_missing_recorded_space_is_not_silently_recreated(authority):
    store, _ = authority
    config = OrganizationConfig.model_validate(policy_values())
    config.reconcile(store)
    with store._control() as db:
        metadata = store._table(db, "metadata")
        registry = json.loads(
            db.execute(
                select(metadata.c.value).where(metadata.c.key == "organization:spaces")
            ).scalar_one()
        )
        registry["missing"] = {
            "world_id": config.space_id(store.authority_id, "missing"),
            "kind": "team",
        }
        db.execute(
            metadata.update()
            .where(metadata.c.key == "organization:spaces")
            .values(value=json.dumps(registry))
        )
    # Removed declarations are also checked, not just currently configured spaces.
    with pytest.raises(Conflict, match="recorded organization space is missing"):
        config.reconcile(store)


@pytest.mark.parametrize("value", ["null", "[]", '{"data": {"world_id": "forged"}}'])
def test_invalid_registry_is_not_overwritten(authority, value):
    store, _ = authority
    with store._control() as db:
        metadata = store._table(db, "metadata")
        db.execute(metadata.insert().values(key="organization:spaces", value=value))
    config = OrganizationConfig.model_validate(policy_values())
    with pytest.raises(Conflict, match="registry is invalid"):
        config.reconcile(store)
    with store._control() as db:
        metadata = store._table(db, "metadata")
        assert (
            db.execute(
                select(metadata.c.value).where(metadata.c.key == "organization:spaces")
            ).scalar_one()
            == value
        )


@pytest.mark.parametrize("change", [{"owner_id": "somebody-else"}, {"kind": "private-work"}])
def test_registry_ownership_or_kind_drift_is_not_adopted(authority, change):
    store, _ = authority
    config = OrganizationConfig.model_validate(policy_values())
    config.reconcile(store)
    data_id = config.space_id(store.authority_id, "data")
    with store._control() as db:
        worlds = store._table(db, "worlds")
        db.execute(worlds.update().where(worlds.c.id == data_id).values(**change))
    with pytest.raises(Conflict, match="ownership or kind"):
        config.reconcile(store)


def test_same_space_key_is_namespaced_by_authority(tmp_path):
    first = MemoryStore(tmp_path / "first")
    second = MemoryStore(tmp_path / "second")
    config = OrganizationConfig.model_validate(policy_values())
    first_id = config.reconcile(first)["Data engineering"]
    second_id = config.reconcile(second)["Data engineering"]
    assert first_id.keys().isdisjoint(second_id.keys())


@pytest.mark.parametrize(
    "change",
    [
        {"extra": True},
        {"spaces": {"private": {"name": "Private", "kind": "private-work"}}},
        {"spaces": {"bad key": {"name": "Data", "kind": "team"}}},
        {"spaces": {"empty": {"name": " ", "kind": "team"}}},
        {"roles": {"employee": {"worlds": {"absent": "reader"}}}},
        {"roles": {"employee": {"worlds": {"company": "administrator"}}}},
        {"groups": {"Staff": ["unknown"]}},
        {"groups": {"Staff": ["employee", "employee"]}},
        {"groups": {" Staff": ["employee"]}},
        {"groups": {"Staff\nadmin": ["employee"]}},
        {"groups": {"x" * 1001: ["employee"]}},
        {
            "spaces": {
                "a": {"name": "A", "kind": "company", "alias": "same"},
                "b": {"name": "B", "kind": "team", "alias": "same"},
            }
        },
    ],
)
def test_invalid_policy_fails_before_storage_is_opened(change, tmp_path):
    values = policy_values() | copy.deepcopy(change)
    with pytest.raises(ValidationError):
        OrganizationConfig.model_validate(values)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "http://identity.example.test"},
        {"jwks_url": "https://identity.example.test/keys?token=secret"},
        {"algorithms": ["HS256"]},
        {"algorithms": ["RS256", "RS256"]},
        {"client_ids": ["api://memory-worlds"]},
        {"required_scopes": ["contains whitespace"]},
        {"groups_claim": ""},
        {"cache_ttl": True},
        {"request_timeout": float("nan")},
        {"admin_claim": "superuser"},
    ],
)
def test_identity_uses_the_pinned_verifier_contract(change):
    values = policy_values()
    values["identity"].update(change)
    with pytest.raises(ValidationError):
        OrganizationConfig.model_validate(values)


def test_auth_config_and_offline_symbolic_plan_do_not_open_storage(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("planning must not create storage")

    monkeypatch.setattr(MemoryStore, "__init__", forbidden)
    config = OrganizationConfig.model_validate(policy_values())
    auth = config.auth_config()
    assert auth.organization is config
    assert auth.issuer == config.identity.issuer
    assert auth.client_ids == ("native-client",)
    assert auth.required_scopes == ("memory-worlds",)
    assert auth.group_mappings == {}
    plan = config.plan()
    assert plan["spaces"]["data"]["alias"] == "data"
    assert plan["group_world_roles"]["Data leads"] == {"company": "reader", "data": "manager"}
    json.dumps(plan)


def minimal_toml():
    return """[identity]
issuer = "https://identity.example.test"
audience = "api://memory-worlds"
jwks_url = "https://identity.example.test/keys"
[spaces.company]
name = "Company"
kind = "company"
[roles.employee]
worlds = {company = "reader"}
[groups]
"All employees" = ["employee"]
"""


def test_loader_accepts_projected_configmap_symlinks(tmp_path):
    revision = tmp_path / "..2026_10_21"
    revision.mkdir()
    (revision / "organization.toml").write_text(minimal_toml())
    (tmp_path / "..data").symlink_to(revision.name, target_is_directory=True)
    path = tmp_path / "organization.toml"
    path.symlink_to("..data/organization.toml")
    assert load_organization(path).spaces["company"].alias == "company"


@pytest.mark.parametrize(
    "content",
    [
        b"x" * (MAX_ORGANIZATION_BYTES + 1),
        b"\xff",
        b"[identity",
        b'password = "DO-NOT-PRINT-THIS"',
    ],
)
def test_loader_rejects_oversized_or_invalid_files_without_echoing_input(tmp_path, content):
    path = tmp_path / "organization.toml"
    path.write_bytes(content)
    with pytest.raises(MemoryError) as caught:
        load_organization(path)
    assert "DO-NOT-PRINT-THIS" not in str(caught.value)


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo"])
def test_loader_requires_a_regular_file_and_never_blocks_on_fifo(tmp_path, kind):
    path = tmp_path / "organization.toml"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    with pytest.raises(MemoryError):
        load_organization(path)
