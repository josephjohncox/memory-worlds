"""Authorized discovery and explicit label resolution retain canonical identities."""

from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from memory_worlds.api import create_app
from memory_worlds.client import Client
from memory_worlds.errors import Conflict, NotFound
from memory_worlds.models import CreateScope, CreateToken, CreateWorld, Role, ScopeKind
from memory_worlds.store import MemoryStore


@pytest.fixture
def authority(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    with TestClient(create_app(tmp_path)) as api:
        yield store, world, owner, token, api


def client_for(api, token):
    client = Client("http://memory.test", token)
    client.http.close()
    client.http = httpx.Client(
        base_url="http://memory.test",
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                (
                    response := api.request(
                        r.method, r.url.path, content=r.content, headers=dict(r.headers)
                    )
                ).status_code,
                content=response.content,
            )
        ),
    )
    return client


def test_aliases_discover_and_resolve_without_rewriting_source(authority):
    store, world, owner, token, api = authority
    headers = {"Authorization": f"Bearer {token}"}
    first = api.post(
        f"/v1/worlds/{world.id}/episodes",
        headers=headers,
        json={
            "source_id": "evidence",
            "content": "Bearing inspection",
            "occurred_at": "2026-10-20T00:00:00Z",
            "project": False,
        },
    ).json()["episode"]
    assert (
        api.put(
            f"/v1/worlds/{world.id}/alias", headers=headers, json={"alias": "personal"}
        ).status_code
        == 200
    )
    private = store.create_scope(
        owner,
        world.id,
        CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Inspection notes", alias="notes"),
    )
    with client_for(api, token) as client:
        assert client.resolve_world("@personal") == world.id
        assert client.resolve_scope(world.id, "@notes") == private.id
        result = client.discover(world="@personal")
        assert result["worlds"][0].name == "Personal"
        assert {scope.alias for scope in result["scopes"][world.id]} == {"shared", "notes"}
        current = client.get_episode(world.id, first["id"])
        assert current.model_dump(mode="json") == first
    assert (
        api.put(
            f"/v1/worlds/{world.id}/alias", headers=headers, json={"alias": "Bad Alias"}
        ).status_code
        == 422
    )


def test_resolution_only_uses_visible_objects_and_rejects_ambiguity(authority):
    store, world, owner, token, api = authority
    other = store.create_world(owner, CreateWorld(name="Hidden", alias="same", owner_id="bob"))
    store.create_world(owner, CreateWorld(name="Also visible", alias="same"))
    reader = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="reader", grants={world.id: Role.READER})
    )
    with client_for(api, reader) as client:
        with pytest.raises(NotFound):
            client.resolve_world("@same")
        assert other.id not in {w.id for w in client.discover()["worlds"]}
    with client_for(api, token) as client:
        with pytest.raises(Conflict, match="Ambiguous"):
            client.resolve_world("@same")


def test_private_aliases_do_not_grant_access_or_expose_hidden_names(authority):
    store, world, owner, token, api = authority
    private = store.create_scope(
        owner,
        world.id,
        CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Private", alias="scratch"),
    )
    reader_token = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="reader", grants={world.id: Role.READER})
    )
    with client_for(api, reader_token) as client:
        with pytest.raises(NotFound):
            client.resolve_scope(world.id, "@scratch")
        assert private.id not in {
            s.id for s in client.discover(include_scopes=True)["scopes"][world.id]
        }
    headers = {"Authorization": f"Bearer {reader_token}"}
    assert (
        api.put(
            f"/v1/worlds/{world.id}/alias", headers=headers, json={"alias": "stolen"}
        ).status_code
        == 403
    )

    assert (
        api.put(
            f"/v1/worlds/{world.id}/scopes/{private.id}/alias",
            headers=headers,
            json={"alias": "stolen"},
        ).status_code
        == 403
    )


def test_actual_cli_discovery_and_alias_capture_use_canonical_ids(tmp_path):
    from test_cli import _daemon, _invoke

    from memory_worlds.models import UpdateAlias

    store = MemoryStore(tmp_path)
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    store.set_world_alias(owner, world.id, UpdateAlias(alias="personal"))
    scope = store.create_scope(
        owner, world.id, CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Notes", alias="notes")
    )
    with _daemon(tmp_path) as url:
        found = _invoke(
            tmp_path, "discover", "--world", "@personal", "--json", url=url, token=token
        )
        assert found["worlds"][0]["id"] == world.id
        assert scope.id in {UUID(s["id"]) for s in found["scopes"][world.id]}
        resolved = _invoke(
            tmp_path, "resolve", "scope", "@notes", "--world", "@personal", url=url, token=token
        )
        assert resolved["id"] == str(scope.id)
        receipt = _invoke(
            tmp_path,
            "capture",
            "@personal",
            "--scope",
            "@notes",
            "--source-only",
            "--source-id",
            "cli-alias",
            "--at",
            "2026-10-20T00:00:00Z",
            "--text",
            "Selected inspection evidence",
            url=url,
            token=token,
        )
        assert receipt["episode"]["scope_id"] == str(scope.id)
        source = _invoke(
            tmp_path,
            "search",
            "inspection",
            "--source",
            "--select",
            "@personal:@notes",
            url=url,
            token=token,
        )
        assert source["results"][0]["id"] == receipt["episode"]["id"]
