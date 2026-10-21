"""Current-contract lifecycle proofs; injected purge adapters make no backend claim."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from memory_worlds.errors import Conflict, Forbidden, NotFound, Unauthorized, Unavailable
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.lifecycle import (
    EpisodeLifecycle,
    LifecycleRequest,
    RetentionEngine,
    RetentionPolicy,
    RetentionRequest,
    assert_source_live,
    has_pending_purges,
    visible_clause,
)
from memory_worlds.models import (
    CreateScope,
    CreateToken,
    DelegateCredential,
    EpisodeInput,
    Role,
    ScopeKind,
    ScopeSelection,
    UpdateScopeGrant,
)
from memory_worlds.outbox import PublicationOutbox, RoutedPublication
from memory_worlds.store import MemoryStore


@pytest.fixture
def local(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    scope = store.create_scope(
        owner, world.id, CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Private notes")
    )
    lifecycle = EpisodeLifecycle(store)
    retention = RetentionEngine(store)
    return SimpleNamespace(**locals())


def capture(env, source="source-1", **changes):
    values = dict(
        scope_id=env.scope.id,
        source_id=source,
        content="Private source body, including its private metadata",
        metadata={"sensitive": "private metadata"},
        occurred_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    values.update(changes)
    return env.store.capture(env.owner, env.world.id, EpisodeInput(**values)).episode


def request(*episodes, action="erase", operation_id=None):
    return LifecycleRequest(
        operation_id=operation_id or uuid4(),
        action=action,
        episode_ids=[episode.id for episode in episodes],
    )


def generations(env, **states):
    with env.store._control() as db:
        table = env.store._table(db, "projection_generations")
        for generation, state in states.items():
            db.execute(
                table.insert().values(
                    world_id=env.world.id,
                    scope_id=str(env.scope.id),
                    generation=generation,
                    state=state,
                    created_at="2025-01-01T00:00:00+00:00",
                    retired_at=None if state == "active" else "2025-01-02T00:00:00+00:00",
                )
            )
    if "active" in states.values():
        active = next(key for key, state in states.items() if state == "active")
        with env.store._world_db(env.world.id, env.scope.id) as db:
            table = env.store._table(db, "projection")
            db.execute(table.update().values(generation=active, state="ready"))


def source_rows(env, name):
    with env.store._world_db(env.world.id, env.scope.id) as db:
        table = env.store._table(db, name)
        return [dict(row) for row in db.execute(select(table)).mappings()]


class Purger:
    def __init__(self, *, fail=()):
        self.fail = set(fail)
        self.calls = []

    async def purge_generation(self, world, generation):
        self.calls.append((world.id, str(world.scope_id), generation))
        if generation in self.fail:
            raise RuntimeError("PRIVATE SOURCE BODY must not enter an operational receipt")


def test_preview_is_content_free_and_makes_no_source_changes(local):
    e = local
    item = capture(e)
    generations(e, active="active", retired="retired")
    preview = e.lifecycle.preview(e.owner, e.world.id, e.scope.id, request(item))
    assert preview["generations_to_purge"] == ["active", "retired"]
    assert preview["retained_source_records"] == 1 and preview["writes"] is False
    assert item.content not in str(preview) and "private metadata" not in str(preview)
    assert source_rows(e, "lifecycle_tombstones") == []
    assert len(source_rows(e, "episodes")) == 1


def test_retraction_hides_all_ordinary_reads_but_retains_the_evidence(local):
    e = local
    item = capture(e)
    operation = request(item, action="retract")
    receipt = e.lifecycle.apply(e.owner, e.world.id, e.scope.id, operation)
    assert receipt["source_state"] == "retracted" and not receipt["erasure_complete"]
    assert len(source_rows(e, "episodes")) == 1
    assert not e.store.list_episodes(e.owner, e.world.id, scope_id=e.scope.id)
    assert not e.store.search(e.owner, [e.world.id], "Private", 10, scope_id=e.scope.id)
    assert not e.store.pending_jobs(e.world.id, scope_id=e.scope.id)
    assert not e.store.projection_episodes(e.world.id, scope_id=e.scope.id)
    with pytest.raises(NotFound):
        e.store.get_episode(e.owner, e.world.id, item.id, scope_id=e.scope.id)
    with e.store._world_db(e.world.id, e.scope.id) as db:
        table = e.store._table(db, "episodes")
        assert not db.execute(select(table).where(visible_clause(e.store, db, table))).first()
        with pytest.raises(Conflict):
            assert_source_live(e.store, db, item.source_id)
    with pytest.raises(Conflict):
        capture(e)


def test_erasure_is_atomic_durable_and_tombstone_blocks_exact_reimport(local):
    e = local
    item = capture(e)
    operation = request(item)
    first = e.lifecycle.apply(e.owner, e.world.id, e.scope.id, operation)
    assert first["erasure_complete"] is True and first["pending_generations"] == []
    assert source_rows(e, "episodes") == [] and source_rows(e, "projection_jobs") == []
    assert len(source_rows(e, "lifecycle_tombstones")) == 1
    assert item.content not in str(source_rows(e, "lifecycle_operations"))
    reopened = MemoryStore(e.tmp_path)
    manager = EpisodeLifecycle(reopened)
    assert manager.apply(e.owner, e.world.id, e.scope.id, operation) == first
    assert manager.receipt(e.owner, e.world.id, e.scope.id, operation.operation_id) == first
    with pytest.raises(Conflict):
        reopened.capture(
            e.owner,
            e.world.id,
            EpisodeInput(**item.model_dump(include=set(EpisodeInput.model_fields))),
        )


def test_all_targets_validate_before_any_tombstone_or_delete(local):
    e = local
    item = capture(e)
    body = request(item)
    body.episode_ids.append("missing-final-target")
    with pytest.raises(NotFound):
        e.lifecycle.apply(e.owner, e.world.id, e.scope.id, body)
    assert len(source_rows(e, "episodes")) == 1
    assert source_rows(e, "lifecycle_tombstones") == []
    assert source_rows(e, "lifecycle_operations") == []


def test_failed_wal_checkpoint_keeps_erasure_incomplete_and_retryable(local, monkeypatch):
    e = local
    item = capture(e)
    body = request(item)
    checkpoint = e.store.persistence.checkpoint_source

    def busy(*_args):
        raise Unavailable("Synthetic external reader holds a native WAL snapshot")

    monkeypatch.setattr(e.store.persistence, "checkpoint_source", busy)
    with pytest.raises(Unavailable):
        e.lifecycle.apply(e.owner, e.world.id, e.scope.id, body)
    assert source_rows(e, "episodes") == []
    assert not e.lifecycle.receipt(e.owner, e.world.id, e.scope.id, body.operation_id)[
        "erasure_complete"
    ]
    monkeypatch.setattr(e.store.persistence, "checkpoint_source", checkpoint)
    assert e.lifecycle.apply(e.owner, e.world.id, e.scope.id, body)["erasure_complete"]


def test_operation_identity_conflicts_and_erased_state_is_monotone(local):
    e = local
    item, other = capture(e), capture(e, "other")
    body = request(item)
    e.lifecycle.apply(e.owner, e.world.id, e.scope.id, body)
    with pytest.raises(Conflict):
        e.lifecycle.apply(
            e.owner, e.world.id, e.scope.id, request(other, operation_id=body.operation_id)
        )
    with pytest.raises(Conflict):
        e.lifecycle.apply(e.owner, e.world.id, e.scope.id, request(item, action="retract"))
    assert e.store.get_episode(e.owner, e.world.id, other.id, scope_id=e.scope.id)


def test_explicit_episode_targets_cannot_cross_scope_boundaries(local):
    e = local
    item = capture(e)
    other = e.store.create_scope(
        e.owner,
        e.world.id,
        CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Different private audience"),
    )
    with pytest.raises(NotFound):
        e.lifecycle.apply(e.owner, e.world.id, other.id, request(item))
    assert e.store.get_episode(e.owner, e.world.id, item.id, scope_id=e.scope.id)


def test_retraction_can_progress_to_erasure_without_exposing_original_content(local):
    e = local
    item = capture(e)
    e.lifecycle.apply(e.owner, e.world.id, e.scope.id, request(item, action="retract"))
    result = e.lifecycle.apply(e.owner, e.world.id, e.scope.id, request(item))
    assert result["erasure_complete"] and source_rows(e, "episodes") == []
    assert [row["state"] for row in source_rows(e, "lifecycle_tombstones")] == ["erased"]


@pytest.mark.parametrize(
    "role,operations",
    [
        (Role.WRITER, ["read", "write", "manage"]),
        (Role.MANAGER, ["read", "write"]),
    ],
)
def test_world_manager_and_private_scope_manage_are_both_required(local, role, operations):
    e = local
    item = capture(e)
    token = e.store.issue_token(
        e.owner, CreateToken(user_id="alice", agent_id="operator", grants={e.world.id: role})
    )
    principal = e.store.authenticate(token)
    e.store.set_scope_grant(
        e.owner,
        e.world.id,
        e.scope.id,
        UpdateScopeGrant(credential_id=principal.credential_id, operations=operations),
    )
    with pytest.raises(Forbidden):
        e.lifecycle.apply(principal, e.world.id, e.scope.id, request(item))
    assert len(source_rows(e, "episodes")) == 1


def test_live_manager_grant_authorizes_and_revocation_blocks_receipt_replay(local):
    e = local
    item = capture(e)
    token = e.store.issue_token(
        e.owner, CreateToken(user_id="alice", agent_id="manager", grants={e.world.id: Role.MANAGER})
    )
    principal = e.store.authenticate(token)
    e.store.set_scope_grant(
        e.owner,
        e.world.id,
        e.scope.id,
        UpdateScopeGrant(credential_id=principal.credential_id, operations=["manage"]),
    )
    body = request(item)
    e.lifecycle.apply(principal, e.world.id, e.scope.id, body)
    e.store.revoke_token(e.owner, principal.credential_id)
    with pytest.raises(Unauthorized):
        e.lifecycle.apply(principal, e.world.id, e.scope.id, body)


async def test_graph_erasure_waits_for_every_generation_and_failed_purge_is_retryable(local):
    e = local
    item = capture(e)
    generations(e, live="active", stale="retired", unknown="failed")
    body = request(item)
    receipt = e.lifecycle.apply(e.owner, e.world.id, e.scope.id, body)
    assert not receipt["erasure_complete"]
    assert receipt["pending_generations"] == ["live", "stale", "unknown"]
    assert has_pending_purges(e.store, e.world.id, e.scope.id)
    adapter = Purger(fail=["unknown"])
    with pytest.raises(InvalidRequest):
        await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, adapter)
    assert adapter.calls == []
    result = await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, adapter, workers_drained=True)
    assert result["failed_generations"] == ["unknown"]
    assert "PRIVATE SOURCE BODY" not in str(source_rows(e, "graph_purges"))
    assert not e.lifecycle.receipt(e.owner, e.world.id, e.scope.id, body.operation_id)[
        "erasure_complete"
    ]
    adapter.fail.clear()
    await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, adapter, workers_drained=True)
    assert [item[2] for item in adapter.calls] == ["live", "stale", "unknown", "unknown"]
    assert e.lifecycle.receipt(e.owner, e.world.id, e.scope.id, body.operation_id)[
        "erasure_complete"
    ]
    assert not has_pending_purges(e.store, e.world.id, e.scope.id)
    assert all(row["state"] == "purged" for row in e.store.generation_inventory())


async def test_later_purge_does_not_delete_a_new_clean_active_generation(local):
    e = local
    item = capture(e)
    generations(e, original="active")
    e.lifecycle.apply(e.owner, e.world.id, e.scope.id, request(item))
    adapter = Purger()
    await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, adapter, workers_drained=True)
    generations(e, clean="active")
    await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, adapter, workers_drained=True)
    assert [item[2] for item in adapter.calls] == ["original"]
    assert e.store.get_world(e.owner, e.world.id, scope_id=e.scope.id).generation == "clean"


async def test_completed_receipt_does_not_reopen_when_a_different_record_is_erased(local):
    e = local
    first, second = capture(e), capture(e, "second")
    generations(e, original="active")
    body = request(first)
    e.lifecycle.apply(e.owner, e.world.id, e.scope.id, body)
    await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, Purger(), workers_drained=True)
    generations(e, clean="active")
    e.lifecycle.apply(e.owner, e.world.id, e.scope.id, request(second))
    assert e.lifecycle.receipt(e.owner, e.world.id, e.scope.id, body.operation_id)[
        "erasure_complete"
    ]


def retention_request(e, **changes):
    values = dict(
        source_before=datetime.now(UTC) + timedelta(seconds=-1),
        scopes=[ScopeSelection(world_id=e.world.id, scope_id=e.scope.id)],
    )
    values.update(changes)
    return RetentionRequest(operation_id=uuid4(), policy=RetentionPolicy(**values))


def age_sources(e):
    with e.store._world_db(e.world.id, e.scope.id) as db:
        table = e.store._table(db, "episodes")
        db.execute(table.update().values(received_at="2025-01-01T00:00:00+00:00"))


def test_retention_preview_is_bounded_explicit_and_replay_does_not_expand_the_plan(local):
    e = local
    items = [capture(e, f"source-{index}") for index in range(3)]
    age_sources(e)
    body = retention_request(e, limit=2)
    preview = e.retention.preview(e.owner, body)
    assert preview["selected"] == 2 and preview["more_available"] and not preview["writes"]
    assert len(source_rows(e, "episodes")) == 3
    result = e.retention.apply(e.owner, body)
    assert result["state"] == "applied" and result["selected"] == 2
    assert e.retention.apply(e.owner, body) == result
    assert [row["id"] for row in source_rows(e, "episodes")] == [items[2].id]
    body.policy.limit = 3
    with pytest.raises(Conflict):
        e.retention.apply(e.owner, body)


def test_retention_can_erase_retained_retractions(local):
    e = local
    item = capture(e)
    age_sources(e)
    e.lifecycle.apply(e.owner, e.world.id, e.scope.id, request(item, action="retract"))
    assert e.retention.apply(e.owner, retention_request(e))["selected"] == 1
    assert source_rows(e, "episodes") == []


def test_retention_resume_reuses_frozen_ids_after_partial_scope_commit(local, monkeypatch):
    e = local
    capture(e)
    age_sources(e)
    body = retention_request(e)
    real = e.retention.lifecycle.apply
    failed = False

    def commit_then_fail(*args):
        nonlocal failed
        result = real(*args)
        if not failed:
            failed = True
            raise RuntimeError("Simulated process interruption after a source commit")
        return result

    monkeypatch.setattr(e.retention.lifecycle, "apply", commit_then_fail)
    with pytest.raises(RuntimeError):
        e.retention.apply(e.owner, body)
    assert e.retention.receipt(e.owner, body.operation_id)["state"] == "pending"
    capture(e, "newer-input")
    age_sources(e)
    result = e.retention.apply(e.owner, body)
    assert result["selected"] == 1
    assert [row["source_id"] for row in source_rows(e, "episodes")] == ["newer-input"]


def test_terminal_outbox_retention_scrubs_content_and_preserves_idempotency(local):
    e = local
    outbox = PublicationOutbox(e.tmp_path, e.store.authority_id, persistence=e.store.persistence)
    mount = SimpleNamespace(id="work", url="https://work.invalid", authority_id="remote")
    publications = []
    for state in ("delivered", "blocked", "pending", "processing"):
        publication = RoutedPublication(
            destination_mount_id="work",
            world_id="team",
            source_id=f"source-{state}",
            content="Private selected excerpt",
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        receipt = outbox.enqueue(e.owner, mount, publication)
        with outbox.db() as db:
            table = outbox._table(db)
            db.execute(
                table.update()
                .where(table.c.id == receipt.id)
                .values(state=state, created_at="2025-01-01T00:00:00+00:00")
            )
        publications.append((publication, receipt))
    body = RetentionRequest(
        operation_id=uuid4(),
        policy=RetentionPolicy(terminal_deliveries_before=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert e.retention.apply(e.owner, body)["selected"] == 2
    with outbox.db() as db:
        rows = list(db.execute(select(outbox._table(db))).mappings())
    assert {row["state"] for row in rows if row["pruned"]} == {"delivered", "blocked"}
    assert all(row["payload"] == "{}" and row["principal"] == "{}" for row in rows if row["pruned"])
    assert all("Private selected excerpt" in row["payload"] for row in rows if not row["pruned"])
    assert outbox.enqueue(e.owner, mount, publications[0][0]).id == publications[0][1].id
    with pytest.raises(Forbidden):
        outbox.retry(e.owner, publications[1][1].id)


async def test_retention_purges_only_old_retired_generations_preserving_active(local):
    e = local
    generations(e, active="active", old="retired", failed="failed")
    body = RetentionRequest(
        operation_id=uuid4(),
        policy=RetentionPolicy(retired_generations_before=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert e.retention.apply(e.owner, body)["selected"] == 2
    purger = Purger()
    await e.lifecycle.purge(e.owner, e.world.id, e.scope.id, purger, workers_drained=True)
    assert [item[2] for item in purger.calls] == ["failed", "old"]
    assert e.store.get_world(e.owner, e.world.id, scope_id=e.scope.id).generation == "active"


def test_retention_preserves_terminal_delivery_that_became_pending_after_plan(local, monkeypatch):
    e = local
    outbox = PublicationOutbox(e.tmp_path, e.store.authority_id, persistence=e.store.persistence)
    mount = SimpleNamespace(id="work", url="https://work.invalid", authority_id="remote")
    item = outbox.enqueue(
        e.owner,
        mount,
        RoutedPublication(
            destination_mount_id="work",
            world_id="team",
            source_id="selected",
            content="Preserve retryable publication",
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        ),
    )
    with outbox.db() as db:
        table = outbox._table(db)
        db.execute(
            table.update()
            .where(table.c.id == item.id)
            .values(state="blocked", created_at="2025-01-01T00:00:00+00:00")
        )
    original = e.retention._plan

    def plan_then_retry(*args):
        plan = original(*args)
        outbox.retry(e.owner, item.id)
        return plan

    monkeypatch.setattr(e.retention, "_plan", plan_then_retry)
    body = RetentionRequest(
        operation_id=uuid4(),
        policy=RetentionPolicy(terminal_deliveries_before=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    result = e.retention.apply(e.owner, body)
    assert result["outcomes"][0]["state"] == "preserved"
    with outbox.db() as db:
        row = db.execute(select(outbox._table(db))).mappings().one()
        assert row["state"] == "pending" and not row["pruned"]
        assert "Preserve retryable publication" in row["payload"]


def test_retention_targets_expired_invocations_but_preserves_live_and_unnamed_scopes(local):
    e = local
    child = e.store.delegate_credential(e.owner, DelegateCredential(private_world_id=e.world.id))
    principal = e.store.authenticate(child["token"])
    private = child["private_scope"]
    e.store.capture(
        principal,
        e.world.id,
        EpisodeInput(
            scope_id=private.id,
            source_id="child-source",
            content="Expired private evidence",
            occurred_at=datetime(2025, 1, 1, tzinfo=UTC),
        ),
    )
    capture(e, "keep-persistent")
    with e.store._control() as db:
        table = e.store._table(db, "credential_contexts")
        db.execute(
            table.update()
            .where(table.c.credential_id == principal.credential_id)
            .values(expires_at=datetime(2025, 1, 1, tzinfo=UTC).timestamp())
        )
    body = RetentionRequest(
        operation_id=uuid4(),
        policy=RetentionPolicy(expired_invocations_before=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    plan = e.retention.preview(e.owner, body)
    assert plan["selected"] == 1 and plan["targets"][0]["scope_id"] == str(private.id)
    e.retention.apply(e.owner, body)
    assert len(source_rows(e, "episodes")) == 1


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"source_before": "2026-01-01T00:00:00Z"},
        {"source_before": "2026-01-01T00:00:00"},
        {"terminal_deliveries_before": "2999-01-01T00:00:00Z"},
        {"terminal_deliveries_before": "2026-01-01T00:00:00Z", "limit": 501},
    ],
)
def test_retention_rejects_implicit_naive_future_and_unbounded_policies(values):
    with pytest.raises(ValidationError):
        RetentionPolicy(**values)


def test_retention_normalizes_cutoffs_and_requires_live_administrator(local):
    e = local
    policy = RetentionPolicy(terminal_deliveries_before="2026-01-01T03:00:00+03:00")
    assert policy.terminal_deliveries_before == datetime(2026, 1, 1, tzinfo=UTC)
    token = e.store.issue_token(
        e.owner, CreateToken(user_id="alice", agent_id="writer", grants={e.world.id: Role.MANAGER})
    )
    with pytest.raises(Forbidden):
        e.retention.preview(
            e.store.authenticate(token), RetentionRequest(operation_id=uuid4(), policy=policy)
        )
