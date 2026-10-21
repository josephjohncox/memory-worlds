"""Exercise the SDK 2 protocol against the facade's HTTP contract."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from mcp import Client as MCPClient

from memory_worlds import mcp as facade
from memory_worlds.client import Client as HTTPClient
from memory_worlds.models import Episode, World, WorldKind


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
    )
    deny = {"enabled": False}

    def handle(request):
        requests.append(request)
        if deny["enabled"]:
            return httpx.Response(403, json={"detail": "private-backend-detail"})
        if request.url.path == "/v1/worlds":
            return httpx.Response(200, json=[world.model_dump(mode="json")])
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
        row = Episode(
            **body,
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
            "memory_worlds",
            "memory_search",
            "memory_capture",
            "memory_publish",
            "memory_mounts",
            "memory_remote_search",
            "memory_deliveries",
        }
        assert "fixture-secret" not in listed.model_dump_json()
        worlds = await client.call_tool("memory_worlds", {})
        assert not worlds.is_error
        assert worlds.structured_content["result"][0]["owner_id"] == "alice"
    assert backend[0][0].headers["authorization"] == "Bearer fixture-secret"


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
