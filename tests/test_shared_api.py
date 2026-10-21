import asyncio

from fastapi.testclient import TestClient
from test_identity import identity as identity

from memory_worlds.api import create_app
from memory_worlds.models import CreateToken, CreateWorld, Role, WorldKind
from memory_worlds.store import MemoryStore


def authorization(token):
    return {"Authorization": f"Bearer {token}"}


def test_real_oidc_http_private_context_membership_and_disable(identity):
    store, operator, auth, token, _, _ = identity
    bearer = token()
    alice = auth.authenticate(bearer)
    team = store.create_world(operator, CreateWorld(name="Data", kind=WorldKind.TEAM))
    admin_token = store.issue_token(
        operator, CreateToken(user_id=operator.user_id, agent_id="http-admin", admin=True)
    )
    admin = authorization(admin_token)
    alice_headers = authorization(bearer)
    with TestClient(create_app(store.data_dir, auth=auth)) as api:
        assert api.get("/readyz").status_code == 200
        assert api.get("/v1/me", headers={"X-User": "operator"}).status_code == 401
        identity_view = api.get("/v1/me", headers=alice_headers).json()
        assert identity_view["user_id"] == alice.user_id
        assert identity_view["authority_id"] == store.authority_id
        assert identity_view["admin"] is False
        private = identity_view["worlds"][0]
        assert private["kind"] == "private-work"
        assert bearer not in str(identity_view)
        assert api.get("/v1/users", headers=alice_headers).status_code == 403
        assert (
            api.put(
                f"/v1/worlds/{team.id}/memberships/{alice.user_id}",
                headers=alice_headers,
                json={"role": "writer"},
            ).status_code
            == 403
        )
        assert (
            api.put(
                f"/v1/worlds/{team.id}/memberships/{alice.user_id}",
                headers=admin,
                json={"role": "writer"},
            ).status_code
            == 204
        )
        selected = api.get("/v1/worlds", headers=alice_headers).json()
        assert {world["id"] for world in selected} == {private["id"], team.id}
        payload = {
            "source_id": "reviewed",
            "content": "Selected team note",
            "occurred_at": "2026-10-20T12:00:00Z",
        }
        assert (
            api.post(
                f"/v1/worlds/{team.id}/episodes", headers=alice_headers, json=payload
            ).status_code
            == 403
        )
        assert (
            api.post(
                f"/v1/worlds/{team.id}/publications", headers=alice_headers, json=payload
            ).status_code
            == 200
        )
        assert (
            api.put(
                f"/v1/worlds/{team.id}/memberships/{alice.user_id}",
                headers=admin,
                json={"role": "reader"},
            ).status_code
            == 204
        )
        assert (
            api.post(
                f"/v1/worlds/{team.id}/publications", headers=alice_headers, json=payload
            ).status_code
            == 403
        )
        assert (
            api.get(f"/v1/worlds/{team.id}/memberships", headers=admin).json()[0]["role"]
            == Role.READER
        )
        assert (
            api.patch(
                f"/v1/users/{alice.user_id}", headers=admin, json={"enabled": False}
            ).status_code
            == 204
        )
        assert api.get("/v1/me", headers=alice_headers).status_code == 401
        assert len(api.get("/v1/users", headers=admin).json()) == 1


def test_readiness_detects_worker_exit_without_breaking_health(tmp_path):
    class FailedWorker:
        closed = False

        async def run(self):
            await asyncio.sleep(0)

        async def close(self):
            self.closed = True

    graph = FailedWorker()
    app = create_app(tmp_path, graph)
    assert app.state.started is False
    with TestClient(app) as api:
        assert api.get("/healthz").status_code == 200
        assert api.get("/readyz").status_code == 503
    assert graph.closed and not app.state.started


def test_router_routes_share_auth_and_empty_config_is_explicit(tmp_path):
    store = MemoryStore(tmp_path)
    world, token = store.bootstrap()
    auth = authorization(token)
    with TestClient(create_app(tmp_path)) as api:
        assert api.get("/v1/mounts").status_code == 401
        assert api.get("/v1/mounts", headers=auth).json() == []
        assert api.get("/v1/router/deliveries", headers=auth).json() == []
        assert (
            api.post(
                "/v1/router/search",
                headers=auth,
                json={"mount_ids": ["unknown"], "query": "ceramic"},
            ).status_code
            == 404
        )
        response = api.post(
            "/v1/router/publications",
            headers=auth,
            json={
                "destination_mount_id": "unknown",
                "world_id": world.id,
                "source_id": "publication",
                "content": "Explicit selection",
                "occurred_at": "2026-10-20T00:00:00Z",
            },
        )
        assert response.status_code == 404
