"""Exercise the SDK 2 protocol against the facade's HTTP contract."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client as MCPClient
from test_identity import identity as identity_fixture

from memory_worlds import mcp as facade
from memory_worlds.api import create_app
from memory_worlds.auth import OIDCAuthenticator
from memory_worlds.client import Client as HTTPClient
from memory_worlds.models import (
    CreateScope,
    CreateWorld,
    Episode,
    Role,
    Scope,
    ScopeKind,
    UpdateAlias,
    World,
    WorldKind,
)
from memory_worlds.scopes import world_shared_scope_id

oidc_identity = identity_fixture


@pytest.fixture
def backend(monkeypatch):
    requests = []
    world = World(
        id="private",
        authority_id="fixture-authority",
        name="Private",
        kind=WorldKind.PERSONAL,
        owner_id="alice",
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
        scope_id=world_shared_scope_id("fixture-authority", "private"),
    )
    scope = Scope(
        id=world.scope_id,
        world_id=world.id,
        kind=ScopeKind.WORLD_SHARED,
        name="Shared",
        alias="shared",
        created_at=world.created_at,
    )
    deny = {"enabled": False}

    def handle(request):
        requests.append(request)
        if deny["enabled"]:
            return httpx.Response(403, json={"detail": "private-backend-detail"})
        if request.url.path == "/v1/worlds":
            document = world.model_dump(mode="json")
            if "poison" in deny:
                document["unexpected_private_field"] = deny["poison"]
            return httpx.Response(200, json=[document])
        if request.url.path == "/v1/worlds/private/scopes":
            return httpx.Response(200, json=[scope.model_dump(mode="json")])
        if request.url.path == "/v1/mounts":
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "mode": body["mode"],
                    "results": [],
                    "worlds": [world.model_dump(mode="json")],
                },
            )
        if request.url.path == "/v1/scopes/flow":
            return httpx.Response(200, json={"allowed": deny.get("flow_allowed", True)})
        row = Episode(
            **{**body, "scope_id": world.scope_id},
            id="episode-one",
            world_id="private",
            sequence=1,
            digest="fixture-digest",
            received_at=datetime(2026, 10, 20, tzinfo=UTC),
            actor_id="alice",
            agent_id="agent",
            projection_state="disabled",
        )
        return httpx.Response(200, json={"episode": row.model_dump(mode="json"), "replayed": False})

    class MockHTTPClient(HTTPClient):
        def __init__(self, url, token):
            self.http = httpx.Client(
                base_url=url,
                headers={"Authorization": f"Bearer {token}"},
                transport=httpx.MockTransport(handle),
                trust_env=False,
            )

    monkeypatch.setattr(facade, "Client", MockHTTPClient)
    return requests, deny


async def test_sdk2_discovers_only_scoped_memory_tools(backend):
    server = facade.create_mcp("http://memory.invalid", "fixture-secret")
    async with MCPClient(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "memory_discover",
            "memory_worlds",
            "memory_search",
            "memory_capture",
            "memory_publish",
            "memory_mounts",
            "memory_remote_search",
            "memory_deliveries",
            "memory_scopes",
            "memory_scope_status",
            "memory_context",
            "memory_bind_context",
            "memory_scope_flow",
            "memory_episodes",
        }
        assert "fixture-secret" not in listed.model_dump_json()
        worlds = await client.call_tool("memory_worlds", {})
        assert not worlds.is_error
        assert worlds.structured_content["result"][0]["owner_id"] == "alice"
    assert backend[0][0].headers["authorization"] == "Bearer fixture-secret"


async def test_sdk2_discovery_uses_only_caller_bearer_and_readonly_endpoints(backend):
    async with MCPClient(facade.create_mcp("http://memory.invalid", "user-access-token")) as client:
        result = await client.call_tool("memory_discover", {})
        assert not result.is_error
        assert result.structured_content["worlds"][0]["id"] == "private"
        assert result.structured_content["scopes"]["private"][0]["alias"] == "shared"
        assert result.structured_content["mounts"] == []
        assert "user-access-token" not in result.model_dump_json()
    assert {request.url.path for request in backend[0]} == {
        "/v1/worlds",
        "/v1/worlds/private/scopes",
        "/v1/mounts",
    }
    assert all(request.method == "GET" for request in backend[0])
    assert all(
        request.headers["authorization"] == "Bearer user-access-token" for request in backend[0]
    )


@pytest.mark.parametrize("allowed", [True, False])
async def test_sdk2_scope_flow_uses_typed_selections_and_only_the_callers_credential(
    backend, allowed
):
    backend[1]["flow_allowed"] = allowed
    selected = {
        "world_id": "private",
        "scope_id": world_shared_scope_id("fixture-authority", "private"),
    }
    async with MCPClient(facade.create_mcp("http://memory.invalid", "scoped-user-token")) as client:
        listed = await client.list_tools()
        schema = next(
            tool for tool in listed.tools if tool.name == "memory_scope_flow"
        ).input_schema
        assert schema["properties"]["read_scopes"]["maxItems"] == 50
        assert set(schema["required"]) == {"read_scopes", "write_scope"}
        assert schema["$defs"]["ScopeSelection"]["properties"]["scope_id"]["format"] == "uuid"
        result = await client.call_tool(
            "memory_scope_flow", {"read_scopes": [selected], "write_scope": selected}
        )
        assert not result.is_error
        assert result.structured_content["result"] is allowed
    assert len(backend[0]) == 1
    request = backend[0][0]
    assert request.method == "POST" and request.url.path == "/v1/scopes/flow"
    assert request.headers["authorization"] == "Bearer scoped-user-token"
    assert json.loads(request.content) == {"read_scopes": [selected], "write_scope": selected}


async def test_sdk2_scope_flow_rejects_invalid_ids_without_contacting_the_authority(backend):
    async with MCPClient(facade.create_mcp("http://memory.invalid", "scoped-user-token")) as client:
        result = await client.call_tool(
            "memory_scope_flow",
            {
                "read_scopes": [{"world_id": "private", "scope_id": "@scratch"}],
                "write_scope": {"world_id": "private", "scope_id": "@shared"},
            },
        )
        assert result.is_error
    assert backend[0] == []


async def test_sdk2_scope_flow_revocation_errors_do_not_expose_credentials_or_backend_details(
    backend, caplog
):
    backend[1]["enabled"] = True
    selected = {
        "world_id": "private",
        "scope_id": world_shared_scope_id("fixture-authority", "private"),
    }
    async with MCPClient(facade.create_mcp("http://memory.invalid", "scoped-user-token")) as client:
        result = await client.call_tool(
            "memory_scope_flow",
            {
                "read_scopes": [selected],
                "write_scope": selected,
            },
        )
        assert result.is_error
        assert "scoped-user-token" not in result.model_dump_json()
        assert "private-backend-detail" not in result.model_dump_json()
    assert "scoped-user-token" not in caplog.text
    assert "private-backend-detail" not in caplog.text
    assert [request.url.path for request in backend[0]] == ["/v1/scopes/flow"]


async def test_sdk2_discovery_propagates_revocation_without_catalog_fallback(backend):
    backend[1]["enabled"] = True
    async with MCPClient(facade.create_mcp("http://memory.invalid", "user-access-token")) as client:
        result = await client.call_tool("memory_discover", {})
        assert result.is_error
        assert "private-backend-detail" not in result.model_dump_json()
        assert "user-access-token" not in result.model_dump_json()
    assert [request.url.path for request in backend[0]] == ["/v1/worlds"]


async def test_sdk2_selected_world_discovery_can_explicitly_omit_scopes(backend):
    async with MCPClient(facade.create_mcp("http://memory.invalid", "user-access-token")) as client:
        result = await client.call_tool(
            "memory_discover", {"world": "private", "include_scopes": False}
        )
        assert not result.is_error
        assert [w["id"] for w in result.structured_content["worlds"]] == ["private"]
        assert result.structured_content["scopes"] == {}
    assert {request.url.path for request in backend[0]} == {"/v1/worlds", "/v1/mounts"}


async def test_sdk2_discovery_redacts_validation_diagnostics_from_server_logs(backend, caplog):
    canary = "synthetic-private-upstream-validation-canary"
    backend[1]["poison"] = canary
    async with MCPClient(facade.create_mcp("http://memory.invalid", "user-access-token")) as client:
        result = await client.call_tool("memory_discover", {})
        assert result.is_error
        assert canary not in result.model_dump_json()
    assert "Memory discovery failed" in caplog.text
    assert canary not in caplog.text
    assert "user-access-token" not in caplog.text
    assert [request.url.path for request in backend[0]] == ["/v1/worlds"]


async def test_sdk2_discovery_with_signed_oidc_user_and_live_group_removal(
    oidc_identity, monkeypatch
):
    store, admin, auth, token, _, _ = oidc_identity
    team = store.create_world(admin, CreateWorld(name="Data team", kind=WorldKind.TEAM))
    company = store.create_world(admin, CreateWorld(name="Company", kind=WorldKind.COMPANY))
    hidden = store.create_world(admin, CreateWorld(name="Unrelated team", kind=WorldKind.TEAM))
    scratch = store.create_scope(
        admin,
        team.id,
        CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Operator scratch", alias="secret"),
    )
    store.set_world_alias(admin, team.id, UpdateAlias(alias="data"))
    mapped = OIDCAuthenticator(
        store,
        replace(
            auth.config,
            client_ids=("native-app",),
            required_scopes=("memory-worlds",),
            group_mappings={
                "data": {team.id: Role.WRITER},
                "employees": {company.id: Role.READER},
            },
        ),
    )
    bearer = token(groups=["data", "employees"], cid="native-app", scp=["memory-worlds"])
    requests = []
    with TestClient(create_app(store.data_dir, store=store, auth=mapped)) as api:

        def handle(request):
            requests.append(request)
            response = api.request(
                request.method,
                request.url.path,
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx.Response(response.status_code, content=response.content)

        class AuthorityClient(HTTPClient):
            def __init__(self, url, access_token):
                self.http = httpx.Client(
                    base_url=url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    transport=httpx.MockTransport(handle),
                    trust_env=False,
                )

        monkeypatch.setattr(facade, "Client", AuthorityClient)
        async with MCPClient(facade.create_mcp("http://memory.test", bearer)) as client:
            result = await client.call_tool("memory_discover", {})
            assert not result.is_error
            catalog = result.structured_content
            assert {w["kind"] for w in catalog["worlds"]} == {
                "private-work",
                "team",
                "company",
            }
            assert len(catalog["worlds"]) == 3
            assert hidden.id not in {w["id"] for w in catalog["worlds"]}
            assert {s["id"] for s in catalog["scopes"][team.id]} == {str(team.scope_id)}
            assert scratch.name not in result.model_dump_json()
            assert scratch.alias not in result.model_dump_json()
            assert bearer not in result.model_dump_json()
            assert not mapped.authenticate(bearer).admin
            selected = await client.call_tool("memory_discover", {"world": "@data"})
            assert not selected.is_error
            assert [w["id"] for w in selected.structured_content["worlds"]] == [team.id]
            assert all(request.method == "GET" for request in requests)
            assert all(
                request.headers["authorization"] == f"Bearer {bearer}" for request in requests
            )
            mapped.authenticate(
                token(groups=["employees"], cid="native-app", scp=["memory-worlds"])
            )
            revoked = await client.call_tool("memory_discover", {})
            assert revoked.is_error
            assert team.id not in revoked.model_dump_json()
            assert bearer not in revoked.model_dump_json()


async def test_sdk2_search_passes_explicit_worlds_and_graph_mode(backend):
    async with MCPClient(facade.create_mcp("http://memory.invalid", "fixture")) as client:
        result = await client.call_tool(
            "memory_search",
            {
                "world_ids": ["private"],
                "query": "capacity",
                "graph": True,
                "limit": 3,
            },
        )
        assert not result.is_error
        assert result.structured_content["mode"] == "graph"
    assert json.loads(backend[0][0].content) == {
        "world_ids": ["private"],
        "query": "capacity",
        "limit": 3,
        "mode": "graph",
        "scope_id": None,
        "scopes": [],
        "include_entities": False,
    }


async def test_sdk2_capture_retries_preserve_identity_and_publication_is_narrow(backend):
    payload = {
        "world_id": "private",
        "source_id": "stable-source",
        "content": "Approved text",
        "occurred_at": "2026-10-20T00:00:00Z",
    }
    async with MCPClient(facade.create_mcp("http://memory.invalid", "fixture")) as client:
        first = await client.call_tool("memory_capture", payload)
        repeated = await client.call_tool("memory_capture", payload)
        assert not first.is_error and not repeated.is_error
        published = await client.call_tool("memory_publish", payload)
        assert not published.is_error
    requests = backend[0]
    assert requests[0].url.path == "/v1/worlds/private/episodes"
    assert requests[0].content == requests[1].content
    assert requests[2].url.path == "/v1/worlds/private/publications"
    assert set(json.loads(requests[2].content)) == {
        "context_id",
        "source_id",
        "content",
        "occurred_at",
        "project",
    }


async def test_sdk2_does_not_expose_credentials_or_response_bodies_in_tool_errors(backend):
    backend[1]["enabled"] = True
    async with MCPClient(facade.create_mcp("http://memory.invalid", "fixture-secret")) as client:
        result = await client.call_tool("memory_worlds", {})
        assert result.is_error
        assert "fixture-secret" not in result.model_dump_json()
        assert "private-backend-detail" not in result.model_dump_json()
