import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from memory_worlds.errors import Conflict, Forbidden, NotFound, Unauthorized
from memory_worlds.errors import MemoryError as InvalidRequest
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
    ScopeKind,
    ScopeSelection,
    UpdateScopeGrant,
)
from memory_worlds.store import MemoryStore


@pytest.fixture
def local(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap()
    return store, world, store.authenticate(token)


def note(source="note", content="secret evidence", **kwargs):
    return EpisodeInput(
        source_id=source, content=content, occurred_at=datetime(2026, 1, 1, tzinfo=UTC), **kwargs
    )


def delegate(store, principal, world, **kwargs):
    receipt = store.delegate_credential(
        principal, DelegateCredential(private_world_id=world.id, **kwargs)
    )
    return store.authenticate(receipt["token"]), receipt["private_scope"], receipt


def test_subagents_have_distinct_uuids_and_no_parent_or_sibling_visibility(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world)
    sibling, other_private, _ = delegate(store, owner, world)
    assert child.agent_uuid != sibling.agent_uuid
    assert child.invocation_id != sibling.invocation_id
    assert private.id != other_private.id
    receipt = store.capture(child, world.id, note(scope_id=private.id))
    assert (
        store.get_episode(child, world.id, receipt.episode.id, scope_id=private.id).content
        == "secret evidence"
    )
    for caller in (owner, sibling):
        for operation in (
            lambda: store.get_episode(caller, world.id, receipt.episode.id, scope_id=private.id),
            lambda: store.snapshot(caller, world.id, scope_id=private.id),
            lambda: store.get_scope(caller, world.id, private.id),
            lambda: store.search(
                caller,
                [],
                "secret",
                scopes=[ScopeSelection(world_id=world.id, scope_id=private.id)],
            ),
        ):
            with pytest.raises(Forbidden):
                operation()
    assert private.id not in {scope.id for scope in store.list_scopes(owner, world.id)}


def test_default_world_selection_cannot_bypass_child_scope_ceiling(local):
    store, world, owner = local
    store.capture(owner, world.id, note("shared", "world common"))
    child, private, _ = delegate(store, owner, world)
    assert store.list_worlds(child)[0].id == world.id
    for operation in (
        lambda: store.get_world(child, world.id),
        lambda: store.list_episodes(child, world.id),
        lambda: store.snapshot(child, world.id),
        lambda: store.capture(child, world.id, note("leak")),
        lambda: store.search(child, [world.id], "common"),
        lambda: store.create_world(child, CreateWorld(name="escape")),
        lambda: store.issue_token(
            child, CreateToken(user_id=child.user_id, agent_id="escape", admin=True)
        ),
    ):
        with pytest.raises(Forbidden):
            operation()
    assert store.list_scopes(child, world.id)[0].id == private.id


def test_same_source_identity_is_independent_per_scope_and_replay_is_stable(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world)
    sibling, private2, _ = delegate(store, owner, world)
    data = note(scope_id=private.id)
    first = store.capture(child, world.id, data)
    assert store.capture(child, world.id, data).replayed
    other = store.capture(sibling, world.id, note(content="different", scope_id=private2.id))
    assert first.episode.id != other.episode.id
    with pytest.raises(Conflict):
        store.capture(child, world.id, note(content="changed", scope_id=private.id))


def test_read_scope_grant_does_not_grant_write_or_publication(local):
    store, world, owner = local
    shared = store.list_scopes(owner, world.id)[0]
    issued = store.delegate_credential(
        owner,
        DelegateCredential(
            grants=[ScopeGrant(world_id=world.id, scope_id=shared.id, operations=["read"])]
        ),
    )
    child = store.authenticate(issued["token"])
    assert store.list_episodes(child, world.id) == []
    with pytest.raises(Forbidden):
        store.capture(child, world.id, note())
    with pytest.raises(Forbidden):
        store.publish_scope(
            child,
            world.id,
            shared.id,
            ScopedPublication(
                **note().model_dump(
                    exclude={
                        "scope_id",
                        "metadata",
                        "source_type",
                        "session_id",
                        "source_entry_id",
                        "parent_entry_id",
                        "role",
                        "source_ref",
                    }
                )
            ),
        )


def test_explicit_return_publishes_new_copy_without_exposing_scratch(local):
    store, world, owner = local
    shared = store.list_scopes(owner, world.id)[0]
    child, private, _ = delegate(
        store,
        owner,
        world,
        grants=[ScopeGrant(world_id=world.id, scope_id=shared.id, operations=["publish"])],
    )
    private_receipt = store.capture(child, world.id, note(scope_id=private.id))
    published = store.publish_scope(
        child,
        world.id,
        shared.id,
        ScopedPublication(
            source_id="selected-result",
            content="accepted finding",
            occurred_at=datetime.now(UTC),
            source_scope=ScopeSelection(world_id=world.id, scope_id=private.id),
            source_episode_id=private_receipt.episode.id,
        ),
    )
    assert published.episode.id != private_receipt.episode.id
    assert store.list_episodes(owner, world.id)[0].content == "accepted finding"
    assert (
        published.episode.metadata["publication_source"]["digest"] == private_receipt.episode.digest
    )
    with pytest.raises(Forbidden):
        store.get_episode(owner, world.id, private_receipt.episode.id, scope_id=private.id)


def test_unreadable_source_cannot_be_linked_into_publication(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world)
    source = store.capture(child, world.id, note(scope_id=private.id))
    shared = store.list_scopes(owner, world.id)[0]
    with pytest.raises(Forbidden):
        store.publish_scope(
            owner,
            world.id,
            shared.id,
            ScopedPublication(
                source_id="leak",
                content="x",
                occurred_at=datetime.now(UTC),
                source_scope=ScopeSelection(world_id=world.id, scope_id=private.id),
                source_episode_id=source.episode.id,
            ),
        )


def test_run_shared_requires_publication_even_in_personal_world(local):
    store, world, owner = local
    run = store.create_scope(owner, world.id, CreateScope(kind="run-shared", name="Task"))
    with pytest.raises(Forbidden):
        store.capture(owner, world.id, note(scope_id=run.id))
    receipt = store.publish_scope(
        owner,
        world.id,
        run.id,
        ScopedPublication(source_id="result", content="result", occurred_at=datetime.now(UTC)),
    )
    assert receipt.episode.scope_id == run.id


def test_shared_outer_world_accepts_private_invocation_capture(local):
    store, _, owner = local
    team = store.create_world(owner, CreateWorld(name="Team", kind="team"))
    child, private, _ = delegate(store, owner, team)
    assert store.capture(child, team.id, note(scope_id=private.id)).episode.scope_id == private.id
    with pytest.raises(Forbidden):
        store.capture(owner, team.id, note())


def test_contexts_bind_explicit_reads_write_and_parent_invocation(local):
    store, world, owner = local
    shared = store.list_scopes(owner, world.id)[0]
    binding = store.bind_context(
        owner,
        BindContext(
            read_scopes=[ScopeSelection(world_id=world.id, scope_id=shared.id)],
            write_scope=ScopeSelection(world_id=world.id, scope_id=shared.id),
            agent_id=uuid4(),
        ),
    )
    child, _, issued = delegate(store, owner, world, parent_context_id=binding.id)
    assert child.parent_invocation_id == binding.invocation_id
    assert store.get_context(child, issued["context"].id).invocation_id == child.invocation_id
    with pytest.raises(NotFound):
        store.get_context(child, binding.id)
    with pytest.raises(Forbidden):
        store.bind_context(child, BindContext(agent_id=uuid4()))


def test_expiry_parent_revocation_and_descendant_revocation_are_live(local):
    store, world, owner = local
    child, private, issued = delegate(store, owner, world, allow_delegation=True)
    grandchild, grandprivate, grandissued = delegate(
        store,
        child,
        world,
        grants=[ScopeGrant(world_id=world.id, scope_id=private.id, operations=["read"])],
    )
    assert grandchild.parent_invocation_id == child.invocation_id
    assert grandissued["expires_at"] <= issued["expires_at"]
    store.revoke_token(owner, child.credential_id)
    for credential in (child, grandchild):
        with pytest.raises(Unauthorized):
            store.validate_principal(credential)
    assert store.world_path(world.id, grandprivate.id).is_file()


def test_default_child_cannot_redelegate_or_escalate_agent_permissions(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world)
    with pytest.raises(Forbidden):
        delegate(store, child, world)
    shared = store.list_scopes(owner, world.id)[0]
    with pytest.raises(Forbidden):
        store.delegate_credential(
            child,
            DelegateCredential(
                grants=[ScopeGrant(world_id=world.id, scope_id=shared.id, operations=["read"])],
                allow_delegation=True,
            ),
        )
    with pytest.raises(Forbidden):
        store.set_scope_grant(
            owner,
            world.id,
            private.id,
            UpdateScopeGrant(credential_id=owner.credential_id, operations=["read"]),
        )


def test_ancestor_world_permission_removal_blocks_child_immediately(local):
    store, world, owner = local
    token = store.issue_token(
        owner, CreateToken(user_id="owner", agent_id="parent", grants={world.id: "writer"})
    )
    parent = store.authenticate(token)
    child, private, _ = delegate(store, parent, world)
    with store._control() as db:
        table = store._table(db, "grants")
        db.execute(table.delete().where(table.c.credential_id == parent.credential_id))
    with pytest.raises(Forbidden):
        store.capture(child, world.id, note(scope_id=private.id))


def test_expired_child_preserves_notes_without_live_access(local):
    store, world, owner = local
    child, private, issued = delegate(store, owner, world)
    store.capture(child, world.id, note(scope_id=private.id))
    with store._control() as db:
        table = store._table(db, "credential_contexts")
        db.execute(
            table.update()
            .where(table.c.credential_id == child.credential_id)
            .values(
                expires_at=time.time() - 1,
            )
        )
    with pytest.raises(Unauthorized):
        store.authenticate(issued["token"])
    assert store.world_path(world.id, private.id).is_file()


def test_child_cannot_outlive_parent(local):
    store, world, owner = local
    child, private, _ = delegate(
        store,
        owner,
        world,
        allow_delegation=True,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(Forbidden):
        delegate(
            store,
            child,
            world,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            grants=[ScopeGrant(world_id=world.id, scope_id=private.id, operations=["read"])],
        )


def test_scope_acl_can_explicitly_share_and_revoke_agent_persistent_memory(local):
    store, world, owner = local
    persistent = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Specialist", agent_id=uuid4())
    )
    peer_token = store.issue_token(
        owner, CreateToken(user_id="owner", agent_id="peer", grants={world.id: "writer"})
    )
    peer = store.authenticate(peer_token)
    with pytest.raises(Forbidden):
        store.get_scope(peer, world.id, persistent.id)
    store.set_scope_grant(
        owner,
        world.id,
        persistent.id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    assert store.get_scope(peer, world.id, persistent.id).id == persistent.id
    store.set_scope_grant(
        owner,
        world.id,
        persistent.id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=[]),
    )
    with pytest.raises(Forbidden):
        store.get_scope(peer, world.id, persistent.id)


def test_scope_graph_journals_jobs_generations_and_recovery_are_isolated(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world)
    receipt = store.capture(child, world.id, note(scope_id=private.id, project=True))
    assert store.pending_jobs(world.id) == []
    assert store.pending_jobs(world.id, scope_id=private.id)[0].id == receipt.episode.id
    lease = store.persistence.acquire_lease(store.projection_resource(world.id, private.id))
    store.mark_processing(world.id, receipt.episode.id, scope_id=private.id, lease=lease)
    assert store.recover_interrupted_projections() == 0
    store.persistence.release_lease(lease)
    assert store.recover_interrupted_projections() == 1
    assert store.get_world(owner, world.id).projection_state == "disabled"
    assert store.get_world(child, world.id, scope_id=private.id).projection_state == "degraded"
    targets = store.projection_worlds()
    assert {(target.id, target.scope_id) for target in targets} == {
        (world.id, world.scope_id),
        (world.id, private.id),
    }


def test_search_authorizes_whole_scope_selection_before_results(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world)
    store.capture(owner, world.id, note("shared"))
    with pytest.raises(Forbidden):
        store.search(
            owner,
            [world.id],
            "secret",
            scopes=[ScopeSelection(world_id=world.id, scope_id=private.id)],
        )
    store.capture(child, world.id, note(scope_id=private.id))
    assert (
        len(
            store.search(
                child, [], "secret", scopes=[ScopeSelection(world_id=world.id, scope_id=private.id)]
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "kind,field",
    [
        (ScopeKind.RUN_SHARED, "run_id"),
        (ScopeKind.AGENT_PERSISTENT, "agent_id"),
        (ScopeKind.INVOCATION_PRIVATE, "invocation_id"),
    ],
)
def test_scope_identity_fields_are_server_issued_when_omitted(local, kind, field):
    store, world, owner = local
    scope = store.create_scope(owner, world.id, CreateScope(kind=kind, name="server identity"))
    assert isinstance(getattr(scope, field), UUID)


def test_arbitrary_run_and_invocation_ids_are_not_trusted(local):
    store, world, owner = local
    with pytest.raises(InvalidRequest):
        store.create_scope(
            owner, world.id, CreateScope(kind="run-shared", name="fake run", run_id=uuid4())
        )
    with pytest.raises(Forbidden):
        store.create_scope(
            owner,
            world.id,
            CreateScope(kind="invocation-private", name="fake invocation", invocation_id=uuid4()),
        )
    with pytest.raises(Forbidden):
        store.bind_context(owner, BindContext(run_id=uuid4()))


def test_scope_paths_and_world_mismatch_fail_closed(local):
    store, world, owner = local
    _, private, _ = delegate(store, owner, world)
    other = store.create_world(owner, CreateWorld(name="Other"))
    with pytest.raises(NotFound):
        store.world_path(world.id, "../../escape")
    with pytest.raises(NotFound):
        store.get_scope(owner, other.id, private.id)


def test_explicit_coordinator_inspection_is_read_only_and_never_inherited(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world, coordinator_read=True)
    assert private.coordinator_credential_id == owner.credential_id
    receipt = store.capture(child, world.id, note(scope_id=private.id))
    assert (
        store.get_episode(owner, world.id, receipt.episode.id, scope_id=private.id).id
        == receipt.episode.id
    )
    with pytest.raises(Forbidden):
        store.capture(owner, world.id, note("parent-write", scope_id=private.id))
    sibling, _, _ = delegate(store, owner, world)
    with pytest.raises(Forbidden):
        store.get_scope(sibling, world.id, private.id)


def test_nested_coordinator_anchor_grants_only_direct_parent_read(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world, allow_delegation=True)
    grandchild, grandprivate, _ = delegate(
        store,
        child,
        world,
        coordinator_read=True,
        grants=[ScopeGrant(world_id=world.id, scope_id=private.id, operations=["read"])],
    )
    receipt = store.capture(grandchild, world.id, note(scope_id=grandprivate.id))
    assert store.get_episode(child, world.id, receipt.episode.id, scope_id=grandprivate.id)
    with pytest.raises(Forbidden):
        store.get_scope(owner, world.id, grandprivate.id)
    store.revoke_token(child, grandchild.credential_id)
    # Retention and the coordinator's explicit audience survive child expiry.
    assert store.get_episode(child, world.id, receipt.episode.id, scope_id=grandprivate.id)


def test_restricted_manage_cannot_detach_scope_rights_from_ancestor_lifetime(local):
    store, world, owner = local
    private = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="notes")
    )
    child_issued = store.delegate_credential(
        owner,
        DelegateCredential(
            grants=[
                ScopeGrant(world_id=world.id, scope_id=private.id, operations=["read", "manage"])
            ]
        ),
    )
    child = store.authenticate(child_issued["token"])
    peer = store.authenticate(
        store.issue_token(
            owner, CreateToken(user_id="owner", agent_id="peer", grants={world.id: "writer"})
        )
    )
    with pytest.raises(Forbidden):
        store.set_scope_grant(
            child,
            world.id,
            private.id,
            UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
        )
    with pytest.raises(Forbidden):
        store.get_scope(peer, world.id, private.id)


def test_persistent_scope_preserves_agent_identity_and_distinct_invocation_provenance(local):
    store, world, owner = local
    agent_id = uuid4()
    scope = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Specialist", agent_id=agent_id)
    )
    grants = [ScopeGrant(world_id=world.id, scope_id=scope.id, operations=["read", "write"])]
    first = store.authenticate(
        store.delegate_credential(owner, DelegateCredential(agent_id=agent_id, grants=grants))[
            "token"
        ]
    )
    second = store.authenticate(
        store.delegate_credential(owner, DelegateCredential(agent_id=agent_id, grants=grants))[
            "token"
        ]
    )
    one = store.capture(first, world.id, note("one", scope_id=scope.id)).episode
    two = store.capture(second, world.id, note("two", scope_id=scope.id)).episode
    assert one.agent_uuid == two.agent_uuid == agent_id
    assert one.invocation_id != two.invocation_id
    replay = store.capture(second, world.id, note("one", scope_id=scope.id))
    assert replay.replayed
    assert replay.episode.invocation_id == first.invocation_id


def test_bound_context_attributes_ordinary_agent_invocation_and_rejects_other_destination(local):
    store, world, owner = local
    shared = store.list_scopes(owner, world.id)[0]
    agent_id = uuid4()
    context = store.bind_context(
        owner,
        BindContext(
            agent_id=agent_id, write_scope=ScopeSelection(world_id=world.id, scope_id=shared.id)
        ),
    )
    captured = store.capture(owner, world.id, note(context_id=context.id)).episode
    assert captured.context_id == context.id
    assert captured.invocation_id == context.invocation_id
    assert captured.agent_uuid == agent_id
    private = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="other")
    )
    with pytest.raises(Forbidden):
        store.capture(owner, world.id, note("wrong", scope_id=private.id, context_id=context.id))


def test_bound_context_cannot_be_spoofed_across_credentials_or_missing_write(local):
    store, world, owner = local
    child, private, issued = delegate(store, owner, world)
    with pytest.raises(NotFound):
        store.capture(owner, world.id, note(context_id=issued["context"].id))
    read_only = store.bind_context(
        child, BindContext(read_scopes=[ScopeSelection(world_id=world.id, scope_id=private.id)])
    )
    with pytest.raises(Forbidden):
        store.capture(child, world.id, note(scope_id=private.id, context_id=read_only.id))


def test_restore_rejects_live_source_context_binding(local):
    store, world, owner = local
    shared = store.list_scopes(owner, world.id)[0]
    context = store.bind_context(
        owner, BindContext(write_scope=ScopeSelection(world_id=world.id, scope_id=shared.id))
    )
    with pytest.raises(Forbidden):
        store.restore(owner, world.id, [note(context_id=context.id)])


def test_nested_fresh_private_scope_requires_no_inheritance_of_parent_scratch(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world, allow_delegation=True)
    parent_note = store.capture(child, world.id, note(scope_id=private.id))
    grandchild, grandprivate, _ = delegate(store, child, world)
    assert store.list_scopes(grandchild, world.id)[0].id == grandprivate.id
    with pytest.raises(Forbidden):
        store.get_episode(grandchild, world.id, parent_note.episode.id, scope_id=private.id)
    store.capture(grandchild, world.id, note("child-only", scope_id=grandprivate.id))
    with pytest.raises(Forbidden):
        store.get_scope(child, world.id, grandprivate.id)


def test_administrator_cannot_delegate_another_users_private_world(local):
    store, _, owner = local
    other = store.create_world(owner, CreateWorld(name="Other private", owner_id="other-user"))
    shared = store.list_scopes(owner, other.id)[0]
    with pytest.raises(Forbidden):
        store.delegate_credential(owner, DelegateCredential(private_world_id=other.id))
    with pytest.raises(Forbidden):
        store.delegate_credential(
            owner,
            DelegateCredential(
                grants=[ScopeGrant(world_id=other.id, scope_id=shared.id, operations=["read"])]
            ),
        )


def flow(reads, write):
    return ScopeFlow(
        read_scopes=[ScopeSelection(world_id=world, scope_id=sid) for world, sid in reads],
        write_scope=ScopeSelection(world_id=write[0], scope_id=write[1]),
    )


def test_scope_flow_allows_shared_run_context_into_narrower_private_audience(local):
    store, world, owner = local
    run = store.create_scope(owner, world.id, CreateScope(kind="run-shared", name="Task"))
    shared = store.list_scopes(owner, world.id)[0]
    child, private, _ = delegate(
        store,
        owner,
        world,
        grants=[
            ScopeGrant(world_id=world.id, scope_id=run.id, operations=["read"]),
            ScopeGrant(world_id=world.id, scope_id=shared.id, operations=["read"]),
        ],
    )
    assert store.scope_flow(
        child, flow([(world.id, run.id), (world.id, shared.id)], (world.id, private.id))
    )


def test_scope_flow_private_audiences_require_actual_reader_inclusion(local):
    store, world, owner = local
    source = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Source")
    )
    target = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Target")
    )
    assert store.scope_flow(owner, flow([(world.id, source.id)], (world.id, target.id)))
    peer = store.authenticate(
        store.issue_token(
            owner, CreateToken(user_id="owner", agent_id="peer", grants={world.id: "reader"})
        )
    )
    store.set_scope_grant(
        owner,
        world.id,
        target.id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    assert not store.scope_flow(owner, flow([(world.id, source.id)], (world.id, target.id)))
    store.set_scope_grant(
        owner,
        world.id,
        source.id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    assert store.scope_flow(owner, flow([(world.id, source.id)], (world.id, target.id)))


def test_scope_flow_coordinator_must_also_read_source(local):
    store, world, owner = local
    child, private, _ = delegate(store, owner, world, allow_delegation=True, coordinator_read=True)
    _grandchild, grandprivate, _ = delegate(store, child, world, coordinator_read=True)
    assert store.get_scope(child, world.id, grandprivate.id)
    assert not store.scope_flow(child, flow([(world.id, grandprivate.id)], (world.id, private.id)))


def test_scope_flow_root_shared_capture_rejects_broader_current_readers(local):
    store, world, owner = local
    private = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Private")
    )
    shared = store.list_scopes(owner, world.id)[0]
    store.issue_token(
        owner, CreateToken(user_id="owner", agent_id="global-reader", grants={world.id: "reader"})
    )
    assert not store.scope_flow(owner, flow([(world.id, private.id)], (world.id, shared.id)))


def test_scope_flow_ignores_revoked_and_expired_readers(local):
    store, world, owner = local
    source = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Source")
    )
    target = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Target")
    )
    peer = store.authenticate(
        store.issue_token(
            owner, CreateToken(user_id="owner", agent_id="peer", grants={world.id: "reader"})
        )
    )
    store.set_scope_grant(
        owner,
        world.id,
        target.id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    check = flow([(world.id, source.id)], (world.id, target.id))
    assert not store.scope_flow(owner, check)
    store.revoke_token(owner, peer.credential_id)
    assert store.scope_flow(owner, check)
    issued = store.delegate_credential(
        owner,
        DelegateCredential(
            grants=[ScopeGrant(world_id=world.id, scope_id=target.id, operations=["read"])]
        ),
    )
    assert not store.scope_flow(owner, check)
    with store._control() as db:
        table = store._table(db, "credential_contexts")
        db.execute(
            table.update()
            .where(table.c.credential_id == issued["credential_id"])
            .values(
                expires_at=time.time() - 1,
            )
        )
    assert store.scope_flow(owner, check)


def test_scope_flow_revoked_caller_and_cross_world_readers_fail_closed(local):
    store, world, owner = local
    other = store.create_world(owner, CreateWorld(name="Other"))
    source = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Source")
    )
    target = store.create_scope(
        owner, other.id, CreateScope(kind="agent-persistent", name="Target")
    )
    peer = store.authenticate(
        store.issue_token(
            owner,
            CreateToken(user_id="owner", agent_id="other-reader", grants={other.id: "reader"}),
        )
    )
    store.set_scope_grant(
        owner,
        other.id,
        target.id,
        UpdateScopeGrant(credential_id=peer.credential_id, operations=["read"]),
    )
    assert not store.scope_flow(owner, flow([(world.id, source.id)], (other.id, target.id)))
    child, private, _ = delegate(store, owner, world)
    store.revoke_token(owner, child.credential_id)
    with pytest.raises(Unauthorized):
        store.scope_flow(child, flow([], (world.id, private.id)))


def test_scope_creation_rejects_unissued_run_on_other_scope_kinds(local):
    store, world, owner = local
    for kind in ("agent-persistent", "invocation-private"):
        with pytest.raises(Forbidden):
            store.create_scope(
                owner, world.id, CreateScope(kind=kind, name="Bad run", run_id=uuid4())
            )
