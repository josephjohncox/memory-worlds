import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from memory_worlds.api import create_app
from memory_worlds.store import MemoryStore


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def note(source="source", **kwargs):
    return {
        "source_id": source,
        "content": "private trial evidence",
        "occurred_at": "2026-10-20T00:00:00Z",
        **kwargs,
    }


def test_http_scope_creation_context_delegation_capture_and_selected_return(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap()
    owner = auth(token)
    with TestClient(create_app(tmp_path)) as api:
        shared = api.get(f"/v1/worlds/{world.id}/scopes", headers=owner).json()[0]
        run_response = api.post(
            f"/v1/worlds/{world.id}/scopes",
            json={"kind": "run-shared", "name": "Task"},
            headers=owner,
        )
        assert run_response.status_code == 201, run_response.text
        run = run_response.json()
        assert UUID(run["run_id"])
        bound = api.post(
            "/v1/contexts",
            json={
                "read_scopes": [{"world_id": world.id, "scope_id": shared["id"]}],
                "run_id": run["run_id"],
            },
            headers=owner,
        )
        assert bound.status_code == 201, bound.text
        parent = bound.json()
        delegation = api.post(
            "/v1/credentials/delegate",
            json={
                "private_world_id": world.id,
                "parent_context_id": parent["id"],
                "run_id": run["run_id"],
                "grants": [
                    {"world_id": world.id, "scope_id": run["id"], "operations": ["read", "publish"]}
                ],
            },
            headers=owner,
        )
        assert delegation.status_code == 201, delegation.text
        issued = delegation.json()
        child = auth(issued["token"])
        private = issued["private_scope"]
        flow = api.post(
            "/v1/scopes/flow",
            json={
                "read_scopes": [{"world_id": world.id, "scope_id": run["id"]}],
                "write_scope": {"world_id": world.id, "scope_id": private["id"]},
            },
            headers=child,
        )
        assert flow.status_code == 200, flow.text
        assert flow.json() == {"allowed": True}
        identity = api.get("/v1/me", headers=child).json()
        assert identity["restricted"] is True
        assert identity["parent_invocation_id"] == parent["invocation_id"]
        assert identity["invocation_id"] == private["invocation_id"]
        base = f"/v1/worlds/{world.id}/scopes/{private['id']}"
        captured = api.post(base + "/episodes", json=note(), headers=child)
        assert captured.status_code == 200, captured.text
        episode = captured.json()["episode"]
        assert episode["scope_id"] == private["id"]
        assert (
            api.get(base + "/episodes/" + episode["id"], headers=child).json()["content"]
            == episode["content"]
        )
        assert api.get(base + "/status", headers=child).json()["episodes"] == 1
        assert api.get(base + "/snapshot", headers=child).json()["scope"]["id"] == private["id"]
        assert api.get(base + "/episodes", headers=owner).status_code == 403
        assert api.get(f"/v1/worlds/{world.id}/episodes", headers=child).status_code == 403
        assert api.get(f"/v1/worlds/{world.id}/status", headers=child).status_code == 403
        assert api.get(f"/v1/contexts/{issued['context']['id']}", headers=child).status_code == 200
        publication = api.post(
            f"/v1/worlds/{world.id}/scopes/{run['id']}/publications",
            json={
                **note("result"),
                "content": "selected result",
                "source_scope": {"world_id": world.id, "scope_id": private["id"]},
                "source_episode_id": episode["id"],
            },
            headers=child,
        )
        assert publication.status_code == 200, publication.text
        assert publication.json()["episode"]["id"] != episode["id"]
        shared_results = api.post(
            "/v1/search",
            json={"scopes": [{"world_id": world.id, "scope_id": run["id"]}], "query": "selected"},
            headers=owner,
        )
        assert shared_results.status_code == 200, shared_results.text
        assert len(shared_results.json()["results"]) == 1
        assert (
            api.delete(f"/v1/credentials/{issued['credential_id']}", headers=owner).status_code
            == 204
        )
        assert api.get(base + "/episodes", headers=child).status_code == 401


def test_http_scope_uuid_mismatch_and_entire_selection_authorization(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap()
    owner = auth(token)
    with TestClient(create_app(tmp_path)) as api:
        one = api.post(
            "/v1/credentials/delegate", json={"private_world_id": world.id}, headers=owner
        ).json()
        two = api.post(
            "/v1/credentials/delegate", json={"private_world_id": world.id}, headers=owner
        ).json()
        child = auth(one["token"])
        sid = one["private_scope"]["id"]
        base = f"/v1/worlds/{world.id}/scopes/{sid}"
        assert (
            api.post(
                base + "/episodes", json=note(scope_id=two["private_scope"]["id"]), headers=child
            ).status_code
            == 403
        )
        assert api.get(f"/v1/worlds/{world.id}/scopes/not-a-uuid", headers=owner).status_code == 422
        assert (
            api.post(
                "/v1/search",
                json={
                    "scopes": [
                        {"world_id": world.id, "scope_id": sid},
                        {"world_id": world.id, "scope_id": two["private_scope"]["id"]},
                    ],
                    "query": "trial",
                },
                headers=child,
            ).status_code
            == 403
        )
        assert api.post("/v1/search", json={"query": "trial"}, headers=child).status_code == 422
        assert (
            api.post(
                "/v1/search",
                json={
                    "world_ids": [world.id],
                    "scope_id": sid,
                    "scopes": [{"world_id": world.id, "scope_id": sid}],
                    "query": "trial",
                },
                headers=child,
            ).status_code
            == 422
        )
        # Agent invocation cannot mutate its capabilities or manufacture an admin token.
        assert (
            api.put(
                base + "/grants",
                json={"credential_id": one["credential_id"], "operations": ["manage"]},
                headers=child,
            ).status_code
            == 403
        )
        assert (
            api.post(
                "/v1/credentials",
                json={"user_id": "owner", "agent_id": "escape", "admin": True},
                headers=child,
            ).status_code
            == 403
        )
        assert api.get(base + "/episodes?limit=1001", headers=child).status_code == 400


def test_http_explicit_root_scope_snapshot_preserves_scope_identity(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap()
    owner = auth(token)
    with TestClient(create_app(tmp_path)) as api:
        shared = api.get(f"/v1/worlds/{world.id}/scopes", headers=owner).json()[0]
        snap = api.get(f"/v1/worlds/{world.id}/scopes/{shared['id']}/snapshot", headers=owner)
        assert snap.status_code == 200
        assert snap.json()["scope"]["id"] == shared["id"]
        assert snap.json()["world"]["scope_id"] == shared["id"]
        assert (
            api.post("/v1/contexts", json={"run_id": str(uuid4())}, headers=owner).status_code
            == 403
        )


def test_http_default_and_explicit_world_shared_views_use_same_canonical_scope(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap()
    owner = auth(token)
    with TestClient(create_app(tmp_path)) as api:
        sid = api.get(f"/v1/worlds/{world.id}/scopes", headers=owner).json()[0]["id"]
        default = api.post(f"/v1/worlds/{world.id}/episodes", json=note(), headers=owner).json()[
            "episode"
        ]
        base = f"/v1/worlds/{world.id}/scopes/{sid}"
        scoped = api.post(base + "/episodes", json=note(), headers=owner).json()
        assert scoped["replayed"]
        assert scoped["episode"]["scope_id"] == sid
        assert scoped["episode"] == default
        assert api.get(base + "/episodes", headers=owner).json()[0]["scope_id"] == sid
        assert api.get(base + "/episodes/" + default["id"], headers=owner).json()["scope_id"] == sid
        assert api.get(base + "/snapshot", headers=owner).json()["episodes"][0]["scope_id"] == sid
        search = api.post(
            "/v1/search",
            json={"scopes": [{"world_id": world.id, "scope_id": sid}], "query": "trial"},
            headers=owner,
        )
        assert search.json()["results"][0]["scope_id"] == sid
        explicit_world_route = api.post(
            f"/v1/worlds/{world.id}/episodes", json=note("explicit", scope_id=sid), headers=owner
        )
        assert explicit_world_route.json()["episode"]["scope_id"] == sid
        published = api.post(base + "/publications", json=note("published"), headers=owner)
        assert published.status_code == 200, published.text
        assert published.json()["episode"]["scope_id"] == sid
        assert (
            api.get(f"/v1/worlds/{world.id}/episodes/{default['id']}", headers=owner).json()
            == default
        )
    with store._world_db(world.id) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM episodes WHERE id=?", (default["id"],)).fetchone()[0]
        )
    assert payload["scope_id"] == sid
    assert payload["context_id"] is None
