from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from memory_worlds.errors import Conflict, Forbidden, Unauthorized
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.lifecycle import EpisodeLifecycle, LifecycleRequest
from memory_worlds.models import (
    CreateScope,
    CreateToken,
    DelegateCredential,
    EpisodeInput,
    ScopedPublication,
    ScopeFlow,
    ScopeGrant,
    ScopeSelection,
    UpdateScopeGrant,
)
from memory_worlds.store import MemoryStore
from memory_worlds.thread_models import CreateThread, ThreadSource
from memory_worlds.threads import ThreadService


@pytest.fixture
def local(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap()
    admin = store.authenticate(token)
    ordinary = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(user_id="owner", agent_id="primary", grants={world.id: "manager"}),
        )
    )
    return store, world, admin, ordinary, ThreadService(store)


def create(local, **kwargs):
    _, world, _, owner, service = local
    return service.create(
        owner, CreateThread(operation_id=uuid4(), world_id=world.id, name="Research", **kwargs)
    )


def source(local, thread, content="Selected evidence"):
    store, _, _, owner, _ = local
    receipt = store.capture(
        owner,
        thread.world_id,
        EpisodeInput(
            scope_id=thread.scope_id,
            source_id=str(uuid4()),
            content=content,
            occurred_at=datetime.now(UTC),
        ),
    )
    return ThreadSource(episode_id=receipt.episode.id, digest=receipt.episode.digest)


def test_ordinary_create_has_dedicated_private_scope_and_revision(local):
    store, world, _, owner, service = local
    thread = create(local, alias="research")
    scope = store.get_scope(owner, world.id, thread.scope_id)
    assert scope.kind == "thread-private"
    assert scope.thread_id == thread.id
    assert scope.id != world.scope_id
    assert service.get(owner, thread.id) == thread
    assert service.list(owner) == [thread]
    assert service.snapshot(owner, thread.id).sources == []
    assert service.snapshot(owner, thread.id).digest == thread.head_digest


def test_creation_replay_across_reopen_preserves_ids_and_payload(local):
    store, world, _, owner, service = local
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Research")
    first = service.create(owner, request)
    reopened = MemoryStore(store.data_dir)
    replay = ThreadService(reopened).create(owner, request)
    assert replay == first
    assert len(service.list(owner)) == 1


def test_creation_receipt_stays_original_after_thread_head_advances(local):
    _, world, _, owner, service = local
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Research")
    first = service.create(owner, request)
    service.append(owner, first.id, uuid4(), 0, [source(local, first)])
    assert service.create(owner, request) == first
    assert service.get(owner, first.id).revision == 1


@pytest.mark.parametrize("field,value", [("name", "Changed"), ("alias", "changed")])
def test_changed_create_intent_conflicts(local, field, value):
    _, world, _, owner, service = local
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Research")
    service.create(owner, request)
    with pytest.raises(Conflict):
        service.create(owner, request.model_copy(update={field: value}))


def test_same_thread_uuid_different_operation_and_alias_conflicts(local):
    thread = create(local, alias="research")
    with pytest.raises(Conflict):
        create(local, thread_id=thread.id)
    with pytest.raises(Conflict):
        create(local, alias="research")


def test_initialization_crash_reuses_prepared_ids(local, monkeypatch):
    store, world, _, owner, service = local
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Research")
    initialize = store._initialize_world
    attempted = []

    def fail(world_id, scope_id):
        attempted.append(scope_id)
        initialize(world_id, scope_id)
        raise RuntimeError("simulated lost initialization acknowledgement")

    monkeypatch.setattr(store, "_initialize_world", fail)
    with pytest.raises(RuntimeError):
        service.create(owner, request)
    assert service.list(owner) == []
    monkeypatch.setattr(store, "_initialize_world", initialize)
    thread = service.create(owner, request)
    assert thread.scope_id == attempted[0]


def test_uuid_name_same_user_and_admin_do_not_supply_implicit_thread_grants(local):
    store, world, admin, owner, service = local
    thread = create(local)
    sibling = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(user_id="owner", agent_id="sibling", grants={world.id: "manager"}),
        )
    )
    other_user = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(user_id="other", agent_id="same-name"),
        )
    )
    for caller in (admin, sibling, other_user):
        assert service.list(caller) == []
        for operation in (
            lambda: service.get(caller, thread.id),
            lambda: service.snapshot(caller, thread.id),
            lambda: store.get_scope(caller, world.id, thread.scope_id),
        ):
            with pytest.raises(Forbidden):
                operation()


def test_live_explicit_grant_and_revocation_control_discovery(local):
    store, world, admin, owner, service = local
    thread = create(local)
    sibling = store.authenticate(
        store.issue_token(
            admin,
            CreateToken(user_id="owner", agent_id="sibling", grants={world.id: "reader"}),
        )
    )
    store.set_scope_grant(
        owner,
        world.id,
        thread.scope_id,
        UpdateScopeGrant(credential_id=sibling.credential_id, operations=["read"]),
    )
    assert service.list(sibling) == [thread]
    with pytest.raises(Forbidden):
        service.append(sibling, thread.id, uuid4(), 0, [source(local, thread)])
    store.set_scope_grant(
        owner,
        world.id,
        thread.scope_id,
        UpdateScopeGrant(credential_id=sibling.credential_id, operations=[]),
    )
    assert service.list(sibling) == []
    with pytest.raises(Forbidden):
        service.get(sibling, thread.id)


def test_delegated_reader_needs_exact_grant_and_cannot_create_or_redelegate(local):
    store, world, _, owner, service = local
    thread, sibling = create(local), create(local)
    child = store.authenticate(
        store.delegate_credential(
            owner,
            DelegateCredential(
                grants=[
                    ScopeGrant(world_id=world.id, scope_id=thread.scope_id, operations=["read"])
                ],
            ),
        )["token"]
    )
    assert service.list(child) == [thread]
    with pytest.raises(Forbidden):
        service.get(child, sibling.id)
    with pytest.raises(Forbidden):
        service.create(child, CreateThread(operation_id=uuid4(), world_id=world.id, name="Escape"))
    with pytest.raises(Forbidden):
        store.delegate_credential(
            child,
            DelegateCredential(
                grants=[
                    ScopeGrant(world_id=world.id, scope_id=thread.scope_id, operations=["read"])
                ],
            ),
        )
    store.revoke_token(owner, child.credential_id)
    with pytest.raises(Unauthorized):
        service.get(child, thread.id)


def test_scope_flow_does_not_make_invocation_private_resumable(local):
    store, world, _, owner, _ = local
    thread = create(local)
    delegated = store.delegate_credential(owner, DelegateCredential(private_world_id=world.id))
    child = store.authenticate(delegated["token"])
    private = delegated["private_scope"]
    with pytest.raises(Forbidden):
        store.scope_flow(
            child,
            ScopeFlow(
                read_scopes=[ScopeSelection(world_id=world.id, scope_id=private.id)],
                write_scope=ScopeSelection(world_id=world.id, scope_id=thread.scope_id),
            ),
        )


def test_generic_scope_creation_cannot_forge_thread_binding(local):
    store, world, _, owner, _ = local
    for request in (
        CreateScope(kind="thread-private", name="Fake", thread_id=uuid4()),
        CreateScope(kind="agent-persistent", name="Fake", thread_id=uuid4()),
    ):
        with pytest.raises(InvalidRequest):
            store.create_scope(owner, world.id, request)


def test_append_exact_sources_receipt_and_replay(local):
    _, _, _, owner, service = local
    thread = create(local)
    first = source(local, thread)
    operation_id = uuid4()
    revision = service.append(owner, thread.id, operation_id, 0, [first])
    assert revision.revision == 1 and revision.sources == [first]
    assert service.append(owner, thread.id, operation_id, 0, [first]) == revision
    assert service.snapshot(owner, thread.id) == revision
    assert service.get(owner, thread.id).head_digest == revision.digest
    assert service.snapshot(owner, thread.id, 0).sources == []
    second = source(local, thread, "Next selected source")
    next_revision = service.append(owner, thread.id, uuid4(), 1, [second])
    assert next_revision.sources == [first, second]
    assert service.append(owner, thread.id, operation_id, 0, [first]) == revision
    with pytest.raises(Conflict):
        service.append(owner, thread.id, operation_id, 0, [second])
    with pytest.raises(Conflict):
        service.append(owner, thread.id, uuid4(), 0, [second])


def test_sources_require_same_scope_correct_digest_and_unique_membership(local):
    _, _, _, owner, service = local
    thread, sibling = create(local), create(local)
    foreign = source(local, sibling)
    own = source(local, thread)
    for sources in (
        [foreign],
        [own.model_copy(update={"digest": "0" * 64})],
        [own, own],
    ):
        with pytest.raises(Conflict):
            service.append(owner, thread.id, uuid4(), 0, sources)
    assert service.get(owner, thread.id).revision == 0


def test_tombstoned_sources_block_new_and_old_revision_reads(local):
    store, _, _, owner, service = local
    thread = create(local)
    selected = source(local, thread)
    operation_id = uuid4()
    service.append(owner, thread.id, operation_id, 0, [selected])
    EpisodeLifecycle(store).apply(
        owner,
        thread.world_id,
        thread.scope_id,
        LifecycleRequest(operation_id=uuid4(), action="retract", episode_ids=[selected.episode_id]),
    )
    for operation in (
        lambda: service.snapshot(owner, thread.id),
        lambda: service.append(owner, thread.id, operation_id, 0, [selected]),
        lambda: service.append(owner, thread.id, uuid4(), 1, [source(local, thread)]),
    ):
        with pytest.raises(Conflict):
            operation()


def test_concurrent_append_cas_accepts_one_head(local):
    _, _, _, owner, service = local
    thread = create(local)
    sources = [source(local, thread), source(local, thread)]

    def append(selected):
        try:
            return service.append(owner, thread.id, uuid4(), 0, [selected])
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, sources))
    assert sum(result is not None for result in results) == 1
    assert service.get(owner, thread.id).revision == 1


def test_concurrent_create_replay_uses_one_thread(local):
    _, world, _, owner, service = local
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Concurrent")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.create(owner, request), range(2)))
    assert results[0] == results[1]
    assert len(service.list(owner)) == 1


def test_expiry_changes_automatic_selection_not_explicit_reads(local, monkeypatch):
    _, _, _, owner, service = local
    expiry = datetime.now(UTC) + timedelta(hours=1)
    thread = create(local, expires_at=expiry)
    monkeypatch.setattr(service, "_now", lambda db: expiry + timedelta(seconds=1))
    assert service.list(owner) == []
    assert service.list(owner, include_expired=True)[0].state == "expired"
    assert service.get(owner, thread.id).state == "expired"
    assert service.snapshot(owner, thread.id).sources == []
    with pytest.raises(Conflict):
        service.append(owner, thread.id, uuid4(), 0, [source(local, thread)])


def test_creation_replay_rechecks_current_write_grant(local):
    store, world, _, owner, service = local
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Research")
    thread = service.create(owner, request)
    store.set_scope_grant(
        owner,
        world.id,
        thread.scope_id,
        UpdateScopeGrant(credential_id=owner.credential_id, operations=["read"]),
    )
    with pytest.raises(Forbidden):
        service.create(owner, request)
    with store._control() as db:
        table = store._table(db, "thread_operations")
        assert db.execute(select(table.c.state)).scalar_one() == "applied"


def test_expired_thread_blocks_generic_writes_but_keeps_read_and_manager_lifecycle(
    local, monkeypatch
):
    store, world, _, owner, service = local
    expiry = datetime.now(UTC) + timedelta(hours=1)
    thread = create(local, expires_at=expiry)
    saved = source(local, thread)
    monkeypatch.setattr(store.persistence, "database_time", lambda db: expiry.timestamp() + 1)
    assert service.get(owner, thread.id).state == "expired"
    for write in (
        lambda: source(local, thread),
        lambda: store.publish_scope(
            owner,
            world.id,
            thread.scope_id,
            ScopedPublication(
                source_id="expired-publication",
                content="No new write",
                occurred_at=datetime.now(UTC),
            ),
        ),
    ):
        with pytest.raises(Conflict):
            write()
    assert store.get_episode(owner, world.id, saved.episode_id, thread.scope_id).content
    # Management and read delegation stay live so expiry cannot trap retained data.
    store.set_scope_grant(
        owner,
        world.id,
        thread.scope_id,
        UpdateScopeGrant(
            credential_id=owner.credential_id,
            operations=["read", "manage", "delegate"],
        ),
    )
    issued = store.delegate_credential(
        owner,
        DelegateCredential(
            grants=[
                ScopeGrant(
                    world_id=world.id,
                    scope_id=thread.scope_id,
                    operations=["read"],
                )
            ],
        ),
    )
    reader = store.authenticate(issued["token"])
    assert store.get_episode(reader, world.id, saved.episode_id, thread.scope_id).content
    receipt = EpisodeLifecycle(store).apply(
        owner,
        world.id,
        thread.scope_id,
        LifecycleRequest(operation_id=uuid4(), action="erase", episode_ids=[saved.episode_id]),
    )
    assert receipt["erasure_complete"]


def test_orphan_thread_scope_binding_is_not_readable(local):
    store, world, _, owner, _ = local
    thread = create(local)
    with store._control() as db:
        scopes = store._table(db, "scopes")
        db.execute(
            scopes.update().where(scopes.c.id == str(thread.scope_id)).values(thread_id=None)
        )
    with pytest.raises(Conflict):
        store.get_scope(owner, world.id, thread.scope_id)
