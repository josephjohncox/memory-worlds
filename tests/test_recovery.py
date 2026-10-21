from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from memory_worlds.errors import Conflict, Forbidden, Unauthorized
from memory_worlds.identity_names import CreateIdentity, IdentityRegistry
from memory_worlds.models import CreateToken, DelegateCredential, EpisodeInput, Role
from memory_worlds.recovery import backup_authority, restore_authority
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore


def note(source, **kwargs):
    return EpisodeInput(
        source_id=source,
        content=f"private evidence {source}",
        occurred_at=kwargs.pop("occurred_at", datetime.now(UTC)),
        **kwargs,
    )


def test_whole_authority_recovers_private_scopes_credentials_names_and_sequences(tmp_path):
    store = MemoryStore(tmp_path / "source")
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    identities = IdentityRegistry(store)
    agent = identities.create(owner, "agent", CreateIdentity(name="Researcher", alias="research"))
    child = store.delegate_credential(
        owner, DelegateCredential(private_world_id=world.id, agent_id=agent.id)
    )
    private = child["private_scope"]
    child_user = store.authenticate(child["token"])
    secret = store.capture(child_user, world.id, note("secret", scope_id=private.id))
    shared = store.capture(owner, world.id, note("common"))
    disabled = store.capture(owner, world.id, note("source-only", project=False))
    revoked = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="revoked", grants={world.id: Role.READER})
    )
    store.revoke_token(owner, store.authenticate(revoked).credential_id)
    key, archive = tmp_path / "backup.key", tmp_path / "authority.mwauthority"
    generate_key(key)
    manifest = backup_authority(store, owner, archive, key)
    assert manifest.graph_mode == "rebuild"
    recovered = tmp_path / "recovered"
    receipt = restore_authority(archive, key, recovered)
    restored = MemoryStore(recovered)
    assert receipt["authority_id"] == store.authority_id == restored.authority_id
    assert restored.authenticate(token) == owner
    with pytest.raises(Unauthorized):
        restored.authenticate(revoked)
    assert IdentityRegistry(restored).resolve(owner, "agent", "@research") == agent
    assert (
        restored.get_episode(child_user, world.id, secret.episode.id, scope_id=private.id).content
        == secret.episode.content
    )
    with pytest.raises(Forbidden):
        restored.get_episode(owner, world.id, secret.episode.id, scope_id=private.id)
    assert restored.capture(
        owner, world.id, note("common", occurred_at=shared.episode.occurred_at)
    ).replayed
    assert restored.get_episode(owner, world.id, disabled.episode.id).projection_state == "disabled"
    added = restored.capture(owner, world.id, note("next"))
    assert added.episode.sequence > disabled.episode.sequence
    assert len(restored.pending_jobs(world.id)) == 2
    with pytest.raises(Conflict):
        restore_authority(archive, key, recovered)


def test_backup_requires_live_admin_and_does_not_include_runtime_files(tmp_path):
    store = MemoryStore(tmp_path / "source")
    world, token = store.bootstrap()
    owner = store.authenticate(token)
    user_token = store.issue_token(
        owner, CreateToken(user_id=owner.user_id, agent_id="reader", grants={world.id: Role.READER})
    )
    key, archive = tmp_path / "key", tmp_path / "backup.mwauthority"
    generate_key(key)
    with pytest.raises(Forbidden):
        backup_authority(store, store.authenticate(user_token), archive, key)
    (store.data_dir / "provider-secret").write_text("not part of authority recovery")
    manifest = backup_authority(store, owner, archive, key)
    assert all(member.name.startswith("tables/") for member in manifest.members)
    assert not archive.stat().st_mode & 0o077
    store.revoke_token(owner, owner.credential_id)
    with pytest.raises(Unauthorized):
        backup_authority(store, owner, tmp_path / "revoked.mwauthority", key)


def test_recovery_preserves_explicit_rebuild_extraction_intent(tmp_path):
    store = MemoryStore(tmp_path / "source")
    world, token = store.bootstrap()
    owner = store.authenticate(token)
    source = store.capture(owner, world.id, note("later-selected", project=False))
    lease = store.persistence.acquire_lease(store.projection_resource(world.id, world.scope_id))
    store.register_generation(world.id, "generation", scope_id=world.scope_id, lease=lease)
    store.activate_generation(
        world.id, "generation", [source.episode.id], scope_id=world.scope_id, lease=lease
    )
    key, archive = tmp_path / "key", tmp_path / "backup.mwauthority"
    generate_key(key)
    backup_authority(store, owner, archive, key)
    restore_authority(archive, key, tmp_path / "restored")
    restored = MemoryStore(tmp_path / "restored")
    assert restored.get_episode(owner, world.id, source.episode.id).projection_state == "pending"
    assert [item.id for item in restored.pending_jobs(world.id)] == [source.episode.id]
    with restored._control() as db:
        leases = restored._table(db, "worker_leases")
        assert db.execute(select(leases.c.owner)).scalar_one() is None


def test_failed_restore_does_not_install_partial_sqlite_authority(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "source")
    _, token = store.bootstrap()
    key, archive = tmp_path / "key", tmp_path / "backup.mwauthority"
    generate_key(key)
    backup_authority(store, store.authenticate(token), archive, key)
    import memory_worlds.recovery as recovery

    def fail(*_args):
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(recovery, "_restore_tables", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        restore_authority(archive, key, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob(".authority-restore-*"))
