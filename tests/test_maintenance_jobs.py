import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from memory_worlds.errors import Conflict, Forbidden, NotFound, Unauthorized
from memory_worlds.lifecycle import EpisodeLifecycle, LifecycleRequest
from memory_worlds.maintenance_jobs import MaintenanceJobs
from memory_worlds.maintenance_models import MaintenanceRequest
from memory_worlds.maintenance_provider import ProviderError, WorkerProfile, WorkerProfiles, digest
from memory_worlds.models import CreateToken, EpisodeInput
from memory_worlds.store import MemoryStore


class ControlledProvider:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        self.failure = None

    async def propose(self, profile, *, kind, text, bundle_ids):
        self.calls.append((profile.snapshot(), kind, text, bundle_ids))
        self.entered.set()
        await self.proceed.wait()
        if self.failure:
            raise self.failure
        return {
            "kind": "summary",
            "content": "A tentative result, awaiting evidence.",
            "source_bundle_ids": [str(item) for item in bundle_ids],
            "unresolved_questions": ["Which result proves this?"],
            "contradictions": [],
            "flags": [],
        }

    async def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    store = MemoryStore(tmp_path / "authority")
    world, token = store.bootstrap("owner")
    principal = store.authenticate(token)
    source = store.capture(
        principal,
        world.id,
        EpisodeInput(
            source_id="context-source",
            content="private-evidence-canary",
            occurred_at=datetime.now(UTC),
            project=False,
        ),
    ).episode
    thread_id, bundle_id = uuid4(), uuid4()
    artifacts = {}
    input_revision = ["first"]
    writer_calls = []
    lost_ack = [False]

    def loader(caller, selected_thread, bundle_ids):
        assert selected_thread == thread_id and bundle_ids == [bundle_id]
        live = store.get_episode(caller, world.id, source.id)
        return {
            "manifest": [{"id": str(bundle_id), "digest": digest(input_revision[0])}],
            "source_refs": [
                {
                    "world_id": world.id,
                    "scope_id": str(world.scope_id),
                    "episode_id": live.id,
                    "digest": live.digest,
                }
            ],
            "text": live.content,
        }

    def writer(caller, job_id, frozen, output):
        with store.persistence.fenced(frozen["_lease"]):
            loader(caller, thread_id, [bundle_id])
            writer_calls.append(job_id)
            identifier = frozen["result_artifact_id"]
            receipt = {"artifact_id": identifier, "content": output["content"]}
            if identifier in artifacts and artifacts[identifier] != receipt:
                raise Conflict("Artifact identity has different content")
            artifacts[identifier] = receipt
        if lost_ack[0]:
            lost_ack[0] = False
            raise OSError("private acknowledgement diagnostic")
        return receipt

    def reader(caller, artifact_id):
        loader(caller, thread_id, [bundle_id])
        if str(artifact_id) not in artifacts:
            raise NotFound("Artifact not found")
        return artifacts[str(artifact_id)]

    key = tmp_path / "key"
    key.write_text("protected-provider-secret")
    key.chmod(0o600)
    profiles = WorkerProfiles(
        profiles={
            "lightweight": WorkerProfile(
                endpoint="https://models.example.test/v1",
                model="operator-luna-model",
                api_key_file=key,
            )
        }
    )
    provider = ControlledProvider()

    def authorize(caller, selected_thread, operation):
        assert selected_thread == thread_id
        with store._control() as db:
            store._scope_authorized(
                db, caller, world.id, world.scope_id, "read" if operation == "read" else "write"
            )

    def service(**changes):
        return MaintenanceJobs(
            store,
            input_loader=loader,
            output_writer=writer,
            result_reader=reader,
            profiles=profiles,
            provider=provider,
            authorize_job=authorize,
            **changes,
        )

    value = SimpleNamespace(
        store=store,
        world=world,
        principal=principal,
        source=source,
        thread_id=thread_id,
        bundle_id=bundle_id,
        profiles=profiles,
        provider=provider,
        service=service,
        artifacts=artifacts,
        input_revision=input_revision,
        writer_calls=writer_calls,
        lost_ack=lost_ack,
        request=lambda **changes: MaintenanceRequest(
            operation_id=uuid4(), thread_id=thread_id, bundle_ids=[bundle_id], **changes
        ),
    )
    yield value
    store.persistence.close()


async def test_same_job_sync_async_restart_and_exact_replay(env):
    jobs = env.service()
    request = env.request()
    queued = await jobs.submit(env.principal, request)
    assert queued.state == "queued" and queued.id == request.operation_id
    assert (await jobs.wait(env.principal, queued.id, wait_seconds=0.01)).id == queued.id
    await env.service().run_once()
    resumed = env.service()
    receipt = await resumed.submit(env.principal, request, wait_seconds=0.1)
    assert receipt.state == "succeeded" and receipt.replayed
    assert receipt.result["artifact_id"] == str(receipt.result_artifact_id)
    assert len(env.provider.calls) == 1 and len(env.writer_calls) == 1
    with pytest.raises(Conflict):
        await resumed.submit(
            env.principal, request.model_copy(update={"kind": "assess-enrichment"})
        )


async def test_lost_artifact_ack_reconciles_immutable_result_without_model_replay(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.lost_ack[0] = True
    await jobs.run_once()
    assert (await jobs.status(env.principal, request.operation_id)).state == "queued"
    await env.service().run_once()
    receipt = await jobs.status(env.principal, request.operation_id)
    assert receipt.state == "succeeded" and not receipt.unresolved_effects
    assert len(env.provider.calls) == 1 and len(env.writer_calls) == 1


async def test_profiles_are_frozen_and_missing_profile_is_visible_blocked(env):
    jobs = env.service()
    missing = await jobs.submit(env.principal, env.request(profile="unconfigured"))
    assert missing.state == "blocked" and missing.error == "model_profile_unavailable"
    request = env.request()
    await jobs.submit(env.principal, request)
    env.profiles.profiles["lightweight"] = env.profiles.profiles["lightweight"].model_copy(
        update={"model": "different-model"}
    )
    await jobs.run_once()
    receipt = await jobs.status(env.principal, request.operation_id)
    assert receipt.state == "blocked" and receipt.error == "model_profile_changed"
    assert not env.provider.calls


async def test_source_deletion_before_dispatch_and_inference_result_blocks_effect(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    EpisodeLifecycle(env.store).apply(
        env.principal,
        env.world.id,
        env.world.scope_id,
        LifecycleRequest(operation_id=uuid4(), action="retract", episode_ids=[env.source.id]),
    )
    await jobs.run_once()
    assert not env.provider.calls and not env.writer_calls
    assert jobs._row(request.operation_id)["state"] == "stale"
    with pytest.raises(NotFound):
        await jobs.status(env.principal, request.operation_id)


async def test_revocation_while_queued_prevents_first_provider_call(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    with env.store._control() as db:
        credentials = env.store._table(db, "credentials")
        db.execute(
            credentials.update()
            .where(credentials.c.id == env.principal.credential_id)
            .values(revoked=1)
        )
    await jobs.run_once()
    assert jobs._row(request.operation_id)["state"] == "blocked"
    assert not env.provider.calls
    with pytest.raises(Unauthorized):
        await jobs.status(env.principal, request.operation_id)


async def test_changed_source_revision_cannot_be_silently_substituted(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.input_revision[0] = "second"
    await jobs.run_once()
    assert (await jobs.status(env.principal, request.operation_id)).state == "stale"
    assert not env.provider.calls


async def test_source_invalidation_during_inference_never_creates_result(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.proceed.clear()
    task = asyncio.create_task(jobs.run_once())
    await env.provider.entered.wait()
    env.input_revision[0] = "new source revision"
    env.provider.proceed.set()
    await task
    assert (await jobs.status(env.principal, request.operation_id)).state == "stale"
    assert not env.writer_calls


async def test_cancel_before_dispatch_and_during_provider_reports_unresolved(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.proceed.clear()
    task = asyncio.create_task(jobs.run_once())
    await env.provider.entered.wait()
    cancelled = await jobs.cancel(env.principal, request.operation_id)
    assert cancelled.state == "cancelled" and cancelled.usage_uncertain
    assert cancelled.unresolved_effects == ["provider:1"]
    env.provider.proceed.set()
    await task
    final = await jobs.status(env.principal, request.operation_id)
    assert final.state == "cancelled" and not final.unresolved_effects
    assert not env.writer_calls
    second = env.request()
    await jobs.submit(env.principal, second)
    await jobs.cancel(env.principal, second.operation_id)
    await jobs.run_once()
    assert len(env.provider.calls) == 1


async def test_two_workers_one_claim_and_lost_fence_cannot_commit(env):
    first, second = env.service(), env.service()
    request = env.request()
    await first.submit(env.principal, request)
    env.provider.proceed.clear()
    task = asyncio.create_task(first.run_once())
    await env.provider.entered.wait()
    assert await second.run_once() == 0
    with env.store._control() as db:
        leases = env.store._table(db, "worker_leases")
        db.execute(
            leases.update()
            .where(leases.c.resource == "maintenance:" + str(request.operation_id))
            .values(expires_at=0)
        )
    env.provider.proceed.set()
    await task
    assert not env.writer_calls
    with env.store._control() as db:
        table = env.store._table(db, "maintenance_jobs")
        db.execute(
            table.update().where(table.c.id == str(request.operation_id)).values(lease_expires_at=0)
        )
    await second.run_once()
    assert (await second.status(env.principal, request.operation_id)).state == "succeeded"
    assert len(env.writer_calls) == 1


async def test_unknown_provider_usage_and_daily_budget_are_not_silent_zero_cost(env):
    selected = env.profiles.profiles["lightweight"]
    env.profiles.profiles["lightweight"] = selected.model_copy(
        update={
            "daily_token_budget": selected.max_input_tokens + selected.max_output_tokens,
        }
    )
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.failure = ProviderError(
        "provider_unavailable", retryable=True, usage_uncertain=True
    )
    await jobs.run_once()
    receipt = await jobs.status(env.principal, request.operation_id)
    assert receipt.usage_uncertain and receipt.unresolved_effects == ["provider:1"]
    with env.store._control() as db:
        table = env.store._table(db, "maintenance_jobs")
        db.execute(table.update().values(next_attempt=0))
    await jobs.run_once()
    final = await jobs.status(env.principal, request.operation_id)
    assert final.state == "blocked" and final.error == "daily_token_budget_exceeded"
    assert len(env.provider.calls) == 1


async def test_control_rows_never_keep_context_output_or_provider_secret(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    await jobs.run_once()
    with env.store._control() as db:
        for name in ("maintenance_jobs", "maintenance_attempts", "maintenance_budgets"):
            table = env.store._table(db, name)
            wire = json.dumps([dict(row) for row in db.execute(select(table)).mappings()])
            assert "private-evidence-canary" not in wire
            assert "A tentative result" not in wire
            assert "protected-provider-secret" not in wire


async def test_profile_scope_policy_and_invented_output_are_rejected(env):
    env.profiles.profiles["lightweight"] = env.profiles.profiles["lightweight"].model_copy(
        update={"allowed_user_ids": ["somebody-else"]}
    )
    with pytest.raises(Forbidden):
        await env.service().submit(env.principal, env.request())


async def test_caller_wait_timeout_does_not_cancel_or_change_identity(env):
    jobs = env.service()
    request = env.request()
    first = await jobs.submit(env.principal, request, wait_seconds=0.01)
    assert first.state == "queued" and first.attempts == 0
    await jobs.run_once()
    same = await jobs.submit(env.principal, request, wait_seconds=0)
    assert same.id == first.id and same.state == "succeeded"


async def test_actual_heartbeat_retains_job_claim(env):
    jobs = env.service(lease_ttl=1)
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.proceed.clear()
    task = asyncio.create_task(jobs.run_once())
    await env.provider.entered.wait()
    await asyncio.sleep(1.2)
    assert env.store.persistence.acquire_lease("maintenance:" + str(request.operation_id)) is None
    env.provider.proceed.set()
    await task
    assert (await jobs.status(env.principal, request.operation_id)).state == "succeeded"


async def test_readonly_credential_cannot_submit_or_cancel_another_credentials_job(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    readonly = env.store.authenticate(
        env.store.issue_token(
            env.principal,
            CreateToken(
                user_id=env.principal.user_id,
                agent_id="readonly-agent",
                grants={env.world.id: "reader"},
            ),
        )
    )
    assert (await jobs.status(readonly, request.operation_id)).id == request.operation_id
    with pytest.raises(Forbidden):
        await jobs.submit(readonly, env.request())
    with pytest.raises(Forbidden):
        await jobs.cancel(readonly, request.operation_id)
    with pytest.raises(Forbidden):
        await jobs.submit(readonly, request)
    assert not env.provider.calls


async def test_live_write_revocation_before_dispatch_blocks_paid_inference(env):
    writer = env.store.authenticate(
        env.store.issue_token(
            env.principal,
            CreateToken(
                user_id=env.principal.user_id,
                agent_id="writer-agent",
                grants={env.world.id: "writer"},
            ),
        )
    )
    jobs = env.service()
    request = env.request()
    await jobs.submit(writer, request)
    with env.store._control() as db:
        grants = env.store._table(db, "grants")
        db.execute(
            grants.update()
            .where(grants.c.credential_id == writer.credential_id)
            .values(role="reader")
        )
    await jobs.run_once()
    assert (await jobs.status(writer, request.operation_id)).state == "blocked"
    assert not env.provider.calls


async def test_duplicate_concurrent_submissions_share_one_job_and_result(env):
    first, second = env.service(), env.service()
    request = env.request()
    receipts = await asyncio.gather(
        first.submit(env.principal, request), second.submit(env.principal, request)
    )
    assert receipts[0].id == receipts[1].id
    await asyncio.gather(first.run_once(), second.run_once())
    assert len(env.provider.calls) == len(env.writer_calls) == 1


async def test_cancelled_dispatched_inference_still_consumes_profile_concurrency(env):
    env.profiles.profiles["lightweight"] = env.profiles.profiles["lightweight"].model_copy(
        update={"max_concurrent_jobs": 1}
    )
    first, second = env.service(), env.service()
    request = env.request()
    await first.submit(env.principal, request)
    env.provider.proceed.clear()
    task = asyncio.create_task(first.run_once())
    await env.provider.entered.wait()
    await first.cancel(env.principal, request.operation_id)
    later = env.request()
    await second.submit(env.principal, later)
    assert await second.run_once() == 0
    env.provider.proceed.set()
    await task
    assert await second.run_once() == 1
    assert len(env.provider.calls) == 2


async def test_shutdown_after_dispatch_preserves_uncertainty_and_resumes_one_job(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.proceed.clear()
    task = asyncio.create_task(jobs.run_once())
    await env.provider.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipt = await jobs.status(env.principal, request.operation_id)
    assert receipt.state == "queued" and receipt.usage_uncertain
    env.provider.proceed.set()
    await env.service().run_once()
    resumed = await jobs.status(env.principal, request.operation_id)
    assert resumed.state == "succeeded" and resumed.attempts == 2
    # The first dispatched request's charge is still unknown after later success.
    assert resumed.unresolved_effects == ["provider:1"]


async def test_inference_retries_are_bounded_and_do_not_reuse_source_identity(env):
    jobs = env.service()
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.failure = ProviderError(
        "provider_unavailable", retryable=True, usage_uncertain=True
    )
    for _ in range(4):
        with env.store._control() as db:
            table = env.store._table(db, "maintenance_jobs")
            db.execute(table.update().values(next_attempt=0))
        await jobs.run_once()
    receipt = await jobs.status(env.principal, request.operation_id)
    assert receipt.state == "failed" and receipt.attempts == 2
    assert len(env.provider.calls) == 2
    assert receipt.unresolved_effects == ["provider:1", "provider:2"]
    assert not env.writer_calls


async def test_authority_maintenance_freezes_dispatched_job_semantics_but_not_lease_release(env):
    jobs = env.service(lease_ttl=1)
    request = env.request()
    await jobs.submit(env.principal, request)
    env.provider.proceed.clear()
    running = asyncio.create_task(jobs.run_once())
    await env.provider.entered.wait()
    # Provider I/O does not retain a shared maintenance lock. A backup can take
    # its exclusive cut, but the returning response must not change job state.
    with env.store.persistence.maintenance_guard():
        env.provider.proceed.set()
        await asyncio.wait_for(running, 2)
        with env.store._control() as db:
            attempts = env.store._table(db, "maintenance_attempts")
            assert db.execute(select(attempts.c.state)).scalar_one() == "unknown"
            table = env.store._table(db, "maintenance_jobs")
            row = db.execute(select(table)).mappings().one()
            assert row["state"] == "running" and row["effect_dispatched"] == 0
            assert row["result_artifact_id"] is None
            leases = env.store._table(db, "worker_leases")
            lease = (
                db.execute(
                    select(leases).where(
                        leases.c.resource == "maintenance:" + str(request.operation_id)
                    )
                )
                .mappings()
                .one()
            )
            # A release is operational bookkeeping, so it can still drain owners.
            assert lease["owner"] is None
        assert not env.writer_calls
    await asyncio.sleep(1.05)
    await env.service(lease_ttl=1).run_once()
    receipt = await jobs.status(env.principal, request.operation_id)
    assert receipt.state == "succeeded"
    assert receipt.unresolved_effects == ["provider:1"]
