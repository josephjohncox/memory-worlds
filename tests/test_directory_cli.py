"""Directory CLI planning and authenticated private imports through the actual API."""

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from memory_worlds import cli, directory_import
from memory_worlds.api import create_app
from memory_worlds.client import Client
from memory_worlds.models import CreateScope, EpisodeInput, ScopeKind, UpdateAlias
from memory_worlds.mounts import MountConfig
from memory_worlds.portability import PortabilityError
from memory_worlds.store import MemoryStore

AT = "2026-10-20T12:00:00Z"
SCOPE = UUID("00000000-0000-4000-8000-000000000001")


def write_chat(folder, identity="synthetic-chat", *, content="Private synthetic fixture note"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{identity}.json"
    path.write_text(
        json.dumps(
            {
                "id": identity,
                "title": "Synthetic archive",
                "create_time": 1_761_048_000,
                "current_node": "answer",
                "mapping": {
                    "question": {
                        "id": "question",
                        "parent": None,
                        "children": ["answer"],
                        "message": {
                            "id": "question",
                            "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["Inspect fixture"]},
                            "create_time": 1_761_048_000,
                        },
                    },
                    "answer": {
                        "id": "answer",
                        "parent": "question",
                        "children": [],
                        "message": {
                            "id": "answer",
                            "author": {"role": "assistant"},
                            "content": {"content_type": "text", "parts": [content]},
                            "create_time": 1_761_048_001,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def invoke(arguments, capsys):
    cli.run(cli.parser().parse_args(arguments))
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out), captured.out


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("MEMORY_WORLDS_"):
            monkeypatch.delenv(key)
    store = MemoryStore(tmp_path / "service-data")
    world, token = store.bootstrap("archive-owner")
    owner = store.authenticate(token)
    world = store.set_world_alias(owner, world.id, UpdateAlias(alias="personal"))
    private = store.create_scope(
        owner,
        world.id,
        CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Chat archive", alias="history"),
    )
    requests = []
    with TestClient(create_app(store.data_dir, mount_config=MountConfig())) as api:

        class ServiceClient(Client):
            def __init__(self, url, token):
                def dispatch(request):
                    requests.append(request)
                    response = api.request(
                        request.method,
                        request.url.path,
                        headers=dict(request.headers),
                        content=request.content,
                    )
                    return httpx.Response(
                        response.status_code,
                        headers=dict(response.headers),
                        content=response.content,
                        request=request,
                    )

                self.http = httpx.Client(
                    base_url=url,
                    headers={"Authorization": f"Bearer {token}"},
                    transport=httpx.MockTransport(dispatch),
                    trust_env=False,
                )

        monkeypatch.setattr(cli, "Client", ServiceClient)
        monkeypatch.setenv("MEMORY_WORLDS_TOKEN", token)
        yield SimpleNamespace(
            store=store, world=world, scope=private, owner=owner, token=token, requests=requests
        )


def test_directory_command_requires_explicit_valid_scope(capsys):
    for arguments in (
        ["import-directory", "world", "archive"],
        ["import-directory", "world", "archive", "--scope", "not-a-scope"],
        ["import-directory", "world", "archive", "--scope", "@history", "--format", "guessed"],
    ):
        with pytest.raises(SystemExit) as error:
            cli.parser().parse_args(arguments)
        assert error.value.code == 2
        capsys.readouterr()


def test_directory_preview_has_no_credentials_network_or_local_database(
    tmp_path, monkeypatch, capsys
):
    folder = tmp_path / "archives"
    write_chat(folder)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Preview must not read credentials, contact HTTP or open local state")

    monkeypatch.setattr(cli, "read_token", forbidden)
    monkeypatch.setattr(cli, "Client", forbidden)
    monkeypatch.setattr(cli, "local_store", forbidden)
    data_dir = tmp_path / "must-not-be-created"
    report, output = invoke(
        [
            "--data-dir",
            str(data_dir),
            "--token-file",
            str(tmp_path / "absent.token"),
            "import-directory",
            "@personal",
            str(folder),
            "--scope",
            "@history",
            "--dry-run",
        ],
        capsys,
    )
    assert report["dry_run"] is True and report["project"] is True
    assert report["destination"] == {"world_id": "@personal", "scope_id": "@history"}
    assert report["episodes"] == 2 and report["selected_files"] == 1
    assert report["files"][0]["path"] == "synthetic-chat.json"
    assert "Private synthetic fixture note" not in output and str(tmp_path) not in output
    assert not data_dir.exists()


def test_directory_preview_forwards_explicit_parser_options(tmp_path, monkeypatch, capsys):
    calls = []
    episode = EpisodeInput(
        source_id="synthetic", content="Do not print this", occurred_at=datetime.now(UTC)
    )

    def plan(folder, **kwargs):
        calls.append((folder, kwargs))
        return directory_import.ImportPlan(episodes=[episode], report={"episodes": 1})

    monkeypatch.setattr(directory_import, "plan_directory", plan)
    report, output = invoke(
        [
            "import-directory",
            "destination",
            str(tmp_path),
            "--scope",
            str(SCOPE),
            "--format",
            "documents",
            "--all-branches",
            "--source-timezone",
            "Etc/UTC",
            "--fallback-timestamp",
            AT,
            "--source-only",
            "--dry-run",
        ],
        capsys,
    )
    assert calls == [
        (
            tmp_path,
            {
                "format": "documents",
                "all_branches": True,
                "timezone": "Etc/UTC",
                "fallback_timestamp": AT,
            },
        )
    ]
    assert report["project"] is False and report["payload_bytes"] > 0
    assert episode.project is True, "The source-only override must not mutate a shared plan"
    assert "Do not print this" not in output


def test_source_only_preview_rechecks_payload_limit(tmp_path, monkeypatch):
    episode = EpisodeInput(source_id="synthetic", content="A note", occurred_at=AT)
    plan = directory_import.ImportPlan(episodes=[episode], report={})
    projected = plan.payload(SCOPE)
    encoded_size = len(json.dumps(projected, ensure_ascii=False, separators=(",", ":")).encode())
    monkeypatch.setattr(directory_import, "MAX_IMPORT_BYTES", encoded_size)
    monkeypatch.setattr(directory_import, "plan_directory", lambda *_args, **_kwargs: plan)
    with pytest.raises(PortabilityError, match="32 MiB"):
        cli.run(
            cli.parser().parse_args(
                [
                    "import-directory",
                    "world",
                    str(tmp_path),
                    "--scope",
                    str(SCOPE),
                    "--source-only",
                    "--dry-run",
                ]
            )
        )


def test_directory_apply_resolves_live_aliases_and_replays_after_move(service, tmp_path, capsys):
    folder = tmp_path / "archives"
    write_chat(folder)
    write_chat(folder / "nested", "second-chat")
    data_dir = tmp_path / "unused-client-data"
    arguments = [
        "--url",
        "http://archive-service.invalid",
        "--data-dir",
        str(data_dir),
        "import-directory",
        "@personal",
        str(folder),
        "--scope",
        "@history",
    ]
    report, output = invoke(arguments, capsys)
    assert (report["imported"], report["replayed"]) == (4, 0)
    assert report["dry_run"] is False and report["project"] is True
    assert report["destination"] == {
        "world_id": service.world.id,
        "scope_id": str(service.scope.id),
    }
    assert [request.method for request in service.requests] == ["GET", "GET", "POST"]
    request = service.requests[-1]
    assert request.url.host == "archive-service.invalid"
    assert request.url.path == (f"/v1/worlds/{service.world.id}/scopes/{service.scope.id}/imports")
    body = json.loads(request.content)
    assert set(body) == {"episodes"}
    assert all(item["scope_id"] == str(service.scope.id) for item in body["episodes"])
    assert not data_dir.exists()
    assert service.token not in output and "Private synthetic fixture note" not in output
    assert str(tmp_path) not in output
    stored = service.store.list_episodes(service.owner, service.world.id, scope_id=service.scope.id)
    assert len(stored) == 4 and all(item.project for item in stored)
    assert len(service.store.pending_jobs(service.world.id, scope_id=service.scope.id)) == 4
    assert service.store.list_episodes(service.owner, service.world.id) == []

    moved = tmp_path / "moved-archives"
    shutil.move(folder, moved)
    arguments[arguments.index(str(folder))] = str(moved)
    replay, _ = invoke(arguments, capsys)
    assert (replay["imported"], replay["replayed"]) == (0, 4)


def test_directory_source_only_application_stores_no_projection_jobs(service, tmp_path, capsys):
    folder = tmp_path / "archives"
    write_chat(folder)
    report, _ = invoke(
        [
            "import-directory",
            service.world.id,
            str(folder),
            "--scope",
            str(service.scope.id),
            "--source-only",
        ],
        capsys,
    )
    assert report["imported"] == 2 and report["project"] is False
    stored = service.store.list_episodes(service.owner, service.world.id, scope_id=service.scope.id)
    assert len(stored) == 2 and all(not item.project for item in stored)
    assert all(item.projection_state == "disabled" for item in stored)
    assert service.store.pending_jobs(service.world.id, scope_id=service.scope.id) == []


def test_malformed_final_file_fails_before_service_or_credentials(tmp_path, monkeypatch):
    folder = tmp_path / "archives"
    write_chat(folder, "a-valid")
    (folder / "z-malformed.json").write_text('{"mapping":', encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Every selected file must validate before accessing the service")

    monkeypatch.setattr(cli, "read_token", forbidden)
    monkeypatch.setattr(cli, "Client", forbidden)
    with pytest.raises(PortabilityError):
        cli.run(
            cli.parser().parse_args(
                [
                    "import-directory",
                    "world",
                    str(folder),
                    "--scope",
                    str(SCOPE),
                ]
            )
        )


def test_directory_api_rejects_world_shared_destination_without_partial_writes(
    service, tmp_path, capsys
):
    folder = tmp_path / "archives"
    write_chat(folder)
    with pytest.raises(httpx.HTTPStatusError) as error:
        cli.run(
            cli.parser().parse_args(
                [
                    "import-directory",
                    service.world.id,
                    str(folder),
                    "--scope",
                    str(service.world.scope_id),
                ]
            )
        )
    assert error.value.response.status_code == 403
    assert capsys.readouterr().out == ""
    assert service.store.list_episodes(service.owner, service.world.id) == []
    assert (
        service.store.list_episodes(service.owner, service.world.id, scope_id=service.scope.id)
        == []
    )


def test_directory_cli_auth_error_omits_credentials_and_source_text(
    service, tmp_path, monkeypatch, capsys
):
    folder = tmp_path / "archives"
    write_chat(folder)
    monkeypatch.setenv("MEMORY_WORLDS_TOKEN", "synthetic-invalid-private-token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory-worlds",
            "import-directory",
            service.world.id,
            str(folder),
            "--scope",
            str(service.scope.id),
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "memory API returned HTTP 401\n"
    assert (
        service.store.list_episodes(service.owner, service.world.id, scope_id=service.scope.id)
        == []
    )
