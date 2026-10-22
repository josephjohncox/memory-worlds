"""Current maintenance contracts across typed HTTP clients, CLI and MCP tools."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from mcp import Client as MCPClient

from memory_worlds import cli, mcp
from memory_worlds.client import Client
from memory_worlds.context_models import ContextBundleInput
from memory_worlds.maintenance_models import MaintenanceRequest
from memory_worlds.thread_models import CreateThread

AT = datetime(2026, 10, 22, tzinfo=UTC)


@pytest.fixture
def transport(monkeypatch):
    thread_id, scope_id, job_id, bundle_id = (uuid4() for _ in range(4))
    thread = {
        "id": str(thread_id),
        "authority_id": "fixture-authority",
        "world_id": "private",
        "scope_id": str(scope_id),
        "owner_id": "alice",
        "creator_credential_id": "agent-one",
        "name": "Research",
        "alias": None,
        "agent_id": None,
        "run_id": None,
        "revision": 0,
        "head_digest": "0" * 64,
        "created_at": AT.isoformat(),
        "expires_at": None,
        "state": "open",
    }
    job = {
        "id": str(job_id),
        "operation_id": str(job_id),
        "thread_id": str(thread_id),
        "kind": "summarize",
        "profile": "lightweight",
        "state": "queued",
        "attempts": 0,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "error": None,
        "result_artifact_id": None,
        "result": None,
        "usage_uncertain": False,
        "unresolved_effects": [],
        "replayed": False,
    }
    bundle = {
        "id": str(bundle_id),
        "bundle_id": str(bundle_id),
        "thread_id": str(thread_id),
        "revision": 1,
        "digest": "1" * 64,
        "archive_state": "ready",
        "coverage": "complete",
        "source_refs": [],
        "entries": [
            {
                "id": "message-1",
                "parent_id": None,
                "type": "message",
                "role": "user",
                "payload": {"content": "Recoverable context"},
            }
        ],
        "omissions": [],
    }
    artifact_id = uuid4()
    artifact = {
        "id": str(artifact_id),
        "artifact_id": str(artifact_id),
        "thread_id": str(thread_id),
        "job_id": str(job_id),
        "kind": "summary",
        "content": "Selected summary",
        "metadata": {},
        "source_refs": [],
    }
    calls, settings = [], {"bundle": bundle, "artifact": artifact}

    def handle(request):
        calls.append(request)
        if settings.get("deny"):
            return httpx.Response(403, json={"detail": "private-upstream-canary"})
        if "/bundles" in request.url.path:
            result = {
                key: value
                for key, value in bundle.items()
                if request.method == "GET" or key not in {"entries", "omissions"}
            }
            if settings.get("wrong_bundle"):
                result["bundle_id"] = str(uuid4())
            return httpx.Response(200, json=result)
        if request.url.path.startswith("/v1/maintenance/artifacts"):
            return httpx.Response(200, json=artifact)
        if request.url.path.startswith("/v1/threads"):
            return httpx.Response(
                200,
                json=[thread]
                if request.method == "GET" and request.url.path == "/v1/threads"
                else thread,
            )
        if request.url.path == "/v1/maintenance/jobs" and request.method == "GET":
            return httpx.Response(200, json=[job])
        return httpx.Response(200, json=job)

    class FixtureClient(Client):
        def __init__(self, url, token):
            self.http = httpx.Client(
                base_url=url,
                headers={"Authorization": f"Bearer {token}"},
                transport=httpx.MockTransport(handle),
                trust_env=False,
                follow_redirects=False,
            )

    monkeypatch.setattr(cli, "Client", FixtureClient)
    monkeypatch.setattr(mcp, "Client", FixtureClient)
    return FixtureClient, calls, settings, thread_id, bundle_id, job_id


def test_typed_thread_and_job_clients_preserve_ids_and_wait_boundaries(transport):
    client_type, calls, _, thread_id, bundle_id, job_id = transport
    request = MaintenanceRequest(operation_id=job_id, thread_id=thread_id, bundle_ids=[bundle_id])
    creation = CreateThread(operation_id=uuid4(), world_id="private", name="Research")
    with client_type("https://memory.test", "ordinary-token") as client:
        assert client.create_thread(creation).id == thread_id
        assert [item.id for item in client.threads()] == [thread_id]
        assert client.thread(thread_id).id == thread_id
        assert client.submit_maintenance(request).id == job_id
        assert [item.id for item in client.maintenance_jobs()] == [job_id]
        assert client.maintenance_job(job_id).state == "queued"
        assert client.wait_maintenance(job_id, timeout_seconds=60).id == job_id
        assert client.cancel_maintenance(job_id).id == job_id
        for timeout in (-1, 61, float("inf"), float("nan"), True, "private-invalid-timeout"):
            with pytest.raises(ValueError, match="finite"):
                client.wait_maintenance(job_id, timeout_seconds=timeout)
        with pytest.raises(ValueError):
            client.maintenance_job("../../credentials")
    assert len(calls) == 8
    assert calls[0].method == "POST" and json.loads(calls[0].content) == creation.model_dump(
        mode="json"
    )
    assert calls[3].method == "POST" and json.loads(calls[3].content) == request.model_dump(
        mode="json"
    )
    assert calls[6].url.params["timeout_seconds"] == "60"
    assert calls[6].extensions["timeout"]["read"] == 65
    assert calls[7].url.path == f"/v1/maintenance/jobs/{job_id}/cancel"
    assert all(call.headers["authorization"] == "Bearer ordinary-token" for call in calls)
    assert all("ordinary-token" not in str(call.url) for call in calls)


def test_typed_archives_recover_exact_bundle_and_reject_mismatched_response(transport):
    client_type, calls, settings, thread_id, bundle_id, _ = transport
    payload = ContextBundleInput(
        operation_id=uuid4(), expected_revision=0, entries=settings["bundle"]["entries"]
    )
    with client_type("https://memory.test", "ordinary-token") as client:
        receipt = client.archive_context(thread_id, payload)
        assert receipt.bundle_id == bundle_id and receipt.archive_state == "ready"
        recovered = client.context_bundle(thread_id, bundle_id)
        assert recovered.entries[0].payload == {"content": "Recoverable context"}
        artifact = client.maintenance_artifact(settings["artifact"]["artifact_id"])
        assert artifact.content == "Selected summary"
        settings["wrong_bundle"] = True
        with pytest.raises(ValueError, match="does not match"):
            client.context_bundle(thread_id, bundle_id)
    assert calls[0].url.path == f"/v1/threads/{thread_id}/bundles"
    assert json.loads(calls[0].content) == payload.model_dump(mode="json")
    assert calls[1].url.path == f"/v1/threads/{thread_id}/bundles/{bundle_id}"


def test_cli_context_archive_read_and_artifact_are_explicit_json_operations(
    transport, tmp_path, capsys, monkeypatch
):
    _, calls, settings, thread_id, bundle_id, _ = transport
    monkeypatch.setenv("MEMORY_WORLDS_TOKEN", "ordinary-token")
    input_path = tmp_path / "context.json"
    input_path.write_text(
        json.dumps(
            {
                "operation_id": str(uuid4()),
                "expected_revision": 0,
                "entries": settings["bundle"]["entries"],
            }
        )
    )
    for arguments in (
        ["context-archive", str(thread_id), "--input", str(input_path)],
        ["context-read", str(thread_id), str(bundle_id)],
        ["maintenance", "artifact", settings["artifact"]["artifact_id"]],
    ):
        monkeypatch.setattr("sys.argv", ["memory-worlds", *arguments])
        cli.main()
        output = capsys.readouterr()
        assert not output.err and "ordinary-token" not in output.out
        assert json.loads(output.out)
    assert [item.method for item in calls] == ["POST", "GET", "GET"]


def test_cli_thread_and_maintenance_use_private_token_file_and_json_output(
    transport, tmp_path, capsys, monkeypatch
):
    _, calls, _, thread_id, bundle_id, job_id = transport
    credential = tmp_path / "agent.token"
    credential.write_text("ordinary-token\n")
    credential.chmod(0o600)
    request_path = tmp_path / "maintenance.json"
    request_path.write_text(
        json.dumps(
            {
                "operation_id": str(job_id),
                "thread_id": str(thread_id),
                "bundle_ids": [str(bundle_id)],
            }
        )
    )
    thread_path = tmp_path / "thread.json"
    thread_path.write_text(
        json.dumps({"operation_id": str(uuid4()), "world_id": "private", "name": "Research"})
    )
    prefix = ["--url", "https://memory.test", "--token-file", str(credential)]
    for arguments in (
        ["thread", "create", "--input", str(thread_path)],
        ["thread", "list"],
        ["thread", "show", str(thread_id)],
        ["maintenance", "submit", "--input", str(request_path)],
        ["maintenance", "status", str(job_id)],
        ["maintenance", "wait", str(job_id), "--timeout", "0"],
        ["maintenance", "cancel", str(job_id)],
    ):
        monkeypatch.setattr("sys.argv", ["memory-worlds", *prefix, *arguments])
        cli.main()
        output = capsys.readouterr()
        assert output.err == ""
        assert "ordinary-token" not in output.out
        assert json.loads(output.out)
    assert len(calls) == 7
    assert all(call.headers["authorization"] == "Bearer ordinary-token" for call in calls)


def test_cli_maintenance_errors_omit_private_input_and_upstream_bodies(
    transport, tmp_path, capsys, monkeypatch
):
    _, calls, settings, thread_id, bundle_id, job_id = transport
    monkeypatch.setenv("MEMORY_WORLDS_TOKEN", "ordinary-token")
    input_path = tmp_path / "request.json"
    input_path.write_text(
        json.dumps(
            {
                "operation_id": str(job_id),
                "thread_id": str(thread_id),
                "bundle_ids": [str(bundle_id)],
                "profile": "private-invalid-input-canary!",
            }
        )
    )
    monkeypatch.setattr(
        "sys.argv", ["memory-worlds", "maintenance", "submit", "--input", str(input_path)]
    )
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert calls == []
    first = capsys.readouterr()
    assert first.out == "" and "private-invalid-input-canary" not in first.err
    settings["deny"] = True
    monkeypatch.setattr("sys.argv", ["memory-worlds", "maintenance", "status", str(job_id)])
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    second = capsys.readouterr()
    assert second.out == "" and second.err.strip() == "memory API returned HTTP 403"
    assert "private-upstream-canary" not in second.err


async def test_mcp_maintenance_tools_are_bounded_typed_and_sanitized(transport, caplog):
    _, calls, settings, thread_id, bundle_id, job_id = transport
    async with MCPClient(mcp.create_mcp("https://memory.test", "ordinary-token")) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        schema = tools["memory_maintenance_submit"].input_schema
        assert schema["$defs"]["MaintenanceRequest"]["properties"]["bundle_ids"]["maxItems"] == 100
        assert schema["$defs"]["MaintenanceRequest"]["additionalProperties"] is False
        for name, arguments in (
            ("memory_threads", {}),
            ("memory_thread", {"thread_id": str(thread_id)}),
            ("memory_context_bundle", {"thread_id": str(thread_id), "bundle_id": str(bundle_id)}),
            ("memory_maintenance_artifact", {"artifact_id": settings["artifact"]["artifact_id"]}),
            (
                "memory_maintenance_submit",
                {
                    "request": {
                        "operation_id": str(job_id),
                        "thread_id": str(thread_id),
                        "bundle_ids": [str(bundle_id)],
                    }
                },
            ),
            ("memory_maintenance_status", {"job_id": str(job_id)}),
            ("memory_maintenance_wait", {"job_id": str(job_id), "timeout_seconds": 0}),
            ("memory_maintenance_cancel", {"job_id": str(job_id)}),
        ):
            result = await client.call_tool(name, arguments)
            assert not result.is_error, result.model_dump_json()
            assert "ordinary-token" not in result.model_dump_json()
        previous = len(calls)
        invalid = await client.call_tool(
            "memory_maintenance_wait", {"job_id": str(job_id), "timeout_seconds": 61}
        )
        assert invalid.is_error and len(calls) == previous
        settings["deny"] = True
        denied = await client.call_tool("memory_maintenance_status", {"job_id": str(job_id)})
        assert denied.is_error
        assert "private-upstream-canary" not in denied.model_dump_json()
        assert "ordinary-token" not in denied.model_dump_json()
    assert "private-upstream-canary" not in caplog.text
    assert "ordinary-token" not in caplog.text


@pytest.fixture
def authority(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from memory_worlds.api import create_app
    from memory_worlds.maintenance_provider import WorkerProfile, WorkerProfiles
    from memory_worlds.models import CreateToken, Role
    from memory_worlds.store import MemoryStore

    monkeypatch.setenv("MEMORY_WORLDS_GRAPH_BACKEND", "disabled")
    store = MemoryStore(tmp_path / "authority")
    world, admin_token = store.bootstrap("alice")
    admin = store.authenticate(admin_token)
    token = store.issue_token(
        admin,
        CreateToken(user_id="alice", agent_id="thread-client", grants={world.id: Role.WRITER}),
    )
    sibling = store.issue_token(
        admin,
        CreateToken(user_id="alice", agent_id="sibling-client", grants={world.id: Role.WRITER}),
    )
    calls = []

    class FixtureProvider:
        def __init__(self):
            self.calls = []

        async def propose(self, profile, *, kind, text, bundle_ids):
            self.calls.append((kind, text, list(bundle_ids)))
            return {
                "kind": "summary" if kind == "summarize" else "enrichment-assessment",
                "content": "Tentative selected technical finding",
                "source_bundle_ids": [str(item) for item in bundle_ids],
                "unresolved_questions": ["Confirm the current operating limit"],
                "contradictions": [],
                "flags": []
                if kind == "summarize"
                else [
                    {
                        "reason": "needs-validation",
                        "bundle_id": str(bundle_ids[0]),
                        "question": "Which source confirms the limit?",
                    }
                ],
            }

        async def close(self):
            return None

    provider = FixtureProvider()
    key_file = tmp_path / "provider.key"
    key_file.write_text("fixture-provider-token\n")
    key_file.chmod(0o600)
    profiles = WorkerProfiles(
        poll_seconds=0.05,
        profiles={
            "lightweight": WorkerProfile(
                endpoint="https://provider.invalid/v1", model="luna", api_key_file=key_file
            )
        },
    )
    with TestClient(
        create_app(
            store.data_dir,
            store=store,
            maintenance_profiles=profiles,
            maintenance_provider=provider,
        )
    ) as api:

        def handle(request):
            calls.append(request)
            response = api.request(
                request.method,
                str(request.url).removeprefix("https://memory.test"),
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        class AuthorityClient(Client):
            def __init__(self, url, bearer):
                self.http = httpx.Client(
                    base_url=url,
                    headers={"Authorization": f"Bearer {bearer}"},
                    transport=httpx.MockTransport(handle),
                    follow_redirects=False,
                    trust_env=False,
                )

        AuthorityClient.provider = provider
        monkeypatch.setattr(cli, "Client", AuthorityClient)
        monkeypatch.setattr(mcp, "Client", AuthorityClient)
        yield AuthorityClient, store, admin, token, sibling, world, calls


def test_client_archives_and_recovers_through_real_authorized_api(authority):
    client_type, store, admin, token, sibling, world, calls = authority
    request = CreateThread(operation_id=uuid4(), world_id=world.id, name="Thread private")
    archive = ContextBundleInput(
        operation_id=uuid4(),
        expected_revision=0,
        entries=[
            {
                "id": "one",
                "type": "message",
                "role": "user",
                "payload": {"text": "A private thread thought"},
            },
            {
                "id": "two",
                "parent_id": "one",
                "type": "tool-call",
                "payload": {"call_id": "call-1", "name": "inspect"},
            },
            {
                "id": "three",
                "parent_id": "two",
                "type": "tool-result",
                "payload": {"call_id": "call-1", "result": {"count": 2}},
            },
        ],
    )
    with client_type("https://memory.test", token) as client:
        thread = client.create_thread(request)
        assert client.create_thread(request).id == thread.id
        assert client.thread(thread.id).scope_id == thread.scope_id
        assert [item.id for item in client.threads(world_id=world.id)] == [thread.id]
        receipt = client.archive_context(thread.id, archive)
        assert receipt.archive_state == "ready" and receipt.coverage == "complete"
        assert client.archive_context(thread.id, archive) == receipt
        recovered = client.context_bundle(thread.id, receipt.bundle_id)
        assert [item.model_dump() for item in recovered.entries] == [
            item.model_dump() for item in archive.entries
        ]
        assert client.thread(thread.id).revision == receipt.revision
    assert not store.authenticate(token).admin
    with client_type("https://memory.test", sibling) as other:
        assert other.threads() == []
        with pytest.raises(httpx.HTTPStatusError) as denied:
            other.context_bundle(thread.id, receipt.bundle_id)
        assert denied.value.response.status_code in (403, 404)
    assert all(request.headers["authorization"] == f"Bearer {token}" for request in calls[:-2])


async def test_cli_and_mcp_recover_archived_context_and_honor_live_revocation(
    authority, tmp_path, capsys, monkeypatch
):
    client_type, store, admin, token, _, world, _ = authority
    with client_type("https://memory.test", token) as client:
        thread = client.create_thread(
            CreateThread(operation_id=uuid4(), world_id=world.id, name="Workflow")
        )
        archive = ContextBundleInput(
            operation_id=uuid4(),
            expected_revision=0,
            entries=[
                {
                    "id": "message",
                    "type": "message",
                    "role": "user",
                    "payload": {"text": "private-context-canary"},
                }
            ],
        )
        receipt = client.archive_context(thread.id, archive)
    path = tmp_path / "agent.token"
    path.write_text(token + "\n")
    path.chmod(0o600)
    monkeypatch.setattr(
        "sys.argv",
        [
            "memory-worlds",
            "--url",
            "https://memory.test",
            "--token-file",
            str(path),
            "context-read",
            str(thread.id),
            str(receipt.bundle_id),
        ],
    )
    cli.main()
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["entries"][0]["payload"]["text"] == "private-context-canary"
    assert token not in output.out
    async with MCPClient(mcp.create_mcp("https://memory.test", token)) as client:
        listed = await client.call_tool("memory_threads", {})
        assert not listed.is_error and str(thread.id) in listed.model_dump_json()
        recovered = await client.call_tool(
            "memory_context_bundle",
            {"thread_id": str(thread.id), "bundle_id": str(receipt.bundle_id)},
        )
        assert not recovered.is_error and "private-context-canary" in recovered.model_dump_json()
        store.revoke_token(admin, store.authenticate(token).credential_id)
        denied = await client.call_tool(
            "memory_context_bundle",
            {"thread_id": str(thread.id), "bundle_id": str(receipt.bundle_id)},
        )
        assert denied.is_error
        assert "private-context-canary" not in denied.model_dump_json()
        assert token not in denied.model_dump_json()


def test_client_jobs_run_with_selected_profile_and_keep_artifacts_private(authority):
    client_type, _, _, token, sibling, world, _ = authority
    with client_type("https://memory.test", token) as client:
        thread = client.create_thread(
            CreateThread(operation_id=uuid4(), world_id=world.id, name="Maintenance")
        )
        archive = client.archive_context(
            thread.id,
            ContextBundleInput(
                operation_id=uuid4(),
                expected_revision=0,
                entries=[
                    {
                        "id": "input",
                        "type": "message",
                        "role": "user",
                        "payload": {"text": "A technical finding needs verification"},
                    }
                ],
            ),
        )
        request = MaintenanceRequest(
            operation_id=uuid4(), thread_id=thread.id, bundle_ids=[archive.bundle_id]
        )
        submitted = client.submit_maintenance(request)
        assert submitted.id == request.operation_id
        result = client.wait_maintenance(submitted.id, timeout_seconds=5)
        assert result.state == "succeeded", result.model_dump_json()
        replay = client.submit_maintenance(request)
        assert replay.replayed and replay.id == submitted.id
        assert len(client_type.provider.calls) == 1
        artifact = client.maintenance_artifact(result.result_artifact_id)
        assert artifact.thread_id == thread.id
        assert artifact.metadata["claim_status"] == "tentative"
        assert artifact.metadata["unresolved_questions"] == ["Confirm the current operating limit"]
        assert client.maintenance_job(submitted.id).result_artifact_id == artifact.artifact_id
        assert [item.id for item in client.maintenance_jobs()] == [submitted.id]
    with client_type("https://memory.test", sibling) as other:
        assert other.maintenance_jobs() == []
        for read in (
            lambda: other.maintenance_job(submitted.id),
            lambda: other.maintenance_artifact(artifact.artifact_id),
        ):
            with pytest.raises(httpx.HTTPStatusError) as denied:
                read()
            assert denied.value.response.status_code in (403, 404)


async def test_mcp_job_submit_wait_and_artifact_use_same_scoped_api(authority):
    client_type, _, _, token, _, world, _ = authority
    with client_type("https://memory.test", token) as client:
        thread = client.create_thread(
            CreateThread(operation_id=uuid4(), world_id=world.id, name="Enrichment")
        )
        archive = client.archive_context(
            thread.id,
            ContextBundleInput(
                operation_id=uuid4(),
                expected_revision=0,
                entries=[
                    {
                        "id": "note",
                        "type": "message",
                        "role": "assistant",
                        "payload": {"text": "Tentative finding"},
                    }
                ],
            ),
        )
    operation_id = uuid4()
    async with MCPClient(mcp.create_mcp("https://memory.test", token)) as client:
        submitted = await client.call_tool(
            "memory_maintenance_submit",
            {
                "request": {
                    "operation_id": str(operation_id),
                    "thread_id": str(thread.id),
                    "bundle_ids": [str(archive.bundle_id)],
                    "kind": "assess-enrichment",
                }
            },
        )
        assert not submitted.is_error, submitted.model_dump_json()
        completed = await client.call_tool(
            "memory_maintenance_wait", {"job_id": str(operation_id), "timeout_seconds": 5}
        )
        assert not completed.is_error, completed.model_dump_json()
        receipt = completed.structured_content
        assert receipt["state"] == "succeeded", receipt
        assert receipt["profile"] == "lightweight"
        artifact = await client.call_tool(
            "memory_maintenance_artifact", {"artifact_id": receipt["result_artifact_id"]}
        )
        assert not artifact.is_error, artifact.model_dump_json()
        assert artifact.structured_content["metadata"]["flags"][0]["reason"] == "needs-validation"
        assert token not in artifact.model_dump_json()
    assert len(client_type.provider.calls) == 1
