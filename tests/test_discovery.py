"""Authorized discovery and explicit label resolution retain canonical identities."""

import json
import socket
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from test_identity import identity as identity

from memory_worlds.api import create_app
from memory_worlds.auth import OIDCAuthenticator
from memory_worlds.client import Client
from memory_worlds.errors import Conflict, NotFound
from memory_worlds.models import (
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    Role,
    ScopeGrant,
    ScopeKind,
    UpdateAlias,
    UpdateScopeGrant,
    WorldKind,
)
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
        result = client.discover(world="@personal", include_scopes=True)
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


@pytest.fixture
def oidc_discovery(identity):
    """A real loopback API and JWKS server; discovery callers never use admin tokens."""
    store, operator, verifier, mint, _, _ = identity
    team = store.create_world(
        operator, CreateWorld(name="Data team", alias="data", kind=WorldKind.TEAM)
    )
    company = store.create_world(
        operator, CreateWorld(name="Company", alias="company", kind=WorldKind.COMPANY)
    )
    hidden = store.create_world(
        operator, CreateWorld(name="Hidden department", alias="hidden", kind=WorldKind.TEAM)
    )
    manual = store.create_world(
        operator, CreateWorld(name="Incident", alias="incident", kind=WorldKind.AD_HOC)
    )
    auth = OIDCAuthenticator(
        store,
        replace(
            verifier.config,
            group_mappings={
                "data": {team.id: Role.WRITER},
                "employees": {company.id: Role.READER},
            },
        ),
    )
    issued_at = time.time() - 30
    auth.reconcile()
    bearer = mint(groups=["data", "employees"], iat=issued_at)
    alice = auth.authenticate(bearer)
    bob = auth.authenticate(mint("bob", groups=["data", "employees"]))
    private = next(w for w in store.list_worlds(alice) if w.kind == WorldKind.PRIVATE_WORK)
    store.set_world_alias(operator, private.id, UpdateAlias(alias="personal"))
    bob_private = next(w for w in store.list_worlds(bob) if w.kind == WorldKind.PRIVATE_WORK)
    own_scope = store.create_scope(
        alice,
        private.id,
        CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Own private notes", alias="notes"),
    )
    sibling_scope = store.create_scope(
        bob,
        team.id,
        CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Sibling secret", alias="sibling"),
    )
    operator_scope = store.create_scope(
        operator,
        team.id,
        CreateScope(kind=ScopeKind.RUN_SHARED, name="Explicit project", alias="project"),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(create_app(store.data_dir, auth=auth), log_level="error", access_log=False)
    )
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "OIDC discovery daemon did not become ready"
        yield SimpleNamespace(**locals())
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        assert not worker.is_alive()


def test_normal_oidc_user_discovers_group_and_manual_worlds_through_api_sdk_and_cli(oidc_discovery):
    from test_cli import _invoke

    e = oidc_discovery
    expected = {e.team.id, e.company.id, e.private.id}
    with Client(e.url, e.bearer) as client:
        me = client.me()
        assert me["admin"] is False and me["restricted"] is False
        assert {w["id"] for w in me["worlds"]} == expected
        found = client.discover(include_scopes=True)
        assert {w.id for w in found["worlds"]} == expected
        assert {s.id for s in found["scopes"][e.team.id]} == {e.team.scope_id}
        assert {s.id for s in found["scopes"][e.private.id]} == {
            e.private.scope_id,
            e.own_scope.id,
        }
        assert client.resolve_world("@data") == e.team.id
        assert client.resolve_world("@company") == e.company.id
        assert client.resolve_scope(e.private.id, "@notes") == e.own_scope.id
        with pytest.raises(NotFound):
            client.resolve_world("@hidden")
        with pytest.raises(NotFound):
            client.resolve_scope(e.team.id, "@sibling")
        assert client.http.get("/v1/users").status_code == 403
        assert client.http.get(f"/v1/worlds/{e.team.id}/memberships").status_code == 403
        e.store.set_membership(e.operator, e.manual.id, e.alice.user_id, Role.READER)
        assert {w.id for w in client.worlds()} == expected | {e.manual.id}
        assert client.resolve_world("@incident") == e.manual.id
        e.store.set_membership(e.operator, e.manual.id, e.alice.user_id, None)
        assert {w.id for w in client.worlds()} == expected
    found = _invoke(e.store.data_dir, "discover", "--scopes", "--json", url=e.url, token=e.bearer)
    assert {w["id"] for w in found["worlds"]} == expected
    assert found["scopes"][e.team.id][0]["id"] == str(e.team.scope_id)
    selected = _invoke(
        e.store.data_dir,
        "discover",
        "--world",
        "@data",
        "--scopes",
        "--json",
        url=e.url,
        token=e.bearer,
    )
    assert [w["id"] for w in selected["worlds"]] == [e.team.id]
    resolved = _invoke(
        e.store.data_dir,
        "resolve",
        "scope",
        "@notes",
        "--world",
        "@personal",
        url=e.url,
        token=e.bearer,
    )
    assert resolved["id"] == str(e.own_scope.id)
    encoded = json.dumps(found)
    for hidden in (
        e.hidden.id,
        e.hidden.name,
        e.bob_private.id,
        e.sibling_scope.name,
        str(e.sibling_scope.id),
        e.operator_scope.name,
        str(e.operator_scope.id),
    ):
        assert hidden not in encoded
    assert e.bearer not in encoded


def test_oidc_explicit_scope_grants_and_delegated_discovery_do_not_expand_audience(oidc_discovery):
    from test_cli import _invoke

    e = oidc_discovery
    e.store.set_projection(e.team.id, "hidden-shared-generation", "ready")
    e.store.set_projection(e.private.id, "hidden-personal-shared-generation", "ready")
    e.store.set_scope_grant(
        e.operator,
        e.team.id,
        e.operator_scope.id,
        UpdateScopeGrant(credential_id=e.alice.credential_id, operations=["read", "delegate"]),
    )
    issued = e.store.delegate_credential(
        e.alice,
        DelegateCredential(
            private_world_id=e.private.id,
            grants=[
                ScopeGrant(world_id=e.team.id, scope_id=e.operator_scope.id, operations=["read"])
            ],
        ),
    )
    with Client(e.url, e.bearer) as client:
        assert (
            next(w for w in client.worlds() if w.id == e.team.id).generation
            == "hidden-shared-generation"
        )
        assert {s.id for s in client.scopes(e.team.id)} == {e.team.scope_id, e.operator_scope.id}
        assert client.resolve_scope(e.team.id, "@project") == e.operator_scope.id
    child_token = issued["token"]
    with Client(e.url, child_token) as child:
        found = child.discover(include_scopes=True)
        assert child.me()["restricted"] is True and child.me()["admin"] is False
        assert {w.id for w in found["worlds"]} == {e.team.id, e.private.id}
        assert {s.id for s in found["scopes"][e.team.id]} == {e.operator_scope.id}
        assert {s.id for s in found["scopes"][e.private.id]} == {issued["private_scope"].id}
        assert all(
            w.generation is None and w.projection_state == "disabled" for w in found["worlds"]
        )
        assert child.http.get(f"/v1/worlds/{e.team.id}/scopes/{e.team.scope_id}").status_code == 403
        assert child.http.get(f"/v1/worlds/{e.company.id}/scopes").status_code == 403
        with pytest.raises(NotFound):
            child.resolve_scope(e.private.id, "@notes")
    found = _invoke(
        e.store.data_dir, "discover", "--scopes", "--json", url=e.url, token=child_token
    )
    assert {s["id"] for s in found["scopes"][e.team.id]} == {str(e.operator_scope.id)}
    encoded = json.dumps(found)
    for hidden in (
        e.sibling_scope.name,
        str(e.sibling_scope.id),
        e.own_scope.name,
        str(e.own_scope.id),
        e.company.name,
        "hidden-shared-generation",
        "hidden-personal-shared-generation",
    ):
        assert hidden not in encoded
    e.store.set_scope_grant(
        e.operator,
        e.team.id,
        e.operator_scope.id,
        UpdateScopeGrant(credential_id=e.alice.credential_id, operations=[]),
    )
    with Client(e.url, child_token) as child:
        assert child.scopes(e.team.id) == []
        assert (
            child.http.get(f"/v1/worlds/{e.team.id}/scopes/{e.operator_scope.id}").status_code
            == 403
        )
    with Client(e.url, e.bearer) as client:
        assert {s.id for s in client.scopes(e.team.id)} == {e.team.scope_id}


def test_oidc_discovery_rechecks_group_removal_old_claims_and_parent_revocation(oidc_discovery):
    from test_cli import _invoke

    e = oidc_discovery
    issued = e.store.delegate_credential(
        e.alice,
        DelegateCredential(
            private_world_id=e.private.id,
            grants=[ScopeGrant(world_id=e.team.id, scope_id=e.team.scope_id, operations=["read"])],
        ),
    )
    child_token = issued["token"]
    with Client(e.url, child_token) as child:
        assert {w.id for w in child.worlds()} == {e.team.id, e.private.id}
    newer = e.mint(groups=["employees"], iat=e.issued_at + 10)
    with Client(e.url, newer) as client:
        assert {w.id for w in client.worlds()} == {e.company.id, e.private.id}
        assert client.http.get(f"/v1/worlds/{e.team.id}/scopes").status_code == 403
        with pytest.raises(NotFound):
            client.resolve_world("@data")
    with Client(e.url, e.bearer) as previous:
        assert previous.http.get("/v1/me").status_code == 401
        assert previous.http.get("/v1/worlds").status_code == 401
    with Client(e.url, child_token) as child:
        found = child.discover(include_scopes=True)
        assert [w.id for w in found["worlds"]] == [e.private.id]
        assert e.team.id not in found["scopes"]
        assert child.http.get(f"/v1/worlds/{e.team.id}/scopes").status_code == 403
    found = _invoke(e.store.data_dir, "discover", "--scopes", "--json", url=e.url, token=newer)
    assert {w["id"] for w in found["worlds"]} == {e.company.id, e.private.id}
    e.store.revoke_token(e.operator, e.alice.credential_id)
    with Client(e.url, child_token) as child:
        assert child.http.get("/v1/me").status_code == 401
        assert child.http.get("/v1/worlds").status_code == 401
    with Client(e.url, newer) as client:
        assert {w.id for w in client.worlds()} == {e.company.id, e.private.id}
