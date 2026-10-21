import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import memory_worlds.setup_provision as provisioning
from memory_worlds.errors import Conflict, Forbidden, MemoryError, Unauthorized
from memory_worlds.locking import DaemonLock
from memory_worlds.models import CreateScope, CreateToken, EpisodeInput, Role, UpdateScopeGrant
from memory_worlds.mounts import MountConfig
from memory_worlds.setup_config import CorporateSettings, ProviderSettings, SetupConfig
from memory_worlds.setup_files import private_read, read_json, setup_lock, write_private
from memory_worlds.setup_provision import apply_setup
from memory_worlds.store import MemoryStore


def config_for(tmp_path, *, hybrid=False):
    provider = ProviderSettings(
        provider="openai-compatible", model="local-model", base_url="http://127.0.0.1:9000/v1"
    )
    return SetupConfig(
        instance="test",
        owner="alice",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        extraction=provider,
        embedding=provider,
        mcp_profiles=True,
        mode="hybrid" if hybrid else "local",
        corporate=CorporateSettings(
            url="https://corporate.example.test",
            token_file=tmp_path / "input.token",
            world_ids=["team", "company"],
        )
        if hybrid
        else None,
    )


def bind_corporate(config, *, worlds=None):
    data = b"synthetic-corporate-access-token\n"
    write_private(config.path("corporate.token"), data)
    write_private(
        config.path("corporate.json"),
        json.dumps(
            {
                "url": config.corporate.url,
                "authority_id": "corporate-authority",
                "user_id": "user_corporate",
                "credential_id": "corporate-session",
                "world_ids": config.corporate.world_ids if worlds is None else worlds,
                "token_file": str(config.path("corporate.token")),
                "token_sha256": hashlib.sha256(data).hexdigest(),
            }
        ),
    )


def token(config, kind):
    return private_read(config.path(f"{kind}.token")).decode().strip()


def snapshot(path):
    return {
        str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()
    }


def test_local_setup_has_durable_canonical_ids_and_nonadministrative_profiles(tmp_path):
    config = config_for(tmp_path)
    receipt = apply_setup(config)
    state = read_json(config.path("state.json"))
    store = MemoryStore(config.data_dir)
    personal = state["resources"]["personal"]
    agent = store.authenticate(token(config, "personal"))
    assert receipt["authority_id"] == state["authority_id"] == store.authority_id
    assert agent.credential_id == personal["credential_id"]
    assert not agent.admin and agent.user_id == "alice" and not agent.restricted
    assert store.get_scope(
        agent, personal["world_id"], personal["scope_id"]
    ).agent_id.hex == personal["agent_id"].replace("-", "")
    with store._control() as db:
        metadata = store._table(db, "metadata")
        bound = json.loads(
            db.execute(select(metadata.c.value).where(metadata.c.key == "setup:test")).scalar_one()
        )
        assert bound["resources"] == state["resources"]
        assert bound["authority_id"] == store.authority_id
    profile = read_json(config.path("pi-personal.json"))
    assert profile["readMountIds"] == []
    assert profile["tokenFile"] == personal["token_file"]
    assert profile["automaticCapture"] is False
    assert profile["retrieval"]["enabled"] is False
    assert profile["projectCaptured"] is True
    assert profile["writeScope"] == {
        "world_id": personal["world_id"],
        "scope_id": personal["scope_id"],
    }
    stored = (
        json.dumps(receipt)
        + config.path("pi-personal.json").read_text()
        + config.path("mcp-personal.json").read_text()
    )
    for kind in ("operator", "personal", "graph"):
        assert token(config, kind) not in stored
    assert str(config.path("operator.token")) not in config.path("mcp-personal.json").read_text()
    assert all(
        config.path(name).stat().st_mode & 0o077 == 0
        for name in (
            "operator.token",
            "personal.token",
            "graph.token",
            "state.json",
            "provision.json",
        )
    )


def test_exact_repeated_apply_preserves_ids_secrets_and_sources(tmp_path):
    config = config_for(tmp_path)
    first = apply_setup(config)
    state = read_json(config.path("state.json"))
    store = MemoryStore(config.data_dir)
    target = state["resources"]["personal"]
    agent = store.authenticate(token(config, "personal"))
    source = store.capture(
        agent,
        target["world_id"],
        EpisodeInput(
            scope_id=target["scope_id"],
            source_id="durable-note",
            content="Keep this source private",
            occurred_at=datetime.now(UTC),
            project=False,
        ),
    )
    files = snapshot(config.state_dir)
    second = apply_setup(config)
    assert second == {**first, "replayed": True}
    assert snapshot(config.state_dir) == files
    assert (
        store.get_episode(
            agent, target["world_id"], source.episode.id, scope_id=target["scope_id"]
        ).content
        == "Keep this source private"
    )


def test_hybrid_credential_only_reader_mount_and_private_world_isolation(tmp_path):
    config = config_for(tmp_path, hybrid=True)
    bind_corporate(config)
    apply_setup(config)
    state = read_json(config.path("state.json"))
    store = MemoryStore(config.data_dir)
    personal = store.authenticate(token(config, "personal"))
    work = store.authenticate(token(config, "work"))
    mount = MountConfig.model_validate(read_json(config.path("mounts.json"))).mounts[0]
    mount.authorize(work)
    with pytest.raises(Forbidden):
        mount.authorize(personal)
    with pytest.raises(Forbidden):
        mount.authorize(work, publish=True)
    assert mount.user_grants == {} and not mount.publish
    for reader, target in (
        (personal, state["resources"]["work"]),
        (work, state["resources"]["personal"]),
    ):
        with pytest.raises(Forbidden):
            store.get_scope(reader, target["world_id"], target["scope_id"])
    work_profile = read_json(config.path("pi-work.json"))
    assert work_profile["readMountIds"] == ["corporate"] and "writeScope" not in work_profile
    corporate_profile = read_json(config.path("pi-corporate.json"))
    assert corporate_profile["url"] == config.corporate.url
    assert corporate_profile["readScopes"] == corporate_profile["readMountIds"] == []
    assert "writeScope" not in corporate_profile


def test_hybrid_can_select_no_shared_worlds(tmp_path):
    config = config_for(tmp_path, hybrid=True)
    config.corporate.world_ids = []
    bind_corporate(config)
    apply_setup(config)
    assert read_json(config.path("mounts.json"))["mounts"] == []
    assert read_json(config.path("pi-work.json"))["readMountIds"] == []
    assert config.path("pi-corporate.json").exists()


def test_hybrid_requires_matching_validated_binding_without_any_network(tmp_path, monkeypatch):
    config = config_for(tmp_path, hybrid=True)
    with pytest.raises(Conflict, match="Connect"):
        apply_setup(config)
    bind_corporate(config, worlds=["other"])
    with pytest.raises(Conflict, match="reconnect"):
        apply_setup(config)
    assert not config.data_dir.exists()


def test_unowned_existing_authority_requires_authenticated_adoption(tmp_path):
    config = config_for(tmp_path)
    store = MemoryStore(config.data_dir)
    original, administrator = store.bootstrap("original-owner")
    before = snapshot(config.data_dir)
    with pytest.raises(Conflict, match="adoption"):
        apply_setup(config)
    after = snapshot(config.data_dir)
    assert {key: value for key, value in after.items() if key != "daemon.lock"} == before
    wrong = config.state_dir / "wrong.token"
    write_private(wrong, "mw_" + "a" * 32 + "." + "b" * 43 + "\n")
    with pytest.raises(Forbidden, match="administrator"):
        apply_setup(config, adopt_admin_file=wrong)
    after = snapshot(config.data_dir)
    assert {key: value for key, value in after.items() if key != "daemon.lock"} == before
    operator_path = config.state_dir / "existing-operator.token"
    write_private(operator_path, administrator + "\n")
    receipt = apply_setup(config, adopt_admin_file=operator_path)
    assert receipt["authority_id"] == store.authority_id
    assert read_json(config.path("state.json"))["operator_token_file"] == str(operator_path)
    assert not config.path("operator.token").exists()
    assert store.get_world(store.authenticate(administrator), original.id).id == original.id
    assert receipt["resources"]["personal"]["world_id"] != original.id
    assert apply_setup(config)["replayed"] is True


def test_adoption_refuses_hard_linked_control_database_before_sqlite_open(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    store = MemoryStore(config.data_dir)
    _, administrator = store.bootstrap("original-owner")
    admin_file = tmp_path / "admin.token"
    write_private(admin_file, administrator + "\n")
    control = config.data_dir / "control.sqlite3"
    alias = tmp_path / "retained-control.sqlite3"
    os.link(control, alias)
    before = snapshot(config.data_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("linked authority storage must be rejected before SQLite is opened")

    monkeypatch.setattr(provisioning.sqlite3, "connect", forbidden)
    monkeypatch.setattr(MemoryStore, "__init__", forbidden)
    with pytest.raises(Conflict, match="hard link"):
        apply_setup(config, adopt_admin_file=admin_file)
    after = snapshot(config.data_dir)
    assert {key: value for key, value in after.items() if key != "daemon.lock"} == before
    assert alias.read_bytes() == before["control.sqlite3"] == control.read_bytes()
    assert alias.stat().st_ino == control.stat().st_ino
    assert not config.path("provision.json").exists()


@pytest.mark.parametrize("name", ["control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm"])
def test_repeated_apply_refuses_hard_linked_sqlite_files_before_mutation(
    tmp_path, monkeypatch, name
):
    config = config_for(tmp_path)
    apply_setup(config)
    path = config.data_dir / name
    if not path.exists():
        path.write_bytes(b"synthetic sidecar that SQLite must not open")
    alias = tmp_path / "retained-sqlite-file"
    os.link(path, alias)
    before = path.read_bytes()
    state_before = snapshot(config.state_dir)

    def forbidden(*args, **kwargs):
        pytest.fail("linked SQLite storage must be rejected before opening or writing")

    monkeypatch.setattr(provisioning.sqlite3, "connect", forbidden)
    monkeypatch.setattr(MemoryStore, "__init__", forbidden)
    with pytest.raises(Conflict, match="hard link"):
        apply_setup(config)
    assert path.read_bytes() == alias.read_bytes() == before
    assert snapshot(config.state_dir) == state_before


def test_repeated_apply_refuses_hard_linked_managed_token_without_rotating(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    path = config.path("personal.token")
    alias = tmp_path / "retained-personal.token"
    os.link(path, alias)
    original = path.read_bytes()
    with pytest.raises(MemoryError, match="hard link"):
        apply_setup(config)
    assert path.read_bytes() == alias.read_bytes() == original


def test_adoption_and_provider_keys_accept_external_hard_linked_references(tmp_path):
    config = config_for(tmp_path)
    store = MemoryStore(config.data_dir)
    _, administrator = store.bootstrap("original-owner")
    admin_file, admin_alias = tmp_path / "admin.token", tmp_path / "admin-alias.token"
    key_file, key_alias = tmp_path / "provider.key", tmp_path / "provider-alias.key"
    write_private(admin_file, administrator + "\n")
    write_private(key_file, "external-provider-key\n")
    os.link(admin_file, admin_alias)
    os.link(key_file, key_alias)
    config.extraction.api_key_file = key_alias
    config.embedding.api_key_file = key_alias
    receipt = apply_setup(config, adopt_admin_file=admin_alias)
    assert receipt["authority_id"] == store.authority_id
    assert apply_setup(config)["replayed"] is True
    assert admin_alias.read_bytes() == admin_file.read_bytes() == (administrator + "\n").encode()
    assert key_file.read_bytes() == key_alias.read_bytes() == b"external-provider-key\n"


def test_durable_secrets_precede_bootstrap_commit_and_retry_after_rollback(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    original = MemoryStore._bootstrap

    def interrupt(store, db, *args, **kwargs):
        original(store, db, *args, **kwargs)
        assert store.authenticate(token(config, "operator")).admin
        assert token(config, "operator") == kwargs["prepared_token"]
        assert config.path("personal.token").is_file()
        raise RuntimeError("simulated interruption before transaction commit")

    monkeypatch.setattr(MemoryStore, "_bootstrap", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        apply_setup(config)
    journal = read_json(config.path("provision.json"))
    original_secrets = {
        name: config.path(name).read_bytes()
        for name in ("operator.token", "personal.token", "graph.token")
    }
    assert provisioning._control_metadata(config.data_dir).get("bootstrapped") is None
    monkeypatch.setattr(MemoryStore, "_bootstrap", original)
    receipt = apply_setup(config)
    assert receipt["resources"]["personal"] == {
        key: value for key, value in journal["resources"]["personal"].items() if key != "token_file"
    }
    assert all(config.path(name).read_bytes() == value for name, value in original_secrets.items())


def test_interrupt_after_database_commit_resumes_without_new_credentials(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    original = provisioning._generated
    monkeypatch.setattr(
        provisioning,
        "_generated",
        lambda *_: (_ for _ in ()).throw(RuntimeError("simulated output interruption")),
    )
    with pytest.raises(RuntimeError, match="simulated output"):
        apply_setup(config)
    journal = read_json(config.path("provision.json"))
    operator, personal = token(config, "operator"), token(config, "personal")
    store = MemoryStore(config.data_dir)
    assert (
        store.authenticate(personal).credential_id
        == journal["resources"]["personal"]["credential_id"]
    )
    monkeypatch.setattr(provisioning, "_generated", original)
    result = apply_setup(config)
    assert result["replayed"] is True
    assert token(config, "operator") == operator and token(config, "personal") == personal


def test_interrupt_after_output_publication_recovers_matching_file_intent(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    original = provisioning.write_private
    interrupted = False

    def fail_after_write(path, data, **kwargs):
        nonlocal interrupted
        result = original(path, data, **kwargs)
        if path == config.path("pi-personal.json") and not interrupted:
            interrupted = True
            raise RuntimeError("simulated lost file acknowledgment")
        return result

    monkeypatch.setattr(provisioning, "write_private", fail_after_write)
    with pytest.raises(RuntimeError, match="lost file"):
        apply_setup(config)
    published = config.path("pi-personal.json").read_bytes()
    assert apply_setup(config)["replayed"] is True
    assert config.path("pi-personal.json").read_bytes() == published


def test_repeat_refuses_revoked_or_broadened_agent_grants(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    store = MemoryStore(config.data_dir)
    administrator = store.authenticate(token(config, "operator"))
    credential = store.authenticate(token(config, "personal"))
    store.revoke_token(administrator, credential.credential_id)
    before = token(config, "personal")
    with pytest.raises(Unauthorized):
        apply_setup(config)
    assert token(config, "personal") == before


def test_repeat_refuses_added_private_scope_readers(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    store = MemoryStore(config.data_dir)
    resource = read_json(config.path("state.json"))["resources"]["personal"]
    agent = store.authenticate(token(config, "personal"))
    operator = store.authenticate(token(config, "operator"))
    other = store.issue_token(
        operator,
        CreateToken(user_id="alice", agent_id="other", grants={resource["world_id"]: Role.READER}),
    )
    store.set_scope_grant(
        agent,
        resource["world_id"],
        resource["scope_id"],
        UpdateScopeGrant(
            credential_id=store.authenticate(other).credential_id, operations=["read"]
        ),
    )
    with pytest.raises(Conflict, match="audience changed"):
        apply_setup(config)


@pytest.mark.parametrize("name", ["personal.token", "graph.token", "pi-personal.json"])
def test_repeat_preserves_and_rejects_modified_files(tmp_path, name):
    config = config_for(tmp_path)
    apply_setup(config)
    path = config.path(name)
    path.write_text("unrelated private change\n")
    with pytest.raises(Conflict, match="changed"):
        apply_setup(config)
    assert path.read_text() == "unrelated private change\n"


def test_repeat_never_reinitializes_a_missing_authority(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    (config.data_dir / "control.sqlite3").unlink()
    with pytest.raises(Conflict, match="refusing reinitialization"):
        apply_setup(config)
    assert not (config.data_dir / "control.sqlite3").exists()


def test_conflicting_config_and_unowned_profile_are_not_overwritten(tmp_path):
    config = config_for(tmp_path)
    write_private(config.path("pi-personal.json"), "unrelated profile")
    with pytest.raises(Conflict, match="belongs to another"):
        apply_setup(config)
    assert config.path("pi-personal.json").read_text() == "unrelated profile"
    changed = config.model_copy(update={"owner": "bob"})
    with pytest.raises(Conflict, match="changed"):
        apply_setup(changed)


def test_setup_and_live_daemon_locks_prevent_competing_provisioning(tmp_path):
    config = config_for(tmp_path)
    with setup_lock(config), pytest.raises(Conflict, match="another setup"):
        apply_setup(config)
    config.data_dir.mkdir(mode=0o700)
    with DaemonLock(config.data_dir), pytest.raises(Conflict):
        apply_setup(config)
    assert not (config.data_dir / "control.sqlite3").exists()


def test_unsafe_secret_reference_never_bootstraps_authority(tmp_path):
    config = config_for(tmp_path)
    unsafe = tmp_path / "unsafe-secret"
    unsafe.write_text("secret")
    unsafe.chmod(0o644)
    config.extraction.api_key_file = unsafe
    with pytest.raises(MemoryError, match="0600"):
        apply_setup(config)
    assert not config.data_dir.exists()


def test_long_instance_has_bounded_stable_compose_identity(tmp_path):
    config = config_for(tmp_path).model_copy(update={"instance": "a" * 40})
    apply_setup(config)
    first = read_json(config.path("state.json"))["compose_project"]
    assert len(first) <= 63
    apply_setup(config)
    assert read_json(config.path("state.json"))["compose_project"] == first


def test_reviewed_configuration_apply_preserves_authority_secrets_and_grants(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    old_state = read_json(config.path("state.json"))
    old_tokens = {kind: token(config, kind) for kind in ("operator", "personal", "graph")}
    changed = config.model_copy(update={"port": 18887, "graph_port": 17379})
    changed.extraction = changed.extraction.model_copy(update={"model": "other-local-model"})
    assert apply_setup(changed)["replayed"] is True
    state = read_json(changed.path("state.json"))
    assert state["authority_id"] == old_state["authority_id"]
    assert state["resources"] == old_state["resources"]
    assert state["config_digest"] == changed.digest != old_state["config_digest"]
    assert read_json(changed.path("pi-personal.json"))["url"] == changed.url
    assert all(token(changed, kind) == value for kind, value in old_tokens.items())
    assert apply_setup(changed)["replayed"] is True


def test_interrupted_configuration_cas_recovers_existing_resources(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    apply_setup(config)
    original_state = read_json(config.path("state.json"))
    changed = config.model_copy(update={"port": 18887})
    original = provisioning._generated
    monkeypatch.setattr(
        provisioning,
        "_generated",
        lambda *_: (_ for _ in ()).throw(RuntimeError("after config CAS")),
    )
    with pytest.raises(RuntimeError, match="after config CAS"):
        apply_setup(changed)
    assert read_json(config.path("state.json")) == original_state
    with pytest.raises(Conflict, match="saved configuration"):
        apply_setup(config.model_copy(update={"port": 18888}))
    monkeypatch.setattr(provisioning, "_generated", original)
    assert apply_setup(changed)["replayed"] is True
    assert read_json(config.path("state.json"))["resources"] == original_state["resources"]


def test_changed_config_does_not_repair_drift_or_replace_user_files(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    before = read_json(config.path("state.json"))
    path = config.path("pi-personal.json")
    path.write_text("user-modified profile")
    with pytest.raises(Conflict, match="changed"):
        apply_setup(config.model_copy(update={"port": 18887}))
    assert path.read_text() == "user-modified profile"
    assert read_json(config.path("state.json")) == before


def test_hybrid_allowlist_change_requires_new_validated_connection(tmp_path):
    config = config_for(tmp_path, hybrid=True)
    bind_corporate(config)
    apply_setup(config)
    original_resources = read_json(config.path("state.json"))["resources"]
    changed = config.model_copy(
        update={"corporate": config.corporate.model_copy(update={"world_ids": ["team"]})}
    )
    with pytest.raises(Conflict, match="reconnect"):
        apply_setup(changed)
    path = config.path("corporate.json")
    bound = read_json(path)
    bound["world_ids"] = ["team"]
    write_private(
        path, json.dumps(bound), expected_hash=hashlib.sha256(private_read(path)).hexdigest()
    )
    apply_setup(changed)
    assert read_json(config.path("mounts.json"))["mounts"][0]["world_ids"] == ["team"]
    assert read_json(config.path("state.json"))["resources"] == original_resources


def test_client_profile_options_are_explicit_and_retained_outputs_reported(tmp_path):
    config = config_for(tmp_path).model_copy(update={"pi_profiles": False, "mcp_profiles": False})
    assert apply_setup(config)["profiles"] == []
    assert not config.path("pi-personal.json").exists()
    assert not config.path("mcp-personal.json").exists()
    enabled = config.model_copy(update={"pi_profiles": True})
    assert apply_setup(enabled)["profiles"] == [str(config.path("pi-personal.json"))]
    disabled = apply_setup(config)
    assert disabled["profiles"] == []
    assert disabled["retained_outputs"] == [str(config.path("pi-personal.json"))]


def test_modified_state_and_missing_committed_secret_are_never_adopted(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    path = config.path("state.json")
    state = read_json(path)
    original = private_read(path)
    state["files"] = {}
    path.write_text(json.dumps(state))
    with pytest.raises(Conflict, match="state file changed"):
        apply_setup(config)
    path.write_bytes(original)
    config.path("personal.token").unlink()
    with pytest.raises(Conflict, match="secret is missing"):
        apply_setup(config)
    assert not config.path("personal.token").exists()


def test_actual_other_process_cannot_apply_while_setup_lock_is_held(tmp_path):
    config = config_for(tmp_path)
    program = """
import sys
from memory_worlds.errors import Conflict
from memory_worlds.setup_config import SetupConfig
from memory_worlds.setup_provision import apply_setup
config = SetupConfig.model_validate_json(sys.stdin.read())
try:
    result = apply_setup(config)
except Conflict:
    print('blocked')
else:
    print(result['authority_id'])
"""
    with setup_lock(config):
        competing = subprocess.run(
            [sys.executable, "-c", program],
            input=config.model_dump_json(),
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    assert competing.stdout.strip() == "blocked"
    assert not config.data_dir.exists()
    result = apply_setup(config)
    repeated = subprocess.run(
        [sys.executable, "-c", program],
        input=config.model_dump_json(),
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert repeated.stdout.strip() == result["authority_id"]
    store = MemoryStore(config.data_dir)
    with store._control() as db:
        credentials = store._table(db, "credentials")
        assert len(db.execute(select(credentials)).all()) == 2


def test_lost_state_publication_acknowledgment_recovers_exact_ids(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    original = provisioning.write_private
    interrupted = False

    def fail_after_state(path, data, **kwargs):
        nonlocal interrupted
        result = original(path, data, **kwargs)
        if path == config.path("state.json") and not interrupted:
            interrupted = True
            raise RuntimeError("lost state acknowledgment")
        return result

    monkeypatch.setattr(provisioning, "write_private", fail_after_state)
    with pytest.raises(RuntimeError, match="lost state"):
        apply_setup(config)
    published = private_read(config.path("state.json"))
    assert apply_setup(config)["replayed"] is True
    assert private_read(config.path("state.json")) == published


def test_malformed_private_journal_fails_with_content_free_error(tmp_path):
    config = config_for(tmp_path)
    apply_setup(config)
    path = config.path("provision.json")
    journal = read_json(path)
    journal["resources"] = "PRIVATE-JOURNAL-CANARY"
    path.write_text(json.dumps(journal))
    with pytest.raises(Conflict, match="current setup contract") as error:
        apply_setup(config)
    assert "PRIVATE-JOURNAL-CANARY" not in str(error.value)


def test_normal_agent_owned_scope_creation_does_not_block_repeat_apply(tmp_path):
    config = config_for(tmp_path)
    first = apply_setup(config)
    store = MemoryStore(config.data_dir)
    agent = store.authenticate(token(config, "personal"))
    world_id = first["resources"]["personal"]["world_id"]
    for kind in ("agent-persistent", "invocation-private", "run-shared"):
        store.create_scope(
            agent, world_id, CreateScope(kind=kind, name="Ordinary agent working scope")
        )
    assert apply_setup(config)["replayed"] is True


def test_extra_scope_grant_from_another_credential_remains_detected(tmp_path):
    config = config_for(tmp_path)
    first = apply_setup(config)
    store = MemoryStore(config.data_dir)
    agent = store.authenticate(token(config, "personal"))
    admin = store.authenticate(token(config, "operator"))
    world_id = first["resources"]["personal"]["world_id"]
    other = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(
                user_id="alice",
                agent_id="other-agent",
                grants={world_id: Role.WRITER},
            ),
        )
    )
    private = store.create_scope(
        other, world_id, CreateScope(kind="invocation-private", name="Other agent scratch")
    )
    store.set_scope_grant(
        other,
        world_id,
        private.id,
        UpdateScopeGrant(credential_id=agent.credential_id, operations=["read"]),
    )
    with pytest.raises(Conflict, match="external private scope"):
        apply_setup(config)


def test_actual_exit_after_first_daemon_lock_recovers_prejournaled_authority(tmp_path):
    config = config_for(tmp_path)
    program = """
import os
import sys
import memory_worlds.setup_provision as provisioning
from memory_worlds.setup_config import SetupConfig
config = SetupConfig.model_validate_json(sys.stdin.read())
def exit_after_lock(*args):
    os._exit(73)
provisioning._apply_stopped = exit_after_lock
provisioning.apply_setup(config)
"""
    interrupted = subprocess.run(
        [sys.executable, "-c", program],
        input=config.model_dump_json(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert interrupted.returncode == 73
    journal = read_json(config.path("provision.json"))
    assert journal["stage"] == "prepared" and journal["secrets"] == {}
    assert {entry.name for entry in config.data_dir.iterdir()} == {"daemon.lock"}
    result = apply_setup(config)
    assert result["authority_id"] == journal["authority_id"]
    assert result["resources"]["personal"] == {
        key: value for key, value in journal["resources"]["personal"].items() if key != "token_file"
    }
    assert apply_setup(config)["replayed"] is True


def test_failure_before_journal_publication_creates_no_authority_directory(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    original = provisioning._journal

    def unavailable_journal(*args):
        raise RuntimeError("journal publication unavailable")

    monkeypatch.setattr(provisioning, "_journal", unavailable_journal)
    with pytest.raises(RuntimeError, match="journal publication"):
        apply_setup(config)
    assert not config.data_dir.exists()
    assert not config.path("provision.json").exists()
    monkeypatch.setattr(provisioning, "_journal", original)
    assert apply_setup(config)["replayed"] is False


def test_unowned_foreign_lock_only_directory_never_becomes_a_setup(tmp_path):
    config = config_for(tmp_path)
    config.data_dir.mkdir(mode=0o700)
    foreign_lock = config.data_dir / "daemon.lock"
    foreign_lock.write_text("unrelated owner marker")
    foreign_lock.chmod(0o600)
    with pytest.raises(Conflict, match="adoption"):
        apply_setup(config)
    assert not config.path("provision.json").exists()
    assert foreign_lock.read_text() == "unrelated owner marker"
    invalid_admin = tmp_path / "invalid-admin.token"
    write_private(invalid_admin, "mw_" + "a" * 32 + "." + "b" * 43 + "\n")
    with pytest.raises(Conflict, match="already initialized"):
        apply_setup(config, adopt_admin_file=invalid_admin)
    assert foreign_lock.read_text() == "unrelated owner marker"
    assert not (config.data_dir / "control.sqlite3").exists()
