"""Whole-authority recovery preserves job effects and uncertain provider charges."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from memory_worlds.context_archive import ContextArchive
from memory_worlds.context_models import ContextBundleInput
from memory_worlds.maintenance_jobs import MaintenanceJobs
from memory_worlds.maintenance_models import MaintenanceRequest
from memory_worlds.maintenance_provider import WorkerProfile, WorkerProfiles
from memory_worlds.recovery import backup_authority, restore_authority
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore
from memory_worlds.thread_models import CreateThread
from memory_worlds.threads import ThreadService


class ControlledProvider:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    async def propose(self, profile, *, kind, text, bundle_ids):
        self.calls += 1
        self.entered.set()
        await self.proceed.wait()
        return {
            "kind": "summary",
            "content": "The fixture decision remains tentative.",
            "source_bundle_ids": [str(item) for item in bundle_ids],
            "unresolved_questions": ["Which runout measurement supports it?"],
            "contradictions": [],
            "flags": [],
        }


class NoInference:
    async def propose(self, *args, **kwargs):
        pytest.fail("Recovery regenerated an already accepted model result")


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path / "original")
    world, token = store.bootstrap("alice")
    principal = store.authenticate(token)
    thread = ThreadService(store).create(
        principal,
        CreateThread(operation_id=uuid4(), world_id=world.id, name="Recoverable investigation"),
    )
    archive = ContextArchive(store)
    bundle = archive.archive(
        principal,
        thread.id,
        ContextBundleInput(
            operation_id=uuid4(),
            expected_revision=0,
            entries=[
                {
                    "id": "finding",
                    "type": "message",
                    "role": "user",
                    "payload": {"text": "Measure runout before selecting the fixture."},
                }
            ],
        ),
    )
    key = tmp_path / "model.key"
    key.write_text("synthetic-provider-key")
    key.chmod(0o600)
    profiles = WorkerProfiles(
        profiles={
            "lightweight": WorkerProfile(
                endpoint="https://models.example.test/v1",
                model="configured-luna",
                api_key_file=key,
            )
        }
    )
    backup_key = tmp_path / "backup.key"
    generate_key(backup_key)
    opened = [store]

    def jobs(selected_store, provider, *, writer=None):
        context = ContextArchive(selected_store)
        return MaintenanceJobs(
            selected_store,
            input_loader=context.load_inputs,
            output_writer=writer if writer is not None else context.write_artifact,
            result_reader=context.artifact,
            authorize_job=context.authorize_job,
            profiles=profiles,
            provider=provider,
        )

    def recover(source, name):
        backup = tmp_path / (name + ".mwauthority")
        backup_authority(source, source.authenticate(token), backup, backup_key)
        destination = tmp_path / name
        restore_authority(backup, backup_key, destination)
        recovered = MemoryStore(destination)
        opened.append(recovered)
        assert recovered.authenticate(token) == principal
        return recovered

    def budgets(selected_store):
        with selected_store._control() as db:
            table = selected_store._table(db, "maintenance_budgets")
            return [dict(row) for row in db.execute(select(table)).mappings()]

    value = SimpleNamespace(
        store=store,
        archive=archive,
        principal=principal,
        thread=thread,
        bundle=bundle,
        jobs=jobs,
        recover=recover,
        budgets=budgets,
        request=MaintenanceRequest(
            operation_id=uuid4(), thread_id=thread.id, bundle_ids=[bundle.bundle_id]
        ),
    )
    yield value
    for selected_store in opened:
        selected_store.persistence.close()


async def test_restore_requeues_inflight_job_and_retains_unknown_attempt_and_reservation(memory):
    provider = ControlledProvider()
    provider.proceed.clear()
    original = memory.jobs(memory.store, provider)
    await original.submit(memory.principal, memory.request)
    running = asyncio.create_task(original.run_once())
    try:
        await provider.entered.wait()
        # The model is in flight without a source/SQL guard. Capture the actual
        # running row and unknown attempt in a complete encrypted authority backup.
        frozen_budget = memory.budgets(memory.store)
        restored = memory.recover(memory.store, "recovered-inflight")
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    resumed_provider = ControlledProvider()
    resumed = memory.jobs(restored, resumed_provider)
    row = resumed._row(memory.request.operation_id)
    assert row["state"] == "queued" and row["next_attempt"] == 0
    assert row["lease_owner"] is None and row["lease_fence"] is None
    assert row["lease_expires_at"] is None and row["attempts"] == 1
    assert row["effect_dispatched"] == 0 and row["result_artifact_id"] is None
    before = await resumed.status(memory.principal, memory.request.operation_id)
    assert before.usage_uncertain and before.unresolved_effects == ["provider:1"]
    assert memory.budgets(restored) == frozen_budget

    # No wait for the old worker's 30-second lease: recovery discarded ownership.
    assert await resumed.run_once() == 1
    final = await resumed.status(memory.principal, memory.request.operation_id)
    assert final.state == "succeeded" and final.attempts == 2
    assert final.unresolved_effects == ["provider:1"]
    assert resumed_provider.calls == 1 and provider.calls == 1
    assert final.result["content"] == "The fixture decision remains tentative."
    assert memory.budgets(restored)[0]["reserved_tokens"] == 2 * frozen_budget[0]["reserved_tokens"]


async def test_restore_reconciles_accepted_artifact_without_repeating_model_or_source_write(memory):
    provider = ControlledProvider()
    committed = asyncio.Event()
    acknowledgement = asyncio.Event()

    async def lose_ack(principal, job_id, frozen_input, output):
        result = memory.archive.write_artifact(principal, job_id, frozen_input, output)
        committed.set()
        # Simulate the independent source journal commit succeeding immediately
        # before this worker dies without recording its control-plane receipt.
        await acknowledgement.wait()
        return result

    original = memory.jobs(memory.store, provider, writer=lose_ack)
    await original.submit(memory.principal, memory.request)
    running = asyncio.create_task(original.run_once())
    try:
        await committed.wait()
        frozen_budget = memory.budgets(memory.store)
        restored = memory.recover(memory.store, "recovered-artifact")
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    resumed = memory.jobs(restored, NoInference())
    before = await resumed.status(memory.principal, memory.request.operation_id)
    assert before.state == "queued" and before.attempts == 1
    assert not before.usage_uncertain and before.unresolved_effects == ["artifact"]
    assert resumed._row(before.id)["effect_dispatched"] == 1
    with restored._world_db(memory.thread.world_id, memory.thread.scope_id) as db:
        artifacts = restored._table(db, "context_artifacts")
        saved = dict(db.execute(select(artifacts)).mappings().one())
    assert await resumed.run_once() == 1
    final = await resumed.status(memory.principal, memory.request.operation_id)
    assert final.state == "succeeded" and final.attempts == 1
    assert str(final.result_artifact_id) == saved["id"]
    assert final.result["content"] == "The fixture decision remains tentative."
    assert not final.unresolved_effects and provider.calls == 1
    assert memory.budgets(restored) == frozen_budget
    with restored._world_db(memory.thread.world_id, memory.thread.scope_id) as db:
        assert [dict(row) for row in db.execute(select(artifacts)).mappings()] == [saved]

    # An acknowledged result reference must also survive another full restore.
    acknowledged = memory.recover(restored, "recovered-acknowledged")
    completed = memory.jobs(acknowledged, NoInference())
    receipt = await completed.status(memory.principal, memory.request.operation_id)
    assert receipt.state == "succeeded" and receipt.result_artifact_id == final.result_artifact_id
    assert await completed.run_once() == 0
