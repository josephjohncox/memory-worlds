from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from memory_worlds.api import create_app
from memory_worlds.locking import DaemonLock
from memory_worlds.models import CreateToken, Role
from memory_worlds.store import MemoryStore


def body(source="test-1", content="The fixture spindle has a ceramic bearing"):
    return {"source_id": source, "content": content, "occurred_at": "2026-10-20T00:00:00Z"}


def test_http_agents_isolation_publication_and_revocation(tmp_path):
    store = MemoryStore(tmp_path)
    world, admin = store.bootstrap("alice")
    owner = store.authenticate(admin)
    agent = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="agent-one", grants={world.id: Role.WRITER})
    )
    with TestClient(create_app(tmp_path)) as api:
        assert api.get("/healthz").status_code == 200
        assert api.get("/v1/worlds").status_code == 401
        auth = {"Authorization": f"Bearer {admin}"}
        token = {"Authorization": f"Bearer {agent}"}
        assert (
            api.post(f"/v1/worlds/{world.id}/episodes", json=body(), headers=token).status_code
            == 200
        )
        found = api.post(
            "/v1/search",
            json={"world_ids": [world.id], "query": "ceramic", "mode": "source"},
            headers=auth,
        )
        assert len(found.json()["results"]) == 1
        bob = api.post(
            "/v1/worlds", json={"name": "Bob private", "owner_id": "bob"}, headers=auth
        ).json()
        assert api.post(
            "/v1/search",
            json={"world_ids": [world.id, bob["id"]], "query": "ceramic", "mode": "source"},
            headers=token,
        ).status_code in (403, 404)
        assert (
            api.post(
                "/v1/credentials",
                json={"user_id": "bob", "agent_id": "theft", "grants": {world.id: "reader"}},
                headers=auth,
            ).status_code
            == 403
        )
        shared = api.post("/v1/worlds", json={"name": "Data", "kind": "team"}, headers=auth).json()
        assert (
            api.post(f"/v1/worlds/{shared['id']}/episodes", json=body(), headers=auth).status_code
            == 403
        )
        published = api.post(
            f"/v1/worlds/{shared['id']}/publications",
            json=body("pub-1", "Approved note"),
            headers=auth,
        )
        assert published.status_code == 200, published.text
        assert published.json()["episode"]["source_ref"] is None
        credential = store.authenticate(agent).credential_id
        assert api.delete(f"/v1/credentials/{credential}", headers=auth).status_code == 204
        assert api.get("/v1/worlds", headers=token).status_code == 401


def test_concurrent_http_capture_and_duplicate_retry(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap("alice")
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(tmp_path)) as api:

        def capture(i):
            return api.post(
                f"/v1/worlds/{world.id}/episodes", json=body(f"entry-{i}"), headers=headers
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(capture, list(range(30)) + list(range(30))))
        assert all(r.status_code == 200 for r in rows), [
            r.text for r in rows if r.status_code != 200
        ]
        assert len({r.json()["episode"]["id"] for r in rows}) == 30
        assert sum(r.json()["replayed"] for r in rows) == 30
    with TestClient(create_app(tmp_path)) as api:
        status = api.get(f"/v1/worlds/{world.id}/status", headers=headers)
        assert status.json()["episodes"] == 30
        conflict = api.post(
            f"/v1/worlds/{world.id}/episodes", json=body("entry-0", "changed"), headers=headers
        )
        assert conflict.status_code == 409


def test_validation_limits_and_graph_unavailability(tmp_path):
    world, token = MemoryStore(tmp_path).bootstrap()
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(create_app(tmp_path)) as api:
        invalid = body(content="secret-fixture-marker")
        invalid["occurred_at"] = "bad"
        response = api.post(f"/v1/worlds/{world.id}/episodes", json=invalid, headers=headers)
        assert response.status_code == 422
        assert "secret-fixture-marker" not in response.text
        assert (
            api.post(
                "/v1/search",
                json={"world_ids": [], "query": "x", "mode": "source"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            api.post(
                "/v1/search",
                json={"world_ids": [world.id], "query": "x", "mode": "graph"},
                headers=headers,
            ).status_code
            == 503
        )
        assert api.post("/v1/search", content="x" * 1_048_577, headers=headers).status_code == 413


def test_single_daemon_lock(tmp_path):
    import pytest

    from memory_worlds.errors import Conflict

    with DaemonLock(tmp_path):
        with pytest.raises(Conflict):
            with DaemonLock(tmp_path):
                pass
