"""Current storage invariants on native SQLite and an actual PostgreSQL server.

Set MEMORY_WORLDS_TEST_POSTGRES_DSN_FILE to a private file containing the test
database URL. Each case owns fresh authority schemas; no hosted credentials or
model calls are required. Injected graph adapters test the distributed state
machine, not Graphiti extraction quality.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text

from memory_worlds.errors import Conflict, NotFound, Unavailable
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.graph import ProjectionWorker
from memory_worlds.models import CreateScope, EpisodeInput
from memory_worlds.outbox import PublicationOutbox, RoutedPublication
from memory_worlds.persistence import LeaseLost
from memory_worlds.store import MemoryStore


@pytest.fixture(params=["sqlite", "postgresql"])
def authority(request, tmp_path):
    url = None
    if request.param == "postgresql":
        filename = os.environ.get("MEMORY_WORLDS_TEST_POSTGRES_DSN_FILE")
        if not filename:
            pytest.skip("an isolated PostgreSQL test database was not configured")
        url = Path(filename).read_text().strip()
    authority_id = uuid4().hex
    store = MemoryStore(tmp_path / "first", url, authority_id=authority_id)
    world, token = store.bootstrap("owner")
    owner = store.authenticate(token)
    peer = MemoryStore(
        store.data_dir if url is None else tmp_path / "peer", url, authority_id=store.authority_id
    )
    value = SimpleNamespace(
        store=store,
        peer=peer,
        owner=owner,
        world=world,
        url=url,
        authority_id=store.authority_id,
        tmp_path=tmp_path,
        authorization=lambda: {"Authorization": "Bearer " + token},
    )
    try:
        yield value
    finally:
        if url is not None:
            # The test role owns only disposable database schemas. Never select
            # schemas by a broad prefix; every removal is derived from this case.
            with store.persistence._engine.begin() as db:
                schemas = {store.persistence.schema("control"), store.persistence.schema("outbox")}
                for target in store.projection_worlds():
                    schemas.add(store.persistence.schema("source", str(target.scope_id)))
                from sqlalchemy.schema import DropSchema

                for schema in schemas:
                    db.execute(DropSchema(schema, cascade=True, if_exists=True))
        peer.persistence.close()
        store.persistence.close()


def episode(source_id="evidence", **changes):
    return EpisodeInput(
        source_id=source_id,
        content="The build uses a bounded queue.",
        occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
        **changes,
    )


def expire(store, lease):
    with store._control() as db:
        table = store._table(db, "worker_leases")
        db.execute(table.update().where(table.c.resource == lease.resource).values(expires_at=0))


def test_same_authority_instances_share_scopes_sources_and_atomic_replay(authority):
    e = authority
    scope = e.store.create_scope(
        e.owner, e.world.id, CreateScope(kind="agent-persistent", name="Owned memory")
    )
    first = episode("first", scope_id=scope.id)
    assert e.store.import_episodes(e.owner, e.world.id, scope.id, [first]) == {
        "imported": 1,
        "replayed": 0,
    }
    assert e.peer.validate_principal(e.owner) == e.owner
    assert e.peer.import_episodes(e.owner, e.world.id, scope.id, [first]) == {
        "imported": 0,
        "replayed": 1,
    }
    with pytest.raises(Conflict):
        e.peer.import_episodes(
            e.owner,
            e.world.id,
            scope.id,
            [
                episode("rolled-back", scope_id=scope.id),
                first.model_copy(update={"content": "Conflicting evidence"}),
            ],
        )
    snapshot = e.store.snapshot(e.owner, e.world.id, scope.id)
    assert [row["source_id"] for row in snapshot["episodes"]] == ["first"]
    assert len(snapshot["audit"]) == 1
    assert len(e.peer.pending_jobs(e.world.id, scope.id)) == 1
    if e.url:
        with e.store._world_db(e.world.id, scope.id) as db:
            assert db.dialect.name == "postgresql"
            assert e.store._table(db, "episodes").schema.startswith("mw_s_")
            # Routing is explicit and unaffected by a caller's search path.
            db.execute(text("SET LOCAL search_path TO pg_catalog"))
            table = e.store._table(db, "episodes")
            assert db.execute(select(table.c.source_id)).scalar_one() == "first"


def test_different_authority_cannot_resolve_another_authoritys_world(authority):
    e = authority
    other = MemoryStore(e.tmp_path / "other", e.url, authority_id=uuid4().hex)
    try:
        _, token = other.bootstrap("owner")
        with pytest.raises(NotFound):
            other.get_world(other.authenticate(token), e.world.id)
        assert other.authority_id != e.store.authority_id
    finally:
        if e.url:
            from sqlalchemy.schema import DropSchema

            schemas = {other.persistence.schema("control"), other.persistence.schema("outbox")}
            schemas.update(
                other.persistence.schema("source", str(w.scope_id))
                for w in other.projection_worlds()
            )
            with other.persistence._engine.begin() as db:
                for schema in schemas:
                    db.execute(DropSchema(schema, cascade=True))
        other.persistence.close()


def test_competing_claims_have_one_owner_and_old_fences_never_revive(authority):
    e = authority
    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = list(executor.map(lambda _: e.store.persistence.acquire_lease("race"), range(8)))
    owned = [lease for lease in leases if lease is not None]
    assert len(owned) == 1
    old = owned[0]
    expire(e.store, old)
    with pytest.raises(LeaseLost):
        e.peer.persistence.renew_lease(old)
    current = e.peer.persistence.acquire_lease("race")
    assert current.fence > old.fence and current.owner != old.owner
    assert not e.store.persistence.release_lease(old)
    with pytest.raises(LeaseLost):
        e.store.persistence.assert_lease(old)
    e.peer.persistence.assert_lease(current)


async def test_heartbeat_renews_using_database_time(authority):
    e = authority
    lease = e.store.persistence.acquire_lease("heartbeat", ttl=1)
    async with e.store.persistence.maintain_lease(lease, ttl=1) as assert_live:
        await asyncio.sleep(1.2)
        assert_live()
        assert e.peer.persistence.acquire_lease("heartbeat") is None
    assert e.store.persistence.release_lease(lease)


class ControlledGraph:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    def configuration_metadata(self):
        return {"backend": "injected-state-machine-fixture"}

    async def project(self, world, generation, source):
        self.calls.append((str(world.scope_id), generation, source.id))
        self.entered.set()
        await self.proceed.wait()

    async def search(self, *args, **kwargs):
        return []

    async def close(self):
        pass


async def test_two_replicas_never_project_or_read_a_live_write_concurrently(authority):
    e = authority
    e.store.capture(e.owner, e.world.id, episode())
    graph = ControlledGraph()
    graph.proceed.clear()
    first = ProjectionWorker(e.store, graph)
    second = ProjectionWorker(e.peer, graph)
    task = asyncio.create_task(first.run_once())
    await asyncio.wait_for(graph.entered.wait(), 3)
    assert await second.run_once() == 0
    with pytest.raises(Unavailable):
        await second.search([e.world], "queue")
    graph.proceed.set()
    assert await task == 1
    assert len(graph.calls) == 1
    assert await second.run_once() == 0
    assert e.peer.get_world(e.owner, e.world.id).projection_state == "ready"
    assert [r["state"] for r in e.store.generation_inventory()] == ["active"]


async def test_expired_external_write_cannot_activate_after_takeover(authority):
    e = authority
    source = e.store.capture(e.owner, e.world.id, episode()).episode
    old_graph = ControlledGraph()
    old_graph.proceed.clear()
    first = ProjectionWorker(e.store, old_graph)
    task = asyncio.create_task(first.run_once())
    await asyncio.wait_for(old_graph.entered.wait(), 3)
    resource = e.store.projection_resource(e.world.id, e.world.scope_id)
    with e.store._control() as db:
        leases = e.store._table(db, "worker_leases")
        db.execute(leases.update().where(leases.c.resource == resource).values(expires_at=0))
    replacement = ControlledGraph()
    second = ProjectionWorker(e.peer, replacement)
    assert await second.run_once() == 0
    assert e.peer.get_episode(e.owner, e.world.id, source.id).projection_state == "unknown"
    rebuilt = await second.rebuild(e.peer.get_world(e.owner, e.world.id))
    assert rebuilt["generation"] != old_graph.calls[0][1]
    old_graph.proceed.set()
    assert await task == 0
    current = e.store.get_world(e.owner, e.world.id)
    assert current.generation == rebuilt["generation"] and current.projection_state == "ready"
    inventory = e.store.generation_inventory()
    assert len(inventory) == 2 and sum(r["state"] == "active" for r in inventory) == 1
    assert sum(r["state"] == "failed" for r in inventory) == 1


async def test_abandoned_rebuild_is_inventoried_for_purge_without_degrading_ready_data(authority):
    e = authority
    e.store.capture(e.owner, e.world.id, episode())
    worker = ProjectionWorker(e.store, ControlledGraph())
    assert await worker.run_once() == 1
    active = e.store.get_world(e.owner, e.world.id).generation
    lease = e.store.persistence.acquire_lease(e.store.projection_resource(e.world.id))
    e.store.register_generation(e.world.id, "abandoned-build", lease=lease)
    expire(e.store, lease)
    assert await worker.run_once() == 0
    assert e.store.get_world(e.owner, e.world.id).generation == active
    inventory = {row["generation"]: row for row in e.store.generation_inventory()}
    assert inventory[active]["state"] == "active"
    assert inventory["abandoned-build"]["state"] == "failed"
    assert inventory["abandoned-build"]["retired_at"] is not None


async def test_lifecycle_blocks_stale_reads_replay_and_new_graph_writes_on_both_backends(authority):
    from memory_worlds.lifecycle import EpisodeLifecycle, LifecycleRequest

    e = authority
    value = episode()
    source = e.store.capture(e.owner, e.world.id, value).episode
    adapter = ControlledGraph()
    worker = ProjectionWorker(e.peer, adapter)
    assert await worker.run_once() == 1
    lifecycle = EpisodeLifecycle(e.store)
    request = LifecycleRequest(operation_id=uuid4(), action="erase", episode_ids=[source.id])
    receipt = await asyncio.to_thread(
        lifecycle.apply, e.owner, e.world.id, e.world.scope_id, request
    )
    assert receipt["source_state"] == "erased" and not receipt["erasure_complete"]
    assert e.peer.list_episodes(e.owner, e.world.id) == []
    assert e.peer.search(e.owner, [e.world.id], "queue") == []
    assert e.peer.snapshot(e.owner, e.world.id)["episodes"] == []
    with pytest.raises(Conflict):
        e.peer.capture(e.owner, e.world.id, value)
    before = list(adapter.calls)
    with pytest.raises(Unavailable, match="erasure is pending"):
        await worker.rebuild(e.peer.get_world(e.owner, e.world.id))
    assert adapter.calls == before

    class Purger:
        async def purge_generation(self, world, generation):
            assert world.id == e.world.id and world.scope_id == e.world.scope_id

    await lifecycle.purge(e.owner, e.world.id, e.world.scope_id, Purger(), workers_drained=True)
    final = lifecycle.receipt(e.owner, e.world.id, e.world.scope_id, request.operation_id)
    assert final["erasure_complete"]
    assert all(row["state"] == "purged" for row in e.store.generation_inventory())


async def test_pending_maintenance_fails_fast_then_drains_active_graph_operation(authority):
    e = authority
    e.store.capture(e.owner, e.world.id, episode())
    graph = ControlledGraph()
    graph.proceed.clear()
    worker = ProjectionWorker(e.store, graph)
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(graph.entered.wait(), 3)
    requested, entered, leave = threading.Event(), threading.Event(), threading.Event()

    def backup():
        requested.set()
        with e.peer.persistence.maintenance_guard():
            entered.set()
            assert leave.wait(5)

    thread = threading.Thread(target=backup)
    thread.start()
    assert requested.wait(1)
    await asyncio.sleep(0.05)
    began = time.monotonic()
    try:
        with pytest.raises(Unavailable):
            e.peer.get_world(e.owner, e.world.id)
        assert time.monotonic() - began < 0.5
        assert not entered.is_set()
        graph.proceed.set()
        assert await task == 1
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(Unavailable):
            e.store.get_world(e.owner, e.world.id)
    finally:
        graph.proceed.set()
        leave.set()
        await asyncio.to_thread(thread.join, 3)
    assert not thread.is_alive()
    assert e.store.get_world(e.owner, e.world.id).projection_state == "ready"


def test_outbox_exact_replay_and_fenced_stale_receipt(authority):
    e = authority
    first = PublicationOutbox(e.store.data_dir, e.authority_id, persistence=e.store.persistence)
    second = PublicationOutbox(e.peer.data_dir, e.authority_id, persistence=e.peer.persistence)
    mount = SimpleNamespace(
        id="remote", url="https://memory.example.test", authority_id=uuid4().hex
    )
    request = RoutedPublication(
        destination_mount_id="remote",
        world_id="team",
        source_id="chosen",
        content="Selected evidence",
        occurred_at=datetime.now(UTC),
    )
    queued = first.enqueue(e.owner, mount, request)
    claimed = first.claim()
    old = first.lease(claimed)
    assert second.claim() is None
    expire(e.store, old)
    with first.db() as db:
        table = e.store._table(db, "deliveries")
        db.execute(table.update().where(table.c.id == queued.id).values(lease_expires_at=0))
    recovered = second.claim()
    assert recovered["payload"] == claimed["payload"]
    assert recovered["lease_fence"] > claimed["lease_fence"]
    with pytest.raises(LeaseLost):
        first.finish(queued.id, "delivered", episode_id="stale", lease=old)
    second.finish(queued.id, "delivered", episode_id="actual", lease=second.lease(recovered))
    assert second.list(e.owner)[0].remote_episode_id == "actual"
    assert first.enqueue(e.owner, mount, request).id == queued.id


def test_postgres_sequence_restore_resumes_above_explicit_rows(authority):
    e = authority
    if not e.url:
        pytest.skip("PostgreSQL serial sequence reset is backend specific")
    first = e.store.capture(e.owner, e.world.id, episode()).episode
    with e.store._world_db(e.world.id) as db:
        table = e.store._table(db, "episodes")
        db.execute(table.update().where(table.c.id == first.id).values(sequence=9000))
        e.store.persistence.reset_sequences(db, table)
    assert e.peer.capture(e.owner, e.world.id, episode("next")).episode.sequence == 9001


def test_postgres_process_death_releases_only_after_server_clock_expiry(authority):
    e = authority
    if not e.url:
        pytest.skip("this case verifies an actual shared PostgreSQL process boundary")
    program = """
import json, os
from pathlib import Path
from memory_worlds.store import MemoryStore
store = MemoryStore(Path(os.environ['MW_TEST_DIR']),
    Path(os.environ['MEMORY_WORLDS_TEST_POSTGRES_DSN_FILE']).read_text().strip(),
    authority_id=os.environ['MW_TEST_AUTHORITY'])
lease = store.persistence.acquire_lease('dead-process', ttl=1)
print(json.dumps({'fence':lease.fence}), flush=True)
os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "MW_TEST_DIR": str(e.tmp_path / "process"),
            "MW_TEST_AUTHORITY": e.authority_id,
        },
    )
    old_fence = json.loads(result.stdout)["fence"]
    assert e.peer.persistence.acquire_lease("dead-process") is None
    time.sleep(1.1)
    lease = e.peer.persistence.acquire_lease("dead-process")
    assert lease is not None and lease.fence > old_fence


def test_two_postgres_api_replicas_share_live_grants_and_private_sources(authority):
    e = authority
    if not e.url:
        pytest.skip("SQLite deliberately has one daemon owner")
    from fastapi.testclient import TestClient

    from memory_worlds.api import create_app

    with (
        TestClient(create_app(e.store.data_dir, store=e.store)) as first,
        TestClient(create_app(e.peer.data_dir, store=e.peer)) as second,
    ):
        scope = first.post(
            f"/v1/worlds/{e.world.id}/scopes",
            json={"kind": "agent-persistent", "name": "Replica memory"},
            headers=e.authorization(),
        )
        assert scope.status_code == 201
        path = f"/v1/worlds/{e.world.id}/scopes/{scope.json()['id']}/episodes"
        captured = first.post(
            path, json=episode().model_dump(mode="json"), headers=e.authorization()
        )
        assert captured.status_code == 200
        read = second.get(path, headers=e.authorization())
        assert read.status_code == 200
        assert read.json()[0]["id"] == captured.json()["episode"]["id"]
        replay = second.post(
            path, json=episode().model_dump(mode="json"), headers=e.authorization()
        )
        assert replay.json()["replayed"]
        e.store.revoke_token(e.owner, e.owner.credential_id)
        assert second.get(path, headers=e.authorization()).status_code == 401


async def test_cancelled_async_maintenance_does_not_leak_an_exclusive_lock(authority):
    e = authority
    entered = asyncio.Event()
    proceed = asyncio.Event()

    async def reader():
        with e.store.persistence.operation_guard():
            entered.set()
            await proceed.wait()

    async def maintenance():
        async with e.peer.persistence.async_maintenance_guard():
            pytest.fail("cancelled maintenance must not run its body")

    reading = asyncio.create_task(reader())
    await asyncio.wait_for(entered.wait(), 2)
    waiting = asyncio.create_task(maintenance())
    await asyncio.sleep(0.05)
    waiting.cancel()
    await asyncio.sleep(0)
    proceed.set()
    await reading
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiting, 3)
    async with e.store.persistence.async_maintenance_guard():
        # Nested DAL calls inherit the exclusive authority context in the task.
        assert e.store.get_world(e.owner, e.world.id).id == e.world.id
    assert e.peer.get_world(e.owner, e.world.id).id == e.world.id


def test_current_sqlite_journals_and_scope_directories_are_private(authority):
    e = authority
    if e.url:
        pytest.skip("native local filesystem policy belongs to SQLite")
    scope = e.store.create_scope(
        e.owner, e.world.id, CreateScope(kind="agent-persistent", name="Private")
    )
    e.store.capture(e.owner, e.world.id, episode(scope_id=scope.id))
    for path in e.store.data_dir.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0, path
    with e.store._world_db(e.world.id, scope.id) as db:
        assert db.exec_driver_sql("PRAGMA secure_delete").scalar_one() == 1
    with pytest.raises(Conflict):
        e.store.persistence.checkpoint_source(e.world.id, scope.id)
    with e.store.persistence.maintenance_guard():
        e.store.persistence.checkpoint_source(e.world.id, scope.id)


@pytest.mark.parametrize(
    "backend,url",
    [
        ("postgres", ""),
        ("postgres", "   "),
        ("sqlite", "postgresql://invalid.example.test/db"),
        ("unknown", ""),
        ("", ""),
    ],
)
def test_explicit_storage_selector_rejects_conflicting_or_missing_config_before_files(
    tmp_path,
    monkeypatch,
    backend,
    url,
):
    target = tmp_path / "must-not-be-created"
    monkeypatch.setenv("MEMORY_WORLDS_STORAGE_BACKEND", backend)
    monkeypatch.setenv("MEMORY_WORLDS_DATABASE_URL", url)
    with pytest.raises(InvalidRequest):
        MemoryStore.database_url_from_env()
    with pytest.raises(InvalidRequest):
        MemoryStore.from_env(target)
    assert not target.exists()


def test_explicit_constructor_ignores_ambient_shared_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_WORLDS_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("MEMORY_WORLDS_DATABASE_URL", "postgresql://invalid.example.test/db")
    store = MemoryStore(tmp_path / "explicit-local")
    assert store.persistence.kind == "sqlite"
    store.persistence.close()


def test_native_environment_factory_selects_the_configured_current_backend(authority, monkeypatch):
    e = authority
    monkeypatch.setenv("MEMORY_WORLDS_STORAGE_BACKEND", "postgres" if e.url else "sqlite")
    monkeypatch.setenv("MEMORY_WORLDS_DATABASE_URL", e.url or "")
    monkeypatch.setenv("MEMORY_WORLDS_AUTHORITY_ID", e.authority_id)
    store = MemoryStore.from_env(e.store.data_dir if not e.url else e.tmp_path / "configured")
    try:
        assert store.authority_id == e.authority_id
        assert store.persistence.kind == e.store.persistence.kind
        assert store.get_world(e.owner, e.world.id).id == e.world.id
    finally:
        store.persistence.close()


@pytest.mark.parametrize("outer_graph_guard", [False, True])
def test_postgres_guards_cannot_exhaust_the_pool_before_sql_sessions(
    authority,
    outer_graph_guard,
):
    e = authority
    if not e.url:
        pytest.skip("SQLite has no shared PostgreSQL connection pool")
    e.store.persistence._engine.dispose()
    e.store.persistence._engine = create_engine(
        e.url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=0,
        hide_parameters=True,
    )
    barrier = threading.Barrier(2)

    def request(_):
        guard = e.store.persistence.operation_guard() if outer_graph_guard else nullcontext()
        with guard, e.store.persistence.control() as db:
            barrier.wait(timeout=2)
            table = e.store._table(db, "metadata")
            return db.execute(
                select(table.c.value).where(table.c.key == "authority_id")
            ).scalar_one()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(request, range(2))) == [e.authority_id, e.authority_id]


def test_nested_fenced_source_mutation_cannot_bypass_maintenance(authority):
    e = authority
    source = e.store.capture(e.owner, e.world.id, episode()).episode
    lease = e.store.persistence.acquire_lease(e.store.projection_resource(e.world.id))
    try:
        with e.store.persistence.maintenance_guard(), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(e.peer.mark_processing, e.world.id, source.id, lease=lease)
            with pytest.raises(Unavailable):
                future.result(timeout=2)
        assert e.store.get_episode(e.owner, e.world.id, source.id).projection_state == "pending"
    finally:
        e.store.persistence.release_lease(lease)


async def test_thread_archives_and_jobs_are_shared_and_fenced_across_authority_instances(authority):
    from memory_worlds.context_archive import ContextArchive
    from memory_worlds.context_models import ContextBundleInput
    from memory_worlds.maintenance_jobs import MaintenanceJobs
    from memory_worlds.maintenance_models import MaintenanceRequest
    from memory_worlds.maintenance_provider import WorkerProfile, WorkerProfiles
    from memory_worlds.thread_models import CreateThread
    from memory_worlds.threads import ThreadService

    e = authority
    first, peer = ContextArchive(e.store), ContextArchive(e.peer)
    thread = first.threads.create(
        e.owner,
        CreateThread(operation_id=uuid4(), world_id=e.world.id, name="Shared durable queue"),
    )
    request = ContextBundleInput(
        operation_id=uuid4(),
        expected_revision=0,
        entries=[
            {"id": "context", "type": "message", "payload": {"text": "A finding needs evidence"}}
        ],
    )
    receipt = first.archive(e.owner, thread.id, request)
    assert peer.archive(e.owner, thread.id, request) == receipt
    assert peer.bundle(e.owner, thread.id, receipt.bundle_id).entries == request.entries
    assert ThreadService(e.peer).get(e.owner, thread.id).revision == 1
    key = e.tmp_path / "model.key"
    key.write_text("synthetic-model-key")
    key.chmod(0o600)
    profiles = WorkerProfiles(
        profiles={
            "lightweight": WorkerProfile(
                endpoint="https://example.invalid/v1", model="luna", api_key_file=key
            )
        }
    )

    class Provider:
        calls = 0

        async def propose(self, profile, *, kind, text, bundle_ids):
            self.calls += 1
            await asyncio.sleep(0.01)
            return {
                "kind": "summary",
                "content": "Tentative derived finding",
                "source_bundle_ids": [str(value) for value in bundle_ids],
            }

    provider = Provider()

    def worker(store, archive):
        return MaintenanceJobs(
            store,
            input_loader=archive.load_inputs,
            output_writer=archive.write_artifact,
            result_reader=archive.artifact,
            authorize_job=archive.authorize_job,
            profiles=profiles,
            provider=provider,
        )

    jobs, other = worker(e.store, first), worker(e.peer, peer)
    submitted = await jobs.submit(
        e.owner,
        MaintenanceRequest(
            operation_id=uuid4(), thread_id=thread.id, bundle_ids=[receipt.bundle_id]
        ),
    )
    await asyncio.gather(jobs.run_once(submitted.id), other.run_once(submitted.id))
    finished = await other.status(e.owner, submitted.id)
    assert finished.state == "succeeded" and provider.calls == 1
    assert (
        peer.artifact(e.owner, finished.result_artifact_id).metadata["claim_status"] == "tentative"
    )
    await jobs.close()
    await other.close()
