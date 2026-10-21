"""Check scope selection and private credential handoff across client boundaries."""

import json
import os
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from mcp import Client as MCPClient
from pydantic import ValidationError

from memory_worlds import cli
from memory_worlds import mcp as facade
from memory_worlds.client import Client, DelegationResult
from memory_worlds.errors import MemoryError
from memory_worlds.models import (
    AgentContext,
    BindContext,
    CreateScope,
    DelegateCredential,
    Episode,
    EpisodeInput,
    Scope,
    ScopedPublication,
    ScopeFlow,
    ScopeGrant,
    ScopeKind,
    ScopeOperation,
    ScopeSelection,
    SearchRequest,
    UpdateScopeGrant,
)

AT = datetime(2026, 10, 20, tzinfo=UTC)
SCOPE = UUID("00000000-0000-4000-8000-000000000001")
OTHER = UUID("00000000-0000-4000-8000-000000000002")
INVOCATION = UUID("00000000-0000-4000-8000-000000000003")
CONTEXT = UUID("00000000-0000-4000-8000-000000000004")


def scope():
    return Scope(
        id=SCOPE,
        world_id="private",
        kind=ScopeKind.INVOCATION_PRIVATE,
        name="Research scratch",
        created_at=AT,
        invocation_id=INVOCATION,
    )


def context():
    selected = ScopeSelection(world_id="private", scope_id=SCOPE)
    return AgentContext(
        id=CONTEXT,
        credential_id="child-id",
        invocation_id=INVOCATION,
        created_at=AT,
        read_scopes=[selected],
        write_scope=selected,
    )


def episode():
    return Episode(
        scope_id=SCOPE,
        source_id="stable-note",
        content="Private tentative finding",
        occurred_at=AT,
        id="episode-one",
        world_id="private",
        sequence=1,
        digest="fixture-digest",
        received_at=AT,
        actor_id="alice",
        agent_id="researcher",
        projection_state="disabled",
    )


@pytest.fixture
def backend(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path == "/v1/scopes/flow":
            return httpx.Response(200, json={"allowed": False})
        if path == "/v1/credentials/delegate":
            return httpx.Response(
                201,
                json={
                    "token": "mw_child-private-secret",
                    "credential_id": "child-id",
                    "context": context().model_dump(mode="json"),
                    "private_scope": scope().model_dump(mode="json"),
                    "expires_at": None,
                },
            )
        if path == "/v1/contexts" or path.startswith("/v1/contexts/"):
            return httpx.Response(200, json=context().model_dump(mode="json"))
        if path.endswith("/grants"):
            return httpx.Response(204)
        if path.endswith("/scopes"):
            value = scope().model_dump(mode="json")
            return httpx.Response(200, json=[value] if request.method == "GET" else value)
        if path.endswith(str(SCOPE)):
            return httpx.Response(200, json=scope().model_dump(mode="json"))
        if path.endswith("/status"):
            return httpx.Response(
                200, json={"scope": scope().model_dump(mode="json"), "episodes": 1}
            )
        if path == "/v1/search":
            return httpx.Response(200, json={"mode": "graph", "results": [], "worlds": []})
        value = episode().model_dump(mode="json")
        if request.method == "POST":
            return httpx.Response(200, json={"episode": value, "replayed": False})
        return httpx.Response(200, json=[value] if path.endswith("/episodes") else value)

    class MockClient(Client):
        def __init__(self, url="http://memory.invalid", token="mw_parent-private-secret"):
            self.http = httpx.Client(
                base_url=url,
                headers={"Authorization": f"Bearer {token}"},
                transport=httpx.MockTransport(handle),
                trust_env=False,
            )

    monkeypatch.setattr(cli, "Client", MockClient)
    monkeypatch.setattr(facade, "Client", MockClient)
    monkeypatch.setenv("MEMORY_WORLDS_TOKEN", "mw_parent-private-secret")
    return MockClient, requests


def test_client_keeps_scope_and_source_links_on_every_operation(backend):
    client_type, requests = backend
    selected = ScopeSelection(world_id="private", scope_id=SCOPE)
    with client_type() as client:
        assert client.scopes("private")[0].id == SCOPE
        assert client.get_scope("private", SCOPE).kind == ScopeKind.INVOCATION_PRIVATE
        client.create_scope("private", CreateScope(kind=ScopeKind.RUN_SHARED, name="Run"))
        client.grant_scope("private", SCOPE, UpdateScopeGrant(credential_id="child", operations=[]))
        assert client.bind_context(BindContext(read_scopes=[selected])).id == CONTEXT
        assert client.get_context(CONTEXT).invocation_id == INVOCATION
        source = EpisodeInput(source_id="stable-note", content="A note", occurred_at=AT)
        assert client.capture("private", source, scope_id=SCOPE).episode.scope_id == SCOPE
        assert source.scope_id is None
        client.get_episode("private", "episode-one", scope_id=SCOPE)
        assert client.episodes("private", scope_id=SCOPE, limit=4)[0].scope_id == SCOPE
        client.status("private", scope_id=SCOPE)
        client.publish(
            "private",
            ScopedPublication(
                source_id="publication-one",
                content="Selected conclusion",
                occurred_at=AT,
                source_scope=selected,
                source_episode_id="episode-one",
            ),
            scope_id=OTHER,
        )
    assert all(r.headers["authorization"] == "Bearer mw_parent-private-secret" for r in requests)
    captured = next(r for r in requests if r.url.path == "/v1/worlds/private/episodes")
    assert json.loads(captured.content)["scope_id"] == str(SCOPE)
    scoped_reads = [r for r in requests if r.method == "GET" and "/episodes" in r.url.path]
    assert all(f"/scopes/{SCOPE}/episodes" in r.url.path for r in scoped_reads)
    assert scoped_reads[-1].url.params["limit"] == "4"
    publication = requests[-1]
    assert publication.url.path == f"/v1/worlds/private/scopes/{OTHER}/publications"
    assert json.loads(publication.content)["source_scope"] == selected.model_dump(mode="json")
    assert json.loads(publication.content)["source_episode_id"] == "episode-one"


def test_client_rejects_scope_conflicts_before_http_and_preserves_multi_scope_query(backend):
    client_type, requests = backend
    with client_type() as client:
        before = len(requests)
        with pytest.raises(ValueError, match="does not match"):
            client.capture(
                "private",
                EpisodeInput(scope_id=SCOPE, source_id="one", content="X", occurred_at=AT),
                scope_id=OTHER,
            )
        with pytest.raises(ValueError, match="explicit destination"):
            client.publish(
                "private",
                ScopedPublication(
                    source_id="two", content="X", occurred_at=AT, source_episode_id="one"
                ),
            )
        assert len(requests) == before
        client.search(
            SearchRequest(
                query="lathe",
                scopes=[
                    ScopeSelection(world_id="private", scope_id=SCOPE),
                    ScopeSelection(world_id="team", scope_id=OTHER),
                ],
                mode="graph",
                include_entities=True,
            )
        )
    payload = json.loads(requests[-1].content)
    assert payload["scopes"] == [
        {"world_id": "private", "scope_id": str(SCOPE)},
        {"world_id": "team", "scope_id": str(OTHER)},
    ]
    assert payload["include_entities"] is True


def test_cli_scope_selections_reach_http_without_implicit_worlds(backend, capsys):
    _, requests = backend
    args = cli.parser().parse_args(
        [
            "search",
            "lathe",
            "--select",
            f"private:{SCOPE}",
            "--select",
            f"team:{OTHER}",
            "--entities",
        ]
    )
    cli.run(args)
    payload = json.loads(requests[-1].content)
    assert payload["world_ids"] == []
    assert len(payload["scopes"]) == 2
    assert payload["include_entities"] is True
    cli.run(cli.parser().parse_args(["status", "private", "--scope", str(SCOPE)]))
    assert requests[-1].url.path.endswith(f"/scopes/{SCOPE}/status")
    assert "mw_parent-private-secret" not in capsys.readouterr().out


def test_python_and_cli_flow_check_preserve_explicit_scopes(backend, tmp_path, capsys):
    client_type, requests = backend
    request = ScopeFlow(
        read_scopes=[ScopeSelection(world_id="private", scope_id=SCOPE)],
        write_scope=ScopeSelection(world_id="private", scope_id=OTHER),
    )
    with client_type() as client:
        assert client.can_flow(request) is False
    path = tmp_path / "flow.json"
    path.write_text(request.model_dump_json())
    cli.run(cli.parser().parse_args(["scope-flow", "--input", str(path)]))
    assert json.loads(capsys.readouterr().out) == {"allowed": False}
    assert requests[-1].url.path == "/v1/scopes/flow"
    assert json.loads(requests[-1].content) == request.model_dump(mode="json")


@pytest.mark.parametrize("command", ["capture", "publish"])
def test_cli_capture_and_publication_keep_explicit_scope(backend, capsys, command):
    _, requests = backend
    cli.run(
        cli.parser().parse_args(
            [
                command,
                "private",
                "--scope",
                str(SCOPE),
                "--context",
                str(CONTEXT),
                "--source-id",
                "one",
                "--text",
                "Selected",
                "--at",
                AT.isoformat(),
            ]
        )
    )
    if command == "capture":
        assert json.loads(requests[-1].content)["scope_id"] == str(SCOPE)
    else:
        assert requests[-1].url.path.endswith(f"/scopes/{SCOPE}/publications")
    assert json.loads(requests[-1].content)["context_id"] == str(CONTEXT)
    assert "mw_parent-private-secret" not in capsys.readouterr().out


def test_cli_delegation_secret_goes_only_to_exclusive_private_file(backend, tmp_path, capsys):
    _, requests = backend
    request = tmp_path / "delegation.json"
    request.write_text(json.dumps({"private_world_id": "private"}))
    output = tmp_path / "child.token"
    args = cli.parser().parse_args(
        [
            "agent-delegate",
            "--input",
            str(request),
            "--token-output",
            str(output),
        ]
    )
    cli.run(args)
    shown = capsys.readouterr().out
    assert output.read_text() == "mw_child-private-secret\n"
    assert output.stat().st_mode & 0o777 == 0o600
    assert "mw_child-private-secret" not in shown
    assert "mw_parent-private-secret" not in shown
    assert json.loads(shown)["credential_id"] == "child-id"
    before = len(requests)
    with pytest.raises(FileExistsError):
        cli.run(args)
    assert len(requests) == before
    assert output.read_text() == "mw_child-private-secret\n"


def test_failed_delegation_removes_empty_reserved_output(tmp_path):
    class Unavailable:
        def delegate(self, request):
            raise RuntimeError("fixture-unavailable")

    output = tmp_path / "child.token"
    with pytest.raises(RuntimeError, match="fixture-unavailable"):
        cli.delegate_to_file(Unavailable(), DelegateCredential(), output)
    assert not output.exists()


def test_delegated_result_repr_hides_token():
    result = DelegationResult(
        token="mw_child-private-secret", credential_id="child-id", context=context()
    )
    assert "mw_child-private-secret" not in repr(result)


def test_cli_structured_input_rejects_unknown_authority_fields_before_request(backend, tmp_path):
    _, requests = backend
    request = tmp_path / "invalid.json"
    request.write_text(json.dumps({"admin": True, "token": "must-not-be-consumed"}))
    with pytest.raises(ValidationError):
        cli.run(
            cli.parser().parse_args(
                [
                    "agent-delegate",
                    "--input",
                    str(request),
                    "--token-output",
                    str(tmp_path / "child.token"),
                ]
            )
        )
    assert not requests
    assert not (tmp_path / "child.token").exists()


def test_cli_structured_input_rejects_fifo_without_waiting(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("requires FIFO support")
    request = tmp_path / "pipe"
    os.mkfifo(request)
    with pytest.raises(MemoryError, match="regular JSON file"):
        cli.read_input(request, DelegateCredential)


@pytest.mark.parametrize(
    "command", ["export", "restore", "import-chatgpt", "import-pi", "graph-rebuild"]
)
def test_cli_local_operations_parse_uuid_scope(command, tmp_path):
    arguments = [command, "private"]
    if command != "graph-rebuild":
        arguments.append(str(tmp_path / "archive"))
    assert cli.parser().parse_args([*arguments, "--scope", str(SCOPE)]).scope == SCOPE


async def test_mcp_scope_tools_preserve_scope_and_do_not_offer_secret_delegation(backend):
    _, requests = backend
    async with MCPClient(
        facade.create_mcp("http://memory.invalid", "mw_parent-private-secret")
    ) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "memory_scopes",
            "memory_scope_status",
            "memory_context",
            "memory_bind_context",
            "memory_episodes",
        } <= names
        assert not any(
            "delegate" in name or "credential" in name or "grant" in name for name in names
        )
        captured = await client.call_tool(
            "memory_capture",
            {
                "world_id": "private",
                "scope_id": str(SCOPE),
                "source_id": "one",
                "content": "Private note",
                "occurred_at": AT.isoformat(),
            },
        )
        assert not captured.is_error
        assert captured.structured_content["episode"]["scope_id"] == str(SCOPE)
        recalled = await client.call_tool(
            "memory_search",
            {
                "world_ids": [],
                "scopes": [{"world_id": "private", "scope_id": str(SCOPE)}],
                "query": "lathe",
                "graph": True,
                "include_entities": True,
            },
        )
        assert not recalled.is_error
        listed = await client.call_tool(
            "memory_episodes", {"world_id": "private", "scope_id": str(SCOPE), "limit": 2}
        )
        assert not listed.is_error
        assert "mw_parent-private-secret" not in listed.model_dump_json()
    assert json.loads(requests[1].content)["scopes"][0]["scope_id"] == str(SCOPE)
    assert json.loads(requests[1].content)["include_entities"] is True
    assert requests[2].url.path.endswith(f"/scopes/{SCOPE}/episodes")


def test_real_daemon_python_and_cli_child_context_isolation(tmp_path):
    from test_cli import _daemon, _invoke

    from memory_worlds.store import MemoryStore

    data_dir = tmp_path / "live-authority"
    world, administrator = MemoryStore(data_dir).bootstrap("alice")
    with _daemon(data_dir) as url:
        with Client(url, administrator) as admin:
            issued = admin.request(
                "POST",
                "/v1/credentials",
                json={
                    "user_id": "alice",
                    "agent_id": "coordinator",
                    "grants": {world.id: "writer"},
                },
            )
        with Client(url, issued["token"]) as parent:
            run = parent.create_scope(
                world.id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Research run")
            )
            assert run.run_id is not None
            selected = ScopeSelection(world_id=world.id, scope_id=run.id)
            binding = parent.bind_context(BindContext(read_scopes=[selected], run_id=run.run_id))
            assert parent.get_context(binding.id).run_id == run.run_id
            parent.publish(
                world.id,
                ScopedPublication(
                    source_id="brief",
                    content="Find the lathe coolant setting",
                    occurred_at=AT,
                ),
                scope_id=run.id,
            )
            delegation = DelegateCredential(
                grants=[ScopeGrant(**selected.model_dump(), operations=[ScopeOperation.READ])],
                private_world_id=world.id,
                run_id=run.run_id,
                parent_context_id=binding.id,
            )
            request_path = tmp_path / "child-request.json"
            request_path.write_text(delegation.model_dump_json())
            token_path = tmp_path / "child.token"
            handed_off = _invoke(
                data_dir,
                "agent-delegate",
                "--input",
                str(request_path),
                "--token-output",
                str(token_path),
                url=url,
                token=issued["token"],
            )
            child_token = token_path.read_text().strip()
            assert "token" not in handed_off
            assert child_token not in json.dumps(handed_off)
            child_scope = Scope.model_validate(handed_off["private_scope"])
            child_context = AgentContext.model_validate(handed_off["context"])
            assert child_context.parent_invocation_id == binding.invocation_id
            assert child_context.write_scope.scope_id == child_scope.id
            sibling = parent.delegate(delegation)
            assert sibling.private_scope.id != child_scope.id
            with Client(url, child_token) as child:
                assert (
                    child.get_context(child_context.id).invocation_id == child_context.invocation_id
                )
                note = EpisodeInput(
                    source_id="hypothesis",
                    content="lathe coolant is tentative",
                    occurred_at=AT,
                    context_id=child_context.id,
                )
                first = child.capture(world.id, note, scope_id=child_scope.id)
                replay = child.capture(world.id, note, scope_id=child_scope.id)
                assert replay.replayed and replay.episode.id == first.episode.id
                assert first.episode.invocation_id == child_context.invocation_id
                found = child.search(
                    SearchRequest(
                        query="coolant",
                        mode="source",
                        scopes=[
                            selected,
                            ScopeSelection(world_id=world.id, scope_id=child_scope.id),
                        ],
                    )
                )
                assert {hit["source_id"] for hit in found["results"]} == {"brief", "hypothesis"}
                assert child.status(world.id, scope_id=child_scope.id)["episodes"] == 1
                assert (
                    child.can_flow(
                        ScopeFlow(
                            read_scopes=[selected],
                            write_scope=ScopeSelection(world_id=world.id, scope_id=child_scope.id),
                        )
                    )
                    is True
                )
                with pytest.raises(httpx.HTTPStatusError) as forbidden_write:
                    child.capture(world.id, note, scope_id=run.id)
                assert forbidden_write.value.response.status_code == 403
                with pytest.raises(httpx.HTTPStatusError) as forbidden_sibling:
                    child.get_scope(world.id, sibling.private_scope.id)
                assert forbidden_sibling.value.response.status_code == 403
            with pytest.raises(httpx.HTTPStatusError) as parent_private_read:
                parent.episodes(world.id, scope_id=child_scope.id)
            assert parent_private_read.value.response.status_code == 403
        with Client(url, administrator) as admin:
            admin.request("DELETE", f"/v1/credentials/{issued['credential_id']}")
        with Client(url, child_token) as child:
            with pytest.raises(httpx.HTTPStatusError) as revoked:
                child.me()
            assert revoked.value.response.status_code == 401
