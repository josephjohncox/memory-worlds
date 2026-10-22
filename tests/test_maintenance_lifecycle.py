"""Derived-copy visibility and bounded source erasure use real source journals."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from memory_worlds.context_archive import ContextArchive
from memory_worlds.context_models import ContextBundleInput, ContextEvent
from memory_worlds.errors import Conflict, NotFound, Unavailable
from memory_worlds.lifecycle import (
    EpisodeLifecycle,
    LifecycleRequest,
    RetentionEngine,
    RetentionPolicy,
    RetentionRequest,
)
from memory_worlds.maintenance_models import MaintenanceOutput
from memory_worlds.models import EpisodeInput, ScopeSelection
from memory_worlds.store import MemoryStore
from memory_worlds.thread_models import CreateThread
from memory_worlds.threads import ThreadService


@pytest.fixture
def env(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap()
    owner = store.authenticate(token)
    thread = ThreadService(store).create(
        owner, CreateThread(operation_id=uuid4(), world_id=world.id, name="Private work")
    )
    lifecycle = EpisodeLifecycle(store)
    return SimpleNamespace(**locals())


def episode(env, text="private evidence", *, source=None):
    return env.store.capture(
        env.owner,
        env.world.id,
        EpisodeInput(
            scope_id=env.thread.scope_id,
            source_id=source or str(uuid4()),
            content=text,
            occurred_at=datetime.now(UTC),
        ),
    ).episode


def derived(env, sources, *, count=1):
    results = []
    with env.store._world_db(env.world.id, env.thread.scope_id) as db:
        dependencies = env.store._table(db, "artifact_dependencies")
        artifacts = env.store._table(db, "context_artifacts")
        for _ in range(count):
            output = episode(env, "derived private evidence")
            for source in sources:
                db.execute(
                    dependencies.insert().values(
                        output_episode_id=output.id,
                        input_episode_id=source.id,
                        input_digest=source.digest,
                    )
                )
            db.execute(
                artifacts.insert().values(
                    id=str(uuid4()),
                    episode_id=output.id,
                    thread_id=str(env.thread.id),
                    job_id=str(uuid4()),
                    kind="summary",
                    source_refs="[]",
                )
            )
            results.append(output)
    return results[0] if count == 1 else results


def rows(env, name):
    with env.store._world_db(env.world.id, env.thread.scope_id) as db:
        table = env.store._table(db, name)
        return [dict(row) for row in db.execute(select(table)).mappings()]


def apply(env, *sources, action="erase", operation_id=None):
    request = LifecycleRequest(
        operation_id=operation_id or uuid4(),
        action=action,
        episode_ids=[source.id for source in sources],
    )
    return request, env.lifecycle.apply(env.owner, env.world.id, env.thread.scope_id, request)


def unavailable(env, output):
    for read in (
        lambda: env.store.get_episode(
            env.owner, env.world.id, output.id, scope_id=env.thread.scope_id
        ),
    ):
        with pytest.raises(NotFound):
            read()
    assert output.id not in {
        value.id
        for value in env.store.list_episodes(env.owner, env.world.id, scope_id=env.thread.scope_id)
    }
    assert output.id not in {
        value.id
        for value in env.store.search(
            env.owner, [env.world.id], "derived", scope_id=env.thread.scope_id
        )
    }
    assert output.id not in {
        value["id"]
        for value in env.store.snapshot(env.owner, env.world.id, scope_id=env.thread.scope_id)[
            "episodes"
        ]
    }
    assert output.id not in {
        value.id for value in env.store.pending_jobs(env.world.id, env.thread.scope_id)
    }
    assert output.id not in {
        value.id
        for value in env.store.projection_episodes(
            env.world.id, scope_id=env.thread.scope_id, include_unprojected=True
        )
    }


def test_retraction_hides_flattened_descendants_without_erasing_payloads(env):
    raw = episode(env)
    summary = derived(env, [raw])
    later = derived(env, [raw, summary])
    _, receipt = apply(env, summary, action="retract")
    unavailable(env, summary)
    unavailable(env, later)
    assert len(rows(env, "episodes")) == 3
    assert receipt["dependent_cleanup_state"] == "not-required"
    assert not receipt["erasure_complete"]


def test_projection_claim_cannot_return_a_hidden_derived_source(env):
    raw = episode(env)
    summary = derived(env, [raw])
    apply(env, raw, action="retract")
    lease = env.store.persistence.acquire_lease(
        env.store.projection_resource(env.world.id, env.thread.scope_id)
    )
    try:
        assert (
            env.store.mark_processing(env.world.id, summary.id, env.thread.scope_id, lease=lease)
            is None
        )
        with pytest.raises(NotFound):
            env.store.mark_projected(
                env.world.id,
                summary.id,
                "unexpected-generation",
                env.thread.scope_id,
                lease=lease,
            )
    finally:
        env.store.persistence.release_lease(lease)


@pytest.mark.parametrize("failure", ["missing", "digest", "tombstone"])
def test_source_read_fence_handles_missing_changed_and_removed_dependencies(env, failure):
    raw = episode(env)
    summary = derived(env, [raw])
    with env.store._world_db(env.world.id, env.thread.scope_id) as db:
        table = env.store._table(db, "episodes")
        if failure == "missing":
            jobs = env.store._table(db, "projection_jobs")
            db.execute(jobs.delete().where(jobs.c.episode_id == raw.id))
            db.execute(table.delete().where(table.c.id == raw.id))
        elif failure == "digest":
            db.execute(table.update().where(table.c.id == raw.id).values(digest="0" * 64))
        else:
            removed = env.store._table(db, "lifecycle_tombstones")
            db.execute(
                removed.insert().values(
                    source_id=raw.source_id,
                    episode_id=raw.id,
                    state="retracted",
                    owner_id=raw.actor_id,
                    changed_at=datetime.now(UTC).isoformat(),
                    operation_id=str(uuid4()),
                )
            )
    unavailable(env, summary)
    with pytest.raises(Conflict):
        env.store.capture(
            env.owner,
            env.world.id,
            EpisodeInput(**summary.model_dump(include=set(EpisodeInput.model_fields))),
        )


def test_erasure_removes_derived_payloads_jobs_metadata_and_prevents_replay(env):
    raw = episode(env)
    summary = derived(env, [raw])
    later = derived(env, [raw, summary])
    request, receipt = apply(env, raw)
    assert receipt["dependent_erased_records"] == 2
    assert receipt["pending_dependent_records"] == 0
    assert receipt["dependent_cleanup_state"] == "complete"
    assert receipt["erasure_complete"]
    for table in ("episodes", "projection_jobs", "context_artifacts"):
        assert rows(env, table) == []
    assert len(rows(env, "lifecycle_tombstones")) == 3
    assert "derived private evidence" not in str(rows(env, "lifecycle_operations"))
    assert env.lifecycle.apply(env.owner, env.world.id, env.thread.scope_id, request) == receipt
    for output in (summary, later):
        with pytest.raises(Conflict):
            env.store.capture(
                env.owner,
                env.world.id,
                EpisodeInput(**output.model_dump(include=set(EpisodeInput.model_fields))),
            )


def test_cleanup_failure_hides_outputs_and_blocks_completion_before_any_batch(env, monkeypatch):
    raw = episode(env)
    summary = derived(env, [raw])
    cleanup = env.lifecycle._cleanup_dependents

    def interrupted(*args):
        raise RuntimeError("synthetic interrupted cleanup")

    monkeypatch.setattr(env.lifecycle, "_cleanup_dependents", interrupted)
    operation = LifecycleRequest(operation_id=uuid4(), action="erase", episode_ids=[raw.id])
    with pytest.raises(RuntimeError):
        env.lifecycle.apply(env.owner, env.world.id, env.thread.scope_id, operation)
    unavailable(env, summary)
    receipt = env.lifecycle.receipt(
        env.owner, env.world.id, env.thread.scope_id, operation.operation_id
    )
    assert receipt["pending_dependent_records"] == 1
    assert not receipt["source_checkpointed"] and not receipt["erasure_complete"]
    monkeypatch.setattr(env.lifecycle, "_cleanup_dependents", cleanup)
    assert env.lifecycle.apply(env.owner, env.world.id, env.thread.scope_id, operation)[
        "erasure_complete"
    ]


def test_bounded_erase_retries_and_checkpoint_truth_after_later_batch(env, monkeypatch):
    raw = episode(env)
    outputs = derived(env, [raw], count=501)
    request, first = apply(env, raw)
    assert first["dependent_erased_records"] == 500
    assert first["pending_dependent_records"] == 1
    assert first["source_checkpointed"] and not first["erasure_complete"]
    for output in outputs:
        # One body remains stored, but none is eligible for retrieval.
        with pytest.raises(NotFound):
            env.store.get_episode(env.owner, env.world.id, output.id, scope_id=env.thread.scope_id)
    checkpoint = env.store.persistence.checkpoint_source

    def busy(*args):
        raise Unavailable("synthetic busy checkpoint")

    monkeypatch.setattr(env.store.persistence, "checkpoint_source", busy)
    with pytest.raises(Unavailable):
        env.lifecycle.apply(env.owner, env.world.id, env.thread.scope_id, request)
    pending = env.lifecycle.receipt(
        env.owner, env.world.id, env.thread.scope_id, request.operation_id
    )
    assert pending["pending_dependent_records"] == 0
    assert pending["dependent_erased_records"] == 501
    assert not pending["source_checkpointed"] and not pending["erasure_complete"]
    monkeypatch.setattr(env.store.persistence, "checkpoint_source", checkpoint)
    complete = env.lifecycle.apply(env.owner, env.world.id, env.thread.scope_id, request)
    assert complete["erasure_complete"] and complete["dependent_erased_records"] == 501


async def test_graph_purge_receipt_cannot_hide_pending_source_derivatives(env):
    raw = episode(env)
    derived(env, [raw], count=501)
    operation, receipt = apply(env, raw)
    assert not receipt["erasure_complete"]
    purged = await env.lifecycle.purge(
        env.owner, env.world.id, env.thread.scope_id, None, workers_drained=True
    )
    assert purged["graph_state"] == "purged"
    assert purged["dependent_cleanup_state"] == "pending"
    assert purged["pending_dependent_records"] == 1
    assert not env.lifecycle.receipt(
        env.owner, env.world.id, env.thread.scope_id, operation.operation_id
    )["erasure_complete"]


def test_erasing_artifact_itself_cleans_pointer_and_dependent_outputs(env):
    raw = episode(env)
    summary = derived(env, [raw])
    later = derived(env, [raw, summary])
    _, receipt = apply(env, summary)
    assert receipt["dependent_erased_records"] == 1
    assert receipt["erasure_complete"]
    assert {row["id"] for row in rows(env, "episodes")} == {raw.id}
    assert rows(env, "context_artifacts") == []
    unavailable(env, later)


def test_retention_stays_pending_and_resumes_its_frozen_dependent_cleanup(env):
    raw = episode(env)
    derived(env, [raw], count=501)
    with env.store._world_db(env.world.id, env.thread.scope_id) as db:
        episodes = env.store._table(db, "episodes")
        db.execute(
            episodes.update()
            .where(episodes.c.id == raw.id)
            .values(received_at="2025-01-01T00:00:00+00:00")
        )
    retention = RetentionEngine(env.store)
    request = RetentionRequest(
        operation_id=uuid4(),
        policy=RetentionPolicy(
            scopes=[ScopeSelection(world_id=env.world.id, scope_id=env.thread.scope_id)],
            source_before=datetime(2026, 1, 1, tzinfo=UTC),
            limit=1,
        ),
    )
    pending = retention.apply(env.owner, request)
    assert pending["state"] == "pending"
    assert pending["source_operations"][0]["pending_dependent_records"] == 1
    assert retention.receipt(env.owner, request.operation_id)["state"] == "pending"
    independent = episode(env, "New independent evidence")
    complete = retention.apply(env.owner, request)
    assert complete["state"] == "applied"
    assert complete["source_operations"][0]["pending_dependent_records"] == 0
    assert (
        complete["source_operations"][0]["operation_id"]
        == (pending["source_operations"][0]["operation_id"])
    )
    assert {row["id"] for row in rows(env, "episodes")} == {independent.id}
    assert retention.apply(env.owner, request) == complete


@pytest.mark.parametrize("operation", ["archive", "artifact"])
def test_archive_source_commit_finishes_before_maintenance_cut(env, monkeypatch, operation):
    archive = ContextArchive(env.store)
    request = ContextBundleInput(
        operation_id=uuid4(),
        expected_revision=0,
        entries=[
            ContextEvent(id="question", type="message", role="user", payload={"text": "Keep"})
        ],
    )
    paused, proceed, maintenance_started, entered, release = [Event() for _ in range(5)]
    lease = None
    if operation == "archive":
        capture = archive._capture

        def pause_after_first_chunk(*args):
            value = capture(*args)
            if not paused.is_set():
                paused.set()
                assert proceed.wait(5)
            return value

        monkeypatch.setattr(archive, "_capture", pause_after_first_chunk)

        def write():
            return archive.archive(env.owner, env.thread.id, request)
    else:
        bundle = archive.archive(env.owner, env.thread.id, request)
        frozen = archive.load_inputs(env.owner, env.thread.id, [bundle.bundle_id])
        job_id, artifact_id = uuid4(), uuid4()
        lease = env.store.persistence.acquire_lease(f"maintenance:{job_id}")
        frozen.update(
            thread_id=str(env.thread.id),
            bundle_ids=[str(bundle.bundle_id)],
            result_artifact_id=str(artifact_id),
            _lease=lease,
        )
        output = MaintenanceOutput(
            kind="summary",
            content="Selected summary",
            source_bundle_ids=[bundle.bundle_id],
        )
        fenced = env.store.persistence.fenced

        @contextmanager
        def pause_before_fence(selected):
            paused.set()
            assert proceed.wait(5)
            with fenced(selected) as db:
                yield db

        monkeypatch.setattr(env.store.persistence, "fenced", pause_before_fence)

        def write():
            return archive.write_artifact(env.owner, job_id, frozen, output)

    def maintenance():
        maintenance_started.set()
        with env.store.persistence.maintenance_guard():
            entered.set()
            assert release.wait(5)
            # This cut must include the complete source/control effect, never a
            # completed thread head pointing at an unfinished bundle or artifact.
            with env.store._control() as db:
                table = env.store._table(db, "context_bundles")
                assert db.execute(select(table.c.state)).scalar_one() == "ready"
            if operation == "artifact":
                assert len(rows(env, "context_artifacts")) == 1

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                writing = pool.submit(write)
                assert paused.wait(5)
                cutting = pool.submit(maintenance)
                assert maintenance_started.wait(5)
                try:
                    assert not entered.wait(0.1)
                finally:
                    proceed.set()
                writing.result(timeout=5)
                assert entered.wait(5)
                release.set()
                cutting.result(timeout=5)
            finally:
                proceed.set()
                release.set()
    finally:
        proceed.set()
        release.set()
        if lease is not None:
            env.store.persistence.release_lease(lease)
