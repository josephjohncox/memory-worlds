from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_worlds.api import create_app
from memory_worlds.models import CreateToken, EpisodeInput, Role
from memory_worlds.operations import BackupService
from memory_worlds.recovery import restore_authority
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore


@pytest.fixture
def operations(tmp_path):
    store = MemoryStore(tmp_path / "authority")
    world, token = store.bootstrap("alice")
    owner = store.authenticate(token)
    reader = store.issue_token(
        owner, CreateToken(user_id="alice", agent_id="reader", grants={world.id: Role.READER})
    )
    key = tmp_path / "key"
    generate_key(key)
    backups = BackupService(store, tmp_path / "backups", key)
    with TestClient(create_app(store.data_dir, store=store, backups=backups)) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, store, world, owner, reader, key


def test_online_encrypted_backup_covers_private_authority_and_downloads_exact_bytes(
    operations, tmp_path
):
    client, store, world, owner, reader, key = operations
    episode = store.capture(
        owner,
        world.id,
        EpisodeInput(
            source_id="private", content="secret source body", occurred_at=datetime.now(UTC)
        ),
    )
    response = client.post("/v1/authority/backups", json={"graph_mode": "rebuild"})
    assert response.status_code == 201
    receipt = response.json()
    assert "secret" not in response.text and receipt["graph_mode"] == "rebuild"
    path = f"/v1/authority/backups/{receipt['artifact_id']}"
    artifact = client.get(path)
    assert artifact.status_code == 200
    assert artifact.headers["cache-control"] == "no-store"
    assert len(artifact.content) == receipt["bytes"]
    assert b"secret source body" not in artifact.content
    archive = tmp_path / "download.mwauthority"
    archive.write_bytes(artifact.content)
    restore_authority(archive, key, tmp_path / "fresh")
    restored = MemoryStore(tmp_path / "fresh")
    assert restored.get_episode(owner, world.id, episode.episode.id).content == "secret source body"
    assert client.get("/v1/authority/backups").json()[0]["artifact_id"] == receipt["artifact_id"]
    for method, route, body in (
        ("GET", path, None),
        ("GET", "/v1/authority/backups", None),
        ("POST", "/v1/authority/backups", {"graph_mode": "rebuild"}),
    ):
        response = client.request(
            method, route, json=body, headers={"Authorization": f"Bearer {reader}"}
        )
        assert response.status_code == 403


def test_lifecycle_api_and_receipts_block_reimport_without_disclosing_source(operations):
    client, store, world, owner, reader, _ = operations
    source = EpisodeInput(
        source_id="remove", content="sensitive body", occurred_at=datetime.now(UTC)
    )
    episode = store.capture(owner, world.id, source).episode
    path = f"/v1/worlds/{world.id}/scopes/{world.scope_id}/lifecycle"
    request = {"operation_id": str(uuid4()), "action": "erase", "episode_ids": [episode.id]}
    assert client.post(path + "/preview", json=request).status_code == 200
    assert (
        client.post(path, json=request, headers={"Authorization": f"Bearer {reader}"}).status_code
        == 403
    )
    response = client.post(path, json=request)
    assert response.status_code == 200
    assert "sensitive body" not in response.text
    assert response.json()["erasure_complete"]
    assert client.get(path + "/" + request["operation_id"]).json() == response.json()
    assert (
        client.post(
            f"/v1/worlds/{world.id}/episodes", json=source.model_dump(mode="json")
        ).status_code
        == 409
    )


def test_metrics_require_admin_and_have_bounded_content_free_labels(operations):
    client, store, world, owner, reader, _ = operations
    store.capture(
        owner,
        world.id,
        EpisodeInput(
            source_id="private-metric",
            content="secret metric content",
            occurred_at=datetime.now(UTC),
        ),
    )
    assert client.get("/metrics", headers={"Authorization": f"Bearer {reader}"}).status_code == 403
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "memory_worlds_ready 1" in response.text
    assert 'memory_worlds_records{kind="projection",state="pending"} 1' in response.text
    assert world.id not in response.text and owner.user_id not in response.text
    assert "private-metric" not in response.text and "secret metric content" not in response.text
    assert "memory_worlds_http_duration_seconds_bucket" in response.text


def test_named_identity_http_routes_resolve_alias_without_uuid_route_shadowing(operations):
    client, _, _, _, _, _ = operations
    result = client.post("/v1/identities/agent", json={"name": "Researcher", "alias": "research"})
    assert result.status_code == 201
    identity = result.json()
    assert (
        client.get("/v1/identities/agent/resolve", params={"reference": "@research"}).json()
        == identity
    )
    assert client.get(f"/v1/identities/agent/{identity['id']}").json() == identity
    assert client.get("/v1/identities/invalid").status_code == 422


def test_readiness_rejects_unavailable_durable_database_even_when_workers_are_running(
    operations, monkeypatch
):
    from contextlib import contextmanager

    from memory_worlds.errors import Unavailable

    client, store, _, _, _, _ = operations

    @contextmanager
    def unavailable():
        raise Unavailable("database fixture unavailable")
        yield

    monkeypatch.setattr(store, "_control", unavailable)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
