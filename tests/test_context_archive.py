"""Real journal archive recovery, failed receipts and derived-source boundaries."""

import json
import random
import string
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from memory_worlds.context_archive import ContextArchive
from memory_worlds.context_models import ContextBundleInput, ContextSourceRef
from memory_worlds.errors import Conflict, Forbidden, NotFound
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.lifecycle import EpisodeLifecycle, LifecycleRequest
from memory_worlds.maintenance_models import MaintenanceOutput
from memory_worlds.models import CreateToken, EpisodeInput, UpdateScopeGrant
from memory_worlds.persistence import LeaseLost
from memory_worlds.store import MemoryStore
from memory_worlds.thread_models import CreateThread
from memory_worlds.threads import ThreadService


@pytest.fixture
def env(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap("alice")
    admin = store.authenticate(token)
    owner = store.authenticate(
        store.issue_token(
            admin, CreateToken(user_id="alice", agent_id="research", grants={world.id: "manager"})
        )
    )
    threads = ThreadService(store)
    thread = threads.create(
        owner, CreateThread(operation_id=uuid4(), world_id=world.id, name="Private research")
    )
    archive = ContextArchive(store, threads)
    return SimpleNamespace(**locals())


def bundle(**values):
    return ContextBundleInput.model_validate(
        {
            "operation_id": str(uuid4()),
            "expected_revision": 0,
            "entries": [
                {
                    "id": "message",
                    "type": "message",
                    "role": "user",
                    "payload": {"text": "Retain exact source detail"},
                }
            ],
            **values,
        }
    )


def control_rows(env, name):
    with env.store._control() as db:
        return [dict(row) for row in db.execute(select(env.store._table(db, name))).mappings()]


def source_rows(env, name):
    with env.store._world_db(env.world.id, env.thread.scope_id) as db:
        return [dict(row) for row in db.execute(select(env.store._table(db, name))).mappings()]


def test_large_unicode_tool_archive_recovers_exact_bytes_across_restart(env):
    rng = random.Random(77)
    text = "測定🛠️\n" + "".join(rng.choices(string.ascii_letters + string.digits, k=190_000))
    request = bundle(
        entries=[
            {"id": "m", "type": "message", "role": "user", "payload": {"text": text}},
            {
                "id": "call",
                "parent_id": "m",
                "type": "tool-call",
                "payload": {"call_id": "c1", "arguments": {"path": "測定.json"}},
            },
            {
                "id": "result",
                "parent_id": "call",
                "type": "tool-result",
                "payload": {"call_id": "c1", "result": {"samples": [0, 0.125, None, True]}},
            },
        ],
        omissions=[{"entry_id": "attachment", "reason": "unavailable"}],
    )
    receipt = env.archive.archive(env.owner, env.thread.id, request)
    assert receipt.coverage == "partial" and len(receipt.source_refs) > 3
    reopened = ContextArchive(MemoryStore(env.store.data_dir))
    recovered = reopened.bundle(env.owner, env.thread.id, receipt.bundle_id)
    assert recovered.entries == request.entries
    assert recovered.omissions == request.omissions
    assert recovered.uncompressed_bytes > 190_000
    assert recovered.stored_bytes > 64_000
    assert reopened.archive(env.owner, env.thread.id, request) == receipt
    assert len(source_rows(env, "episodes")) == len(receipt.source_refs)
    assert source_rows(env, "projection_jobs") == []
    assert text not in json.dumps(control_rows(env, "context_bundles"))


def test_archive_reconciles_lost_append_ack_with_frozen_original_timestamp(env, monkeypatch):
    request = bundle()
    append = env.threads.append

    def lose_ack(*args, **kwargs):
        append(*args, **kwargs)
        raise OSError("lost durable append acknowledgement")

    monkeypatch.setattr(env.threads, "append", lose_ack)
    with pytest.raises(OSError):
        env.archive.archive(env.owner, env.thread.id, request)
    assert control_rows(env, "context_bundles")[0]["state"] == "preparing"
    assert env.threads.get(env.owner, env.thread.id).revision == 1
    with pytest.raises(Conflict, match="durability receipt"):
        env.archive.bundle(env.owner, env.thread.id, request.operation_id)
    original_sources = source_rows(env, "episodes")
    monkeypatch.setattr(env.threads, "append", append)
    receipt = env.archive.archive(env.owner, env.thread.id, request)
    assert receipt.revision == 1
    assert source_rows(env, "episodes") == original_sources
    with pytest.raises(Conflict, match="different input"):
        env.archive.archive(
            env.owner,
            env.thread.id,
            ContextBundleInput.model_validate(
                {
                    **request.model_dump(),
                    "omissions": [{"entry_id": "changed", "reason": "excluded"}],
                }
            ),
        )


def test_concurrent_archive_replay_has_one_revision_and_one_source_set(env):
    request = bundle()
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda _: env.archive.archive(env.owner, env.thread.id, request), range(2))
        )
    assert receipts[0] == receipts[1]
    assert env.threads.get(env.owner, env.thread.id).revision == 1
    assert len(source_rows(env, "episodes")) == len(receipts[0].source_refs)


def test_stale_revision_never_returns_archive_ready_receipt(env):
    env.archive.archive(env.owner, env.thread.id, bundle())
    stale = bundle()
    with pytest.raises(Conflict):
        env.archive.archive(env.owner, env.thread.id, stale)
    assert {row["state"] for row in control_rows(env, "context_bundles")} == {"preparing", "ready"}
    with pytest.raises(Conflict, match="durability receipt"):
        env.archive.bundle(env.owner, env.thread.id, stale.operation_id)


def test_worker_input_expansion_is_bounded_before_loading_more_bundles(env, monkeypatch):
    entries = [{"id": "m", "type": "message", "payload": {"text": "a" * 700_000}}]
    first = env.archive.archive(env.owner, env.thread.id, bundle(entries=entries))
    second = env.archive.archive(
        env.owner, env.thread.id, bundle(entries=entries, expected_revision=1)
    )
    recover = env.archive.bundle
    reads = []

    def selected(*args):
        reads.append(args[-1])
        return recover(*args)

    monkeypatch.setattr(env.archive, "bundle", selected)
    with pytest.raises(InvalidRequest, match="1 MiB"):
        env.archive.load_inputs(
            env.owner, env.thread.id, [first.bundle_id, second.bundle_id, uuid4()]
        )
    assert reads == [first.bundle_id, second.bundle_id]


@pytest.mark.parametrize(
    "events",
    [
        [{"id": "r", "type": "tool-result", "payload": {"call_id": "unknown"}}],
        [{"id": "r", "type": "tool-result", "payload": {"call_id": {"invalid": True}}}],
        [
            {"id": "c", "type": "tool-call", "payload": {"call_id": "same"}},
            {"id": "d", "type": "tool-call", "payload": {"call_id": "same"}},
        ],
        [
            {"id": "a", "type": "custom", "payload": {}},
            {"id": "a", "type": "custom", "payload": {}},
        ],
        [{"id": "a", "type": "custom", "payload": {"value": float("nan")}}],
    ],
)
def test_malformed_archive_events_fail_typed_validation(events):
    with pytest.raises(ValidationError):
        bundle(entries=events)


def test_archive_with_source_provenance_and_derived_summary_retract_together(env):
    source = env.store.capture(
        env.owner,
        env.world.id,
        EpisodeInput(
            scope_id=env.thread.scope_id,
            source_id="evidence",
            content="retractable evidence",
            occurred_at=datetime.now(UTC),
            project=False,
        ),
    ).episode
    request = bundle(
        source_refs=[
            ContextSourceRef(
                world_id=env.world.id,
                scope_id=env.thread.scope_id,
                episode_id=source.id,
                digest=source.digest,
            )
        ]
    )
    receipt = env.archive.archive(env.owner, env.thread.id, request)
    derived_bundle = env.archive.archive(
        env.owner,
        env.thread.id,
        bundle(
            expected_revision=1,
            source_refs=[receipt.source_refs[-1]],
        ),
    )
    preflight = env.archive.load_inputs(env.owner, env.thread.id, [derived_bundle.bundle_id])
    assert source.id in {ref["episode_id"] for ref in preflight["source_refs"]}
    frozen = env.archive.load_inputs(env.owner, env.thread.id, [receipt.bundle_id])
    job_id, artifact_id = uuid4(), uuid4()
    lease = env.store.persistence.acquire_lease(f"maintenance:{job_id}")
    frozen.update(
        thread_id=str(env.thread.id),
        bundle_ids=[str(receipt.bundle_id)],
        result_artifact_id=str(artifact_id),
        _lease=lease,
    )
    output = MaintenanceOutput(
        kind="summary",
        content="Tentative summary of evidence",
        source_bundle_ids=[receipt.bundle_id],
    )
    env.archive.write_artifact(env.owner, job_id, frozen, output)
    assert env.archive.artifact(env.owner, artifact_id).metadata["claim_status"] == "tentative"
    deps = source_rows(env, "artifact_dependencies")
    assert len([row for row in deps if row["input_episode_id"] == source.id]) >= 3
    EpisodeLifecycle(env.store).apply(
        env.owner,
        env.world.id,
        env.thread.scope_id,
        LifecycleRequest(operation_id=uuid4(), action="retract", episode_ids=[source.id]),
    )
    with pytest.raises(NotFound):
        env.archive.bundle(env.owner, env.thread.id, receipt.bundle_id)
    with pytest.raises(NotFound):
        env.archive.artifact(env.owner, artifact_id)
    assert env.store.list_episodes(env.owner, env.world.id, scope_id=env.thread.scope_id) == []
    assert (
        env.store.snapshot(env.owner, env.world.id, scope_id=env.thread.scope_id)["episodes"] == []
    )
    env.store.persistence.release_lease(lease)


def test_artifact_commit_rejects_stale_worker_fence(env):
    receipt = env.archive.archive(env.owner, env.thread.id, bundle())
    frozen = env.archive.load_inputs(env.owner, env.thread.id, [receipt.bundle_id])
    job_id, artifact_id = uuid4(), uuid4()
    lease = env.store.persistence.acquire_lease(f"maintenance:{job_id}")
    env.store.persistence.release_lease(lease)
    successor = env.store.persistence.acquire_lease(f"maintenance:{job_id}")
    frozen.update(
        thread_id=str(env.thread.id),
        bundle_ids=[str(receipt.bundle_id)],
        result_artifact_id=str(artifact_id),
        _lease=lease,
    )
    output = MaintenanceOutput(
        kind="summary", content="Stale worker output", source_bundle_ids=[receipt.bundle_id]
    )
    with pytest.raises(LeaseLost):
        env.archive.write_artifact(env.owner, job_id, frozen, output)
    assert source_rows(env, "context_artifacts") == []
    assert len(source_rows(env, "episodes")) == len(receipt.source_refs)
    env.store.persistence.release_lease(successor)


def test_artifact_rechecks_grant_inside_commit_fence(env, monkeypatch):
    receipt = env.archive.archive(env.owner, env.thread.id, bundle())
    frozen = env.archive.load_inputs(env.owner, env.thread.id, [receipt.bundle_id])
    job_id, artifact_id = uuid4(), uuid4()
    lease = env.store.persistence.acquire_lease(f"maintenance:{job_id}")
    frozen.update(
        thread_id=str(env.thread.id),
        bundle_ids=[str(receipt.bundle_id)],
        result_artifact_id=str(artifact_id),
        _lease=lease,
    )
    output = MaintenanceOutput(
        kind="summary",
        content="Worker completed after grant revocation",
        source_bundle_ids=[receipt.bundle_id],
    )
    fenced = env.store.persistence.fenced

    @contextmanager
    def revoke_before_commit(selected_lease):
        env.store.set_scope_grant(
            env.owner,
            env.world.id,
            env.thread.scope_id,
            UpdateScopeGrant(credential_id=env.owner.credential_id, operations=["read"]),
        )
        with fenced(selected_lease) as db:
            yield db

    monkeypatch.setattr(env.store.persistence, "fenced", revoke_before_commit)
    with pytest.raises(Forbidden):
        env.archive.write_artifact(env.owner, job_id, frozen, output)
    assert source_rows(env, "context_artifacts") == []
    assert len(source_rows(env, "episodes")) == len(receipt.source_refs)
    monkeypatch.setattr(env.store.persistence, "fenced", fenced)
    env.store.persistence.release_lease(lease)
