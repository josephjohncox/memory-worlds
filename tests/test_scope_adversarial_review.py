"""Independent attempts to cross invocation and delegation boundaries."""

from datetime import UTC, datetime

import pytest

from memory_worlds.client import Client
from memory_worlds.errors import Forbidden, NotFound
from memory_worlds.models import (
    BindContext,
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    EpisodeInput,
    ScopedPublication,
    ScopeFlow,
    ScopeGrant,
    ScopeSelection,
    SearchRequest,
    UpdateScopeGrant,
)
from memory_worlds.mounts import Mount, MountConfig
from memory_worlds.outbox import RoutedPublication
from memory_worlds.portability import PortabilityError, export_world, restore_world
from memory_worlds.router import WorldRouter
from memory_worlds.store import MemoryStore

AT = datetime(2026, 10, 20, tzinfo=UTC)


@pytest.mark.parametrize("linked", [False, True])
def test_scoped_publication_requires_an_explicit_destination(linked):
    source = ScopeSelection(world_id="private", scope_id="00000000-0000-4000-8000-000000000001")
    request = ScopedPublication(
        source_id="result",
        content="Selected finding",
        occurred_at=AT,
        source_scope=source if linked else None,
        source_episode_id="episode" if linked else None,
    )
    with Client("https://memory.invalid", "synthetic-token") as client:
        with pytest.raises(ValueError, match="explicit destination scope"):
            client.publish("private", request)


@pytest.fixture
def authority(tmp_path):
    store = MemoryStore(tmp_path / "authority")
    world, token = store.bootstrap("alice")
    return store, world, store.authenticate(token)


def child(store, principal, world, **kwargs):
    issued = store.delegate_credential(
        principal, DelegateCredential(private_world_id=world.id, **kwargs)
    )
    return store.authenticate(issued["token"]), issued["private_scope"], issued


def note(scope_id=None, **kwargs):
    return EpisodeInput(
        scope_id=scope_id,
        source_id="note-one",
        content="private hypothesis",
        occurred_at=AT,
        **kwargs,
    )


def test_restricted_manage_cannot_create_independent_permanent_grant(authority):
    store, world, owner = authority
    private = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Private")
    )
    recipient = store.authenticate(
        store.issue_token(
            owner,
            CreateToken(
                user_id="alice",
                agent_id="recipient",
                grants={world.id: "writer"},
            ),
        )
    )
    issued = store.delegate_credential(
        owner,
        DelegateCredential(
            grants=[
                ScopeGrant(
                    world_id=world.id,
                    scope_id=private.id,
                    operations=["read", "manage"],
                )
            ],
            allow_delegation=False,
        ),
    )
    manager = store.authenticate(issued["token"])
    with pytest.raises(Forbidden):
        store.get_scope(recipient, world.id, private.id)
    with pytest.raises(Forbidden):
        store.set_scope_grant(
            manager,
            world.id,
            private.id,
            UpdateScopeGrant(
                credential_id=recipient.credential_id,
                operations=["read"],
            ),
        )
    store.revoke_token(owner, manager.credential_id)
    with pytest.raises(Forbidden):
        store.get_scope(recipient, world.id, private.id)


def test_scope_only_child_cannot_discover_parent_federation_receipts(authority, tmp_path):
    store, world, owner = authority
    worker, _, _ = child(store, owner, world)
    mount = Mount(
        id="work",
        name="Work",
        authority_id="remote-authority",
        world_ids=["company"],
        url="https://remote.example.test",
        token_file=tmp_path / "unused-token",
        publish=True,
    )
    router = WorldRouter(store, MountConfig(mounts=[mount]))
    queued = router.publish(
        owner,
        RoutedPublication(
            destination_mount_id="work",
            world_id="company",
            source_id="confidential-project-name",
            content="Selected publication",
            occurred_at=AT,
        ),
    )
    assert router.deliveries(owner)[0].id == queued.id
    assert router.list_mounts(worker) == []
    with pytest.raises(Forbidden):
        router.deliveries(worker)
    with pytest.raises(Forbidden):
        router.retry(worker, queued.id)


def test_context_rebinding_cannot_convert_read_or_private_grant_into_shared_write(authority):
    store, world, owner = authority
    shared = store.list_scopes(owner, world.id)[0]
    worker, private, _ = child(
        store,
        owner,
        world,
        grants=[
            ScopeGrant(
                world_id=world.id,
                scope_id=shared.id,
                operations=["read"],
            )
        ],
    )
    with pytest.raises(Forbidden):
        store.bind_context(
            worker,
            BindContext(
                read_scopes=[ScopeSelection(world_id=world.id, scope_id=private.id)],
                write_scope=ScopeSelection(world_id=world.id, scope_id=shared.id),
            ),
        )
    with pytest.raises(Forbidden):
        store.capture(worker, world.id, note(scope_id=shared.id))
    with pytest.raises(Forbidden):
        store.publish_scope(
            worker,
            world.id,
            shared.id,
            ScopedPublication(
                source_id="leak",
                content="Private leaked text",
                occurred_at=AT,
            ),
        )


def test_coordinator_inspection_is_explicit_read_only_and_cannot_be_redelegated(authority):
    store, world, owner = authority
    parent, parent_scope, _ = child(store, owner, world, allow_delegation=True)
    worker, private, _ = child(
        store,
        parent,
        world,
        coordinator_read=True,
        grants=[
            ScopeGrant(
                world_id=world.id,
                scope_id=parent_scope.id,
                operations=["read"],
            )
        ],
    )
    stored = store.capture(worker, world.id, note(scope_id=private.id))
    assert store.get_episode(parent, world.id, stored.episode.id, scope_id=private.id).content
    with pytest.raises(Forbidden):
        store.capture(parent, world.id, note(scope_id=private.id))
    with pytest.raises(Forbidden):
        store.delegate_credential(
            parent,
            DelegateCredential(
                grants=[
                    ScopeGrant(
                        world_id=world.id,
                        scope_id=private.id,
                        operations=["read"],
                    )
                ]
            ),
        )


def test_source_episode_uuid_cannot_cross_scope_even_when_caller_reads_both(authority):
    store, world, owner = authority
    left = store.create_scope(owner, world.id, CreateScope(kind="invocation-private", name="Left"))
    right = store.create_scope(
        owner, world.id, CreateScope(kind="invocation-private", name="Right")
    )
    source = store.capture(owner, world.id, note(scope_id=left.id))
    shared = store.list_scopes(owner, world.id)[0]
    with pytest.raises(NotFound):
        store.get_episode(owner, world.id, source.episode.id, scope_id=right.id)
    with pytest.raises(NotFound):
        store.publish_scope(
            owner,
            world.id,
            shared.id,
            ScopedPublication(
                source_id="wrong-reference",
                content="Result",
                occurred_at=AT,
                source_scope=ScopeSelection(world_id=world.id, scope_id=right.id),
                source_episode_id=source.episode.id,
            ),
        )
    assert not store.list_episodes(owner, world.id)


def test_metadata_cannot_grant_sibling_scope_or_change_captured_audience(authority):
    store, world, owner = authority
    worker, private, _ = child(store, owner, world)
    _, sibling, _ = child(store, owner, world)
    receipt = store.capture(
        worker,
        world.id,
        note(
            scope_id=private.id,
            metadata={
                "scope_id": str(sibling.id),
                "admin": True,
                "grants": [{"scope_id": str(sibling.id), "operations": ["read", "write"]}],
            },
        ),
    )
    assert receipt.episode.scope_id == private.id
    with pytest.raises(Forbidden):
        store.get_scope(worker, world.id, sibling.id)
    with pytest.raises(Forbidden):
        store.search(
            worker,
            [],
            "private",
            scopes=[
                ScopeSelection(
                    world_id=world.id,
                    scope_id=sibling.id,
                )
            ],
        )


def test_private_archive_restore_requires_private_destination_and_preserves_scope(
    authority, tmp_path
):
    store, world, owner = authority
    private = store.create_scope(
        owner, world.id, CreateScope(kind="invocation-private", name="Private")
    )
    store.capture(owner, world.id, note(scope_id=private.id))
    archive = export_world(store, owner, world.id, tmp_path / "private.mworld", scope_id=private.id)
    with pytest.raises(PortabilityError):
        restore_world(store, owner, world.id, archive)
    run = store.create_scope(owner, world.id, CreateScope(kind="run-shared", name="Run"))
    with pytest.raises(PortabilityError):
        restore_world(store, owner, world.id, archive, scope_id=run.id)
    other = store.create_scope(
        owner, world.id, CreateScope(kind="invocation-private", name="Other")
    )
    assert restore_world(store, owner, world.id, archive, scope_id=other.id)["imported"] == 1
    assert store.list_episodes(owner, world.id, scope_id=other.id)[0].scope_id == other.id
    assert not store.list_episodes(owner, world.id)


def test_granting_metadata_scope_does_not_override_query_selection(authority):
    # Validation rejects ambiguous selectors before any store/backend is queried.
    from pydantic import ValidationError

    _, world, _ = authority
    from uuid import uuid4

    with pytest.raises(ValidationError):
        SearchRequest(
            query="private",
            scope_id=uuid4(),
            scopes=[
                ScopeSelection(
                    world_id=world.id,
                    scope_id=uuid4(),
                )
            ],
        )


def test_explicit_world_shared_uuid_restore_uses_same_journal(authority, tmp_path):
    store, world, owner = authority
    original = store.capture(owner, world.id, note())
    archive = export_world(store, owner, world.id, tmp_path / "shared.mworld")
    target = store.create_world(owner, CreateWorld(name="Restored"))
    target_scope = store.list_scopes(owner, target.id)[0]
    result = restore_world(store, owner, target.id, archive, scope_id=target_scope.id)
    assert result["imported"] == 1
    explicit = store.list_episodes(owner, target.id, scope_id=target_scope.id)
    implicit = store.list_episodes(owner, target.id)
    assert explicit[0].scope_id == target_scope.id
    assert explicit == implicit
    assert implicit[0].scope_id == target_scope.id
    assert explicit[0].content == original.episode.content
    assert (
        restore_world(store, owner, target.id, archive, scope_id=target_scope.id)["replayed"] == 1
    )


def flow(store, principal, world, source, target):
    return store.scope_flow(
        principal,
        ScopeFlow(
            read_scopes=[ScopeSelection(world_id=world.id, scope_id=source.id)],
            write_scope=ScopeSelection(world_id=world.id, scope_id=target.id),
        ),
    )


def test_run_shared_retrieval_can_flow_into_delegated_invocation_private_notes(authority):
    store, world, owner = authority
    run = store.create_scope(owner, world.id, CreateScope(kind="run-shared", name="Task"))
    worker, private, _ = child(
        store,
        owner,
        world,
        coordinator_read=True,
        grants=[
            ScopeGrant(
                world_id=world.id,
                scope_id=run.id,
                operations=["read"],
            )
        ],
    )
    assert flow(store, worker, world, run, private) is True


def test_nested_coordinator_anchors_do_not_make_private_flow_transitive(authority):
    store, world, owner = authority
    parent, parent_private, _ = child(
        store, owner, world, coordinator_read=True, allow_delegation=True
    )
    _, grand_private, _ = child(
        store,
        parent,
        world,
        coordinator_read=True,
        grants=[
            ScopeGrant(
                world_id=world.id,
                scope_id=parent_private.id,
                operations=["read"],
            )
        ],
    )
    assert store.get_scope(parent, world.id, grand_private.id).id == grand_private.id
    with pytest.raises(Forbidden):
        store.get_scope(owner, world.id, grand_private.id)
    # The root coordinator reads parent_private but cannot read grand_private.
    assert flow(store, parent, world, grand_private, parent_private) is False


def test_flow_uses_actual_readers_and_live_acl_changes_instead_of_scope_kinds(authority):
    store, world, owner = authority
    source = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Source")
    )
    target = store.create_scope(
        owner, world.id, CreateScope(kind="invocation-private", name="Target")
    )
    peer = store.authenticate(
        store.issue_token(
            owner,
            CreateToken(
                user_id="alice",
                agent_id="peer",
                grants={world.id: "writer"},
            ),
        )
    )
    assert flow(store, owner, world, source, target) is True
    store.set_scope_grant(
        owner,
        world.id,
        target.id,
        UpdateScopeGrant(
            credential_id=peer.credential_id,
            operations=["write", "publish"],
        ),
    )
    assert flow(store, owner, world, source, target) is True
    store.set_scope_grant(
        owner,
        world.id,
        target.id,
        UpdateScopeGrant(
            credential_id=peer.credential_id,
            operations=["read"],
        ),
    )
    assert flow(store, owner, world, source, target) is False
    store.set_scope_grant(
        owner,
        world.id,
        source.id,
        UpdateScopeGrant(
            credential_id=peer.credential_id,
            operations=["read"],
        ),
    )
    assert flow(store, owner, world, source, target) is True
    store.set_scope_grant(
        owner,
        world.id,
        source.id,
        UpdateScopeGrant(
            credential_id=peer.credential_id,
            operations=[],
        ),
    )
    assert flow(store, owner, world, source, target) is False
    store.revoke_token(owner, peer.credential_id)
    assert flow(store, owner, world, source, target) is True


def test_flow_checks_implicit_world_shared_readers_and_denies_unreadable_sources(authority):
    store, world, owner = authority
    source = store.create_scope(
        owner, world.id, CreateScope(kind="invocation-private", name="Source")
    )
    shared = store.list_scopes(owner, world.id)[0]
    peer = store.authenticate(
        store.issue_token(
            owner,
            CreateToken(
                user_id="alice",
                agent_id="peer",
                grants={world.id: "writer"},
            ),
        )
    )
    assert flow(store, owner, world, source, shared) is False
    with pytest.raises(Forbidden):
        flow(store, peer, world, source, shared)
