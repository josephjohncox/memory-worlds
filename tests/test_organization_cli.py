"""Offline organization inspection and startup authorization through the real API."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_identity import identity as identity

from memory_worlds import cli
from memory_worlds.api import create_app
from memory_worlds.client import Client
from memory_worlds.models import CreateScope, Role, ScopeKind
from memory_worlds.organization import load_organization
from memory_worlds.store import MemoryStore


def policy_document(issuer="https://identity.example.test", *, include_data_group=True):
    return f"""[identity]
issuer = {json.dumps(issuer)}
audience = "memory-api"
jwks_url = {json.dumps(issuer + "/jwks")}
[spaces.company]
name = "Company"
kind = "company"
[spaces.data]
name = "Data team"
kind = "team"
[spaces.incident]
name = "Incident"
kind = "ad-hoc"
[roles.employee]
worlds = {{company = "reader"}}
[roles.data-member]
worlds = {{data = "writer"}}
[groups]
employees = ["employee"]
""" + ('data = ["employee", "data-member"]\n' if include_data_group else "")


@pytest.mark.parametrize("action", ["validate", "plan"])
def test_cli_organization_commands_require_neither_token_storage_nor_network(
    tmp_path, monkeypatch, capsys, action
):
    path = tmp_path / "organization.toml"
    path.write_text(policy_document())

    def forbidden(*args, **kwargs):
        raise AssertionError("organization inspection must be offline and must not read a token")

    monkeypatch.setattr(MemoryStore, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(cli, "read_token", forbidden)
    cli.run(
        cli.parser().parse_args(
            [
                "--data-dir",
                str(tmp_path / "must-not-exist"),
                "--token-file",
                str(tmp_path / "no-token"),
                "organization",
                action,
                "--config",
                str(path),
            ]
        )
    )
    result = json.loads(capsys.readouterr().out)
    if action == "validate":
        assert result == {"valid": True, "spaces": 3, "roles": 2, "groups": 2}
    else:
        assert result["group_world_roles"]["data"] == {"company": "reader", "data": "writer"}
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize("action", ["validate", "plan"])
def test_installed_cli_resolves_relative_policy_path_from_the_calling_directory(tmp_path, action):
    (tmp_path / "organization.toml").write_text(policy_document())
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MEMORY_WORLDS_")
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "memory_worlds.cli",
            "--data-dir",
            str(tmp_path / "unused"),
            "organization",
            action,
            "--config",
            "organization.toml",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    json.loads(completed.stdout)
    assert not (tmp_path / "unused").exists()


def test_invalid_policy_cli_exits_before_creating_data_and_does_not_echo_input(tmp_path):
    path = tmp_path / "organization.toml"
    path.write_text('password = "DO-NOT-PRINT-THIS"')
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "memory_worlds.cli",
            "--data-dir",
            str(tmp_path / "unused"),
            "organization",
            "validate",
            "--config",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 1
    assert "organization TOML" in completed.stderr
    assert "DO-NOT-PRINT-THIS" not in completed.stderr
    assert completed.stdout == ""
    assert not (tmp_path / "unused").exists()


def test_kind_qualification_bootstrap_and_daemon_use_the_same_declarative_spaces(
    tmp_path, monkeypatch
):
    import uvicorn

    path = Path(__file__).resolve().parents[1] / "deploy/qualification/runtime.py"
    specification = importlib.util.spec_from_file_location("qualification_runtime", path)
    fixture = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixture)
    fixture.directory = tmp_path / "state"
    fixture.receipt = tmp_path / "qualification.json"
    fixture.organization_path = tmp_path / "organization.toml"
    fixture.bootstrap()
    receipt = json.loads(fixture.receipt.read_text())
    assert set(receipt["worlds"]) == {"company", "data"}
    for key in tuple(os.environ):
        if key.startswith("MEMORY_WORLDS_OIDC_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("QUALIFICATION_ISSUER", "http://127.0.0.1:18080/realms/memory-worlds")
    monkeypatch.setenv("MEMORY_WORLDS_ORGANIZATION_FILE", "overridden-by-fixture")
    monkeypatch.setenv("MEMORY_WORLDS_GRAPH_BACKEND", "disabled")
    started = []

    def start(app, **kwargs):
        with TestClient(app) as api:
            assert api.get("/readyz").status_code == 200
        started.append(kwargs)

    monkeypatch.setattr(uvicorn, "run", start)
    fixture.serve()
    assert started[0]["port"] == 8787
    config = load_organization(fixture.organization_path)
    assert config.identity.client_id_claim == "azp"
    assert config.identity.scopes_claim == "scope"
    assert config.identity.client_ids == ["mw-native", "mw-wrong-audience"]
    store = MemoryStore(fixture.directory)
    mappings = config.reconcile(store)
    assert mappings == {
        "mw-company": {receipt["worlds"]["company"]["id"]: Role.READER},
        "mw-data-team": {receipt["worlds"]["data"]["id"]: Role.WRITER},
    }
    assert not any(key.startswith("MEMORY_WORLDS_OIDC_") for key in os.environ)


def service_client(api, token):
    def dispatch(request):
        response = api.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    client = Client("http://service", token)
    client.http.close()
    client.http = httpx.Client(
        base_url="http://service",
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(dispatch),
    )
    return client


def test_startup_provisions_spaces_and_normal_tokens_discover_only_authorized_audiences(
    identity, monkeypatch, tmp_path
):
    store, operator, verifier, mint, _, _ = identity
    for key in tuple(os.environ):
        if key.startswith("MEMORY_WORLDS_OIDC_"):
            monkeypatch.delenv(key)
    path = tmp_path / "organization.toml"
    path.write_text(policy_document(verifier.config.issuer))
    monkeypatch.setenv("MEMORY_WORLDS_ORGANIZATION_FILE", str(path))
    config = load_organization(path)
    data_id = config.space_id(store.authority_id, "data")
    company_id = config.space_id(store.authority_id, "company")
    incident_id = config.space_id(store.authority_id, "incident")
    assert len(store.list_worlds(operator)) == 1
    alice_token = mint(groups=["data", "employees"], iat=time.time() - 10)
    bob_token = mint("bob", groups=["data", "employees"])
    body = {
        "source_id": "finding",
        "content": "An approved finding",
        "occurred_at": "2026-10-21T00:00:00Z",
    }
    with TestClient(create_app(store.data_dir)) as api:
        assert api.get("/readyz").status_code == 200
        with service_client(api, alice_token) as alice:
            catalog = alice.discover(include_scopes=True)
            alice_id = alice.me()["user_id"]
            alice_worlds = {world.kind.value: world for world in catalog["worlds"]}
            assert set(alice_worlds) == {"private-work", "company", "team"}
            assert alice_worlds["team"].id == data_id
            assert not alice.me()["admin"]
            assert [scope.kind.value for scope in catalog["scopes"][data_id]] == ["world-shared"]
        headers = {"Authorization": f"Bearer {alice_token}"}
        assert api.get("/v1/users", headers=headers).status_code == 403
        assert (
            api.post(
                f"/v1/worlds/{company_id}/publications", json=body, headers=headers
            ).status_code
            == 403
        )
        published = api.post(f"/v1/worlds/{data_id}/publications", json=body, headers=headers)
        assert published.status_code == 200, published.text
        episode_id = published.json()["episode"]["id"]
        # Broader team membership cannot disclose another invocation or an
        # operator-created run scope.
        bob_headers = {"Authorization": f"Bearer {bob_token}"}
        bob_scope = api.post(
            f"/v1/worlds/{data_id}/scopes",
            headers=bob_headers,
            json={"kind": "invocation-private", "name": "Bob scratch"},
        )
        assert bob_scope.status_code == 201, bob_scope.text
        run_scope = store.create_scope(
            operator, data_id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Operator project")
        )
        for scope in (bob_scope.json()["id"], str(run_scope.id)):
            assert (
                api.get(f"/v1/worlds/{data_id}/scopes/{scope}", headers=headers).status_code == 403
            )
        delegated = api.post(
            "/v1/credentials/delegate",
            headers=headers,
            json={
                "grants": [
                    {
                        "world_id": data_id,
                        "scope_id": str(alice_worlds["team"].scope_id),
                        "operations": ["read"],
                    }
                ]
            },
        )
        assert delegated.status_code == 201, delegated.text
        child_headers = {"Authorization": "Bearer " + delegated.json()["token"]}
        assert api.get(f"/v1/worlds/{data_id}/scopes", headers=child_headers).status_code == 200
        store.set_membership(operator, incident_id, alice_id, Role.READER)

    # A second startup uses the changed file, clears old mapped access before
    # serving requests, preserves manual membership and retains the source data.
    path.write_text(policy_document(verifier.config.issuer, include_data_group=False))
    with TestClient(create_app(store.data_dir)) as api:
        assert api.get(f"/v1/worlds/{data_id}/scopes", headers=child_headers).status_code == 403
        with service_client(api, alice_token) as alice:
            catalog = alice.discover(include_scopes=True)
            assert {world.kind.value for world in catalog["worlds"]} == {
                "private-work",
                "company",
                "ad-hoc",
            }
        assert api.get(f"/v1/worlds/{data_id}/scopes", headers=headers).status_code == 403
        assert store.get_episode(operator, data_id, episode_id).content == "An approved finding"
