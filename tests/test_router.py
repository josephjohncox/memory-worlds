import asyncio
import json
import socket
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from memory_worlds.api import create_app
from memory_worlds.errors import Conflict, Forbidden, Unauthorized
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.models import CreateToken, CreateWorld, Role, WorldKind
from memory_worlds.mounts import Mount, MountConfig, MountRole
from memory_worlds.outbox import RoutedPublication, RouterSearch
from memory_worlds.router import RemoteFailure, WorldRouter
from memory_worlds.store import MemoryStore


@pytest.fixture
def federation(tmp_path):
    local = MemoryStore(tmp_path / "local")
    _, admin_token = local.bootstrap("operator")
    admin = local.authenticate(admin_token)

    def agent(user="alice", name="agent"):
        return local.authenticate(
            local.issue_token(admin, CreateToken(user_id=user, agent_id=name))
        )

    alice, bob = agent(), agent("bob")
    remote = MemoryStore(tmp_path / "remote")
    private, remote_admin_token = remote.bootstrap("remote-owner")
    remote_admin = remote.authenticate(remote_admin_token)
    team = remote.create_world(remote_admin, CreateWorld(name="Team", kind=WorldKind.TEAM))
    remote_token = remote.issue_token(
        remote_admin,
        CreateToken(user_id="service", agent_id="router", grants={team.id: Role.WRITER}),
    )
    token_file = tmp_path / "remote-token"
    token_file.write_text(remote_token)
    token_file.chmod(0o600)
    mount = Mount(
        id="work",
        name="Work",
        url="https://work.example.test",
        authority_id=remote.authority_id,
        world_ids=[team.id],
        token_file=token_file,
        user_grants={alice.user_id: MountRole.PUBLISHER},
        publish=True,
    )
    config = MountConfig(mounts=[mount])
    app = create_app(remote.data_dir)
    transport = httpx.ASGITransport(app=app)
    router = WorldRouter(local, config, transport=transport)
    return SimpleNamespace(**locals())


def publication(env, source="note", **changes):
    data = dict(
        destination_mount_id="work",
        world_id=env.team.id,
        source_id=source,
        content="Approved team evidence",
        occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    data.update(changes)
    return RoutedPublication(**data)


class InterceptTransport(httpx.AsyncBaseTransport):
    def __init__(self, wrapped, intercept):
        self.wrapped, self.intercept = wrapped, intercept
        self.paths = []

    async def handle_async_request(self, request):
        self.paths.append(request.url.path)
        response = await self.wrapped.handle_async_request(request)
        await response.aread()
        return self.intercept(request, response)


async def test_publication_is_explicit_durable_idempotent_and_search_has_origin(federation):
    e = federation
    request = publication(e)
    queued = e.router.publish(e.alice, request)
    assert queued.state == "pending"
    assert queued.agent_id == e.alice.agent_id
    assert not e.remote.list_episodes(e.remote_admin, e.team.id)
    assert e.router.publish(e.alice, request).id == queued.id
    with pytest.raises(Conflict):
        e.router.publish(e.alice, publication(e, content="Changed input"))
    assert await e.router.run_once() == 1
    delivered = e.router.deliveries(e.alice)[0]
    assert delivered.state == "delivered"
    assert delivered.remote_episode_id
    rows = e.remote.list_episodes(e.remote_admin, e.team.id)
    assert len(rows) == 1
    assert rows[0].source_type == "publication"
    assert rows[0].source_ref is None and rows[0].metadata == {}
    assert rows[0].source_id == f"publication:{e.local.authority_id}:{queued.id}"
    found = await e.router.search(e.alice, RouterSearch(mount_ids=["work"], query="evidence"))
    assert found["complete"] and not found["failures"]
    assert found["results"][0]["authority_id"] == e.remote.authority_id
    assert found["results"][0]["mount_id"] == "work"
    assert found["results"][0]["record_type"] == "source"
    assert found["results"][0]["id"] == delivered.remote_episode_id
    assert await e.router.run_once() == 0
    assert e.router.publish(e.alice, request).state == "delivered"


async def test_mount_authorization_is_complete_before_any_remote_request(federation):
    e = federation
    count = []

    def unused(request):
        count.append(request)
        return httpx.Response(200, json=[])

    e.router.transport = httpx.MockTransport(unused)
    other = e.mount.model_copy(update={"id": "other", "user_grants": {}})
    e.router.mounts["other"] = other
    with pytest.raises(Forbidden):
        await e.router.search(e.alice, RouterSearch(mount_ids=["work", "other"], query="private"))
    with pytest.raises(Forbidden):
        e.router.publish(e.bob, publication(e))
    assert count == []
    assert e.router.list_mounts(e.bob) == []
    view = e.router.list_mounts(e.alice)[0].model_dump(mode="json")
    assert "token_file" not in view and "user_grants" not in view
    e.router.publish(e.alice, publication(e))
    assert e.router.deliveries(e.bob) == []
    assert len(e.router.deliveries(e.admin)) == 1


async def test_private_remote_worlds_cannot_be_federated_or_receive_publications(federation):
    e = federation
    e.token_file.write_text(e.remote_admin_token)
    e.mount.world_ids = [e.private.id]
    paths = []

    def record(request, response):
        paths.append(request.url.path)
        return response

    e.router.transport = InterceptTransport(e.transport, record)
    result = await e.router.search(e.alice, RouterSearch(mount_ids=["work"], query="secret"))
    assert result == {
        "results": [],
        "failures": [{"mount_id": "work", "error": "remote_world_is_private"}],
        "complete": False,
    }
    queued = e.router.publish(e.alice, publication(e, world_id=e.private.id))
    await e.router.run_once()
    assert e.router.deliveries(e.alice)[0].id == queued.id
    assert e.router.deliveries(e.alice)[0].error == "remote_world_is_private"
    assert all(path == "/v1/worlds" for path in paths)


async def test_outage_is_durable_backed_off_and_explicit_retry_preserves_identity(federation):
    e = federation

    def outage(request):
        raise httpx.ConnectError("must not expose credential or private content", request=request)

    e.router.transport = httpx.MockTransport(outage)
    queued = e.router.publish(e.alice, publication(e))
    await e.router.run_once()
    pending = e.router.deliveries(e.alice)[0]
    assert pending.state == "pending" and pending.error == "remote_unavailable"
    assert pending.attempts == 1
    assert await e.router.run_once() == 0
    restarted = WorldRouter(MemoryStore(e.local.data_dir), e.config, transport=e.transport)
    assert restarted.deliveries(e.alice)[0].id == queued.id
    restarted.retry(e.alice, queued.id)
    await restarted.run_once()
    final = restarted.deliveries(e.alice)[0]
    assert final.state == "delivered" and final.attempts == 2
    assert len(e.remote.list_episodes(e.remote_admin, e.team.id)) == 1


async def test_lost_acknowledgment_retries_without_duplicate_remote_evidence(federation):
    e = federation
    lost = []

    def lose_once(request, response):
        if request.url.path.endswith("/publications") and not lost:
            lost.append(True)
            raise httpx.ReadError("lost acknowledgement", request=request)
        return response

    e.router.transport = InterceptTransport(e.transport, lose_once)
    queued = e.router.publish(e.alice, publication(e))
    await e.router.run_once()
    assert e.router.deliveries(e.alice)[0].state == "pending"
    assert len(e.remote.list_episodes(e.remote_admin, e.team.id)) == 1
    restarted = WorldRouter(e.local, e.config, transport=e.transport)
    restarted.retry(e.alice, queued.id)
    await restarted.run_once()
    assert restarted.deliveries(e.alice)[0].state == "delivered"
    assert len(e.remote.list_episodes(e.remote_admin, e.team.id)) == 1


async def test_restarts_recover_interrupted_processing_jobs(federation):
    e = federation
    e.router.publish(e.alice, publication(e))
    assert e.router.outbox.claim()["state"] == "pending"
    restarted = WorldRouter(e.local, e.config, transport=e.transport)
    await restarted.run_once()
    final = restarted.deliveries(e.alice)[0]
    assert final.state == "delivered" and final.attempts == 2


async def test_revocation_during_metadata_request_stops_publication_dispatch(federation):
    e = federation

    def revoke(request, response):
        if request.url.path == "/v1/worlds":
            e.local.revoke_token(e.admin, e.alice.credential_id)
        return response

    intercept = InterceptTransport(e.transport, revoke)
    e.router.transport = intercept
    e.router.publish(e.alice, publication(e))
    await e.router.run_once()
    row = e.router.deliveries(e.admin)[0]
    assert row.state == "blocked"
    assert row.error == "local_authorization_or_configuration_changed"
    assert not any(path.endswith("/publications") for path in intercept.paths)
    assert not e.remote.list_episodes(e.remote_admin, e.team.id)
    with pytest.raises(Unauthorized):
        e.router.deliveries(e.alice)


async def test_owner_retry_rebinds_current_login_but_preserves_original_attribution(federation):
    e = federation
    queued = e.router.publish(e.alice, publication(e))
    e.local.revoke_token(e.admin, e.alice.credential_id)
    await e.router.run_once()
    fresh = e.agent(name="fresh-login")
    for other in (e.bob, e.admin):
        with pytest.raises(Forbidden):
            e.router.retry(other, queued.id)
    resumed = e.router.retry(fresh, queued.id)
    assert resumed.user_id == e.alice.user_id and resumed.agent_id == e.alice.agent_id
    with e.router.outbox.db() as db:
        row = db.execute("SELECT * FROM deliveries WHERE id=?", (queued.id,)).fetchone()
    assert row["credential_id"] == fresh.credential_id
    assert json.loads(row["principal"])["agent_id"] == fresh.agent_id
    await e.router.run_once()
    assert e.router.deliveries(fresh)[0].state == "delivered"


async def test_expired_oidc_job_requires_a_fresh_authorized_session(federation):
    e = federation
    old = e.local.authenticate_oidc("https://id.example.test", "alice", "old", time.time() + 60)
    e.mount.user_grants[old.user_id] = MountRole.PUBLISHER
    queued = e.router.publish(old, publication(e))
    with e.local._control() as db:
        db.execute(
            "UPDATE identity_sessions SET expires_at=0 WHERE credential_id=?", (old.credential_id,)
        )
    await e.router.run_once()
    assert e.router.deliveries(e.admin)[0].state == "blocked"
    fresh = e.local.authenticate_oidc("https://id.example.test", "alice", "fresh", time.time() + 60)
    assert fresh.user_id == old.user_id
    del e.mount.user_grants[fresh.user_id]
    with pytest.raises(Forbidden):
        e.router.retry(fresh, queued.id)
    e.mount.user_grants[fresh.user_id] = MountRole.PUBLISHER
    e.router.retry(fresh, queued.id)
    await e.router.run_once()
    assert e.router.deliveries(fresh)[0].state == "delivered"


@pytest.mark.parametrize(
    "field,value",
    [
        ("occurred_at", "2026-10-21T00:00:00Z"),
        ("source_type", "text"),
        ("metadata", {"private_ref": "must not be linked"}),
        ("digest", "incorrect"),
        ("content", "Changed content"),
    ],
)
async def test_remote_receipt_must_match_complete_publication_source(federation, field, value):
    e = federation

    def tamper(request, response):
        if request.url.path.endswith("/publications"):
            body = json.loads(response.content)
            body["episode"][field] = value
            return httpx.Response(200, json=body)
        return response

    e.router.transport = InterceptTransport(e.transport, tamper)
    e.router.publish(e.alice, publication(e))
    await e.router.run_once()
    final = e.router.deliveries(e.alice)[0]
    assert final.state == "blocked" and final.error == "remote_receipt_mismatch"


async def test_remote_authority_and_result_world_are_checked(federation):
    e = federation
    e.router.publish(e.alice, publication(e))
    await e.router.run_once()

    def tamper(request, response):
        body = json.loads(response.content)
        if request.url.path == "/v1/search":
            body["results"][0]["world_id"] = e.private.id
        return httpx.Response(200, json=body)

    e.router.transport = InterceptTransport(e.transport, tamper)
    found = await e.router.search(e.alice, RouterSearch(mount_ids=["work"], query="evidence"))
    assert not found["results"]
    assert found["failures"][0]["error"] == "remote_scope_mismatch"
    e.mount.authority_id = "wrong-authority"
    found = await e.router.search(e.alice, RouterSearch(mount_ids=["work"], query="evidence"))
    assert found["failures"][0]["error"] == "remote_authority_mismatch"


async def test_streaming_response_limit_stops_reading_and_closes_stream():
    class Chunks(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self):
            for _ in range(10):
                self.consumed += 1
                yield b"x" * 1_048_576

        async def aclose(self):
            self.closed = True

    chunks = Chunks()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=chunks))
    ) as client:
        with pytest.raises(RemoteFailure) as error:
            await WorldRouter.request(client, "GET", "https://work.example.test/v1/worlds")
    assert error.value.code == "remote_response_too_large"
    assert chunks.consumed == 5 and chunks.closed


def test_mount_configuration_and_secret_files_are_restricted(federation, monkeypatch):
    e = federation
    for url in (
        "https://name:password@remote.test",
        "https://remote.test?query=1",
        "https://remote.test/other",
        "https://remote.test:99999",
    ):
        with pytest.raises(ValidationError):
            e.mount.model_copy().model_validate(
                {**e.mount.model_dump(), "token_file": e.token_file, "url": url}
            )
    insecure = e.mount.model_copy(update={"url": "http://remote.test"})
    with pytest.raises(InvalidRequest):
        MountConfig(mounts=[insecure]).validated()
    assert MountConfig(
        mounts=[insecure.model_copy(update={"allow_insecure_http": True})]
    ).validated()
    with pytest.raises(InvalidRequest):
        MountConfig(mounts=[e.mount, e.mount]).validated()
    e.token_file.chmod(0o644)
    with pytest.raises(InvalidRequest):
        e.mount.token()
    e.token_file.chmod(0o600)
    config_file = e.tmp_path / "mounts.json"
    raw = e.config.model_dump(mode="json")
    raw["mounts"][0]["token_file"] = e.token_file.name
    config_file.write_text(json.dumps(raw))
    monkeypatch.setenv("MEMORY_WORLDS_MOUNTS_FILE", str(config_file))
    assert MountConfig.from_env().mounts[0].token_file == e.token_file


async def test_real_loopback_http_publication_and_recall(federation):
    e = federation
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    server = uvicorn.Server(
        uvicorn.Config(e.app, log_level="error", access_log=False, lifespan="off")
    )
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        mount = e.mount.model_copy(update={"url": f"http://127.0.0.1:{listener.getsockname()[1]}"})
        router = WorldRouter(e.local, MountConfig(mounts=[mount]))
        router.publish(e.alice, publication(e))
        await router.run_once()
        assert router.deliveries(e.alice)[0].state == "delivered"
        found = await router.search(e.alice, RouterSearch(mount_ids=["work"], query="evidence"))
        assert found["complete"] and len(found["results"]) == 1
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        assert not worker.is_alive()


async def test_partial_federated_recall_reports_remote_outage_without_hiding_results(federation):
    e = federation
    e.router.publish(e.alice, publication(e))
    await e.router.run_once()
    offline = e.mount.model_copy(update={"id": "offline", "url": "https://offline.example.test"})
    e.router.mounts["offline"] = offline

    async def handle(request):
        if request.url.host == "offline.example.test":
            return httpx.Response(503, text="private backend diagnostics")
        return await e.transport.handle_async_request(request)

    e.router.transport = httpx.MockTransport(handle)
    found = await e.router.search(
        e.alice, RouterSearch(mount_ids=["work", "offline"], query="evidence")
    )
    assert not found["complete"] and len(found["results"]) == 1
    assert found["results"][0]["mount_id"] == "work"
    assert found["failures"] == [{"mount_id": "offline", "error": "remote_http_503"}]


async def test_nested_remote_json_cannot_stop_other_deliveries_or_recall(federation):
    e = federation
    malformed = e.mount.model_copy(
        update={"id": "malformed", "url": "https://malformed.example.test"}
    )
    e.router.mounts[malformed.id] = malformed

    async def handle(request):
        if request.url.host == "malformed.example.test":
            # This is well below the byte limit, but exceeds the nesting supported
            # by recursive JSON parsers on supported Python versions.
            return httpx.Response(200, content=b"[" * 10000 + b"0" + b"]" * 10000)
        return await e.transport.handle_async_request(request)

    e.router.transport = httpx.MockTransport(handle)
    e.router.publish(e.alice, publication(e, destination_mount_id="malformed"))
    e.router.publish(e.alice, publication(e))
    assert await e.router.run_once() == 2
    deliveries = {item.destination_mount_id: item for item in e.router.deliveries(e.alice)}
    assert deliveries["malformed"].state == "blocked"
    assert deliveries["malformed"].error == "remote_protocol_error"
    assert deliveries["work"].state == "delivered"
    assert len(e.remote.list_episodes(e.remote_admin, e.team.id)) == 1

    e.router.publish(e.alice, publication(e, source="next-note"))
    assert await e.router.run_once() == 1
    assert len(e.remote.list_episodes(e.remote_admin, e.team.id)) == 2
    found = await e.router.search(
        e.alice, RouterSearch(mount_ids=["malformed", "work"], query="evidence")
    )
    assert len(found["results"]) == 2
    assert not found["complete"]
    assert found["failures"] == [{"mount_id": "malformed", "error": "remote_protocol_error"}]


async def test_graph_recall_preserves_origin_and_rejects_wrong_authority(federation):
    e = federation
    authority = e.remote.authority_id

    def graph(request, response):
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "mode": "graph",
                    "results": [
                        {
                            "fact": "Approved fact",
                            "world_id": e.team.id,
                            "authority_id": authority,
                            "generation": "generation-id",
                            "episode_ids": ["evidence-id"],
                        }
                    ],
                },
            )
        return response

    e.router.transport = InterceptTransport(e.transport, graph)
    request = RouterSearch(mount_ids=["work"], query="evidence", mode="graph")
    found = await e.router.search(e.alice, request)
    assert found["complete"]
    assert found["results"][0]["generation"] == "generation-id"
    assert found["results"][0]["episode_ids"] == ["evidence-id"]
    assert found["results"][0]["authority_id"] == authority
    authority = "untrusted-authority"
    found = await e.router.search(e.alice, request)
    assert not found["results"]
    assert found["failures"][0]["error"] == "remote_authority_mismatch"


async def test_retry_cannot_redirect_an_existing_envelope_or_bypass_publish_switch(federation):
    e = federation
    queued = e.router.publish(e.alice, publication(e))
    e.mount.url = "https://new-endpoint.example.test"
    with pytest.raises(Forbidden):
        e.router.retry(e.alice, queued.id)
    await e.router.run_once()
    assert e.router.deliveries(e.alice)[0].error == "destination_changed"
    e.mount.url = "https://work.example.test"
    e.mount.publish = False
    with pytest.raises(Forbidden):
        e.router.retry(e.alice, queued.id)
    e.mount.publish = True
    e.router.retry(e.alice, queued.id)
    await e.router.run_once()
    assert e.router.deliveries(e.alice)[0].state == "delivered"
