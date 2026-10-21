import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import yaml

from memory_worlds import setup_runtime as runtime
from memory_worlds.errors import Conflict, MemoryError
from memory_worlds.locking import DaemonLock
from memory_worlds.setup_config import SetupConfig
from memory_worlds.setup_files import ensure_private_dir, setup_lock, write_private


def config(tmp_path, **changes):
    settings = {
        "provider": "openai-compatible",
        "model": "synthetic-model",
        "base_url": "http://127.0.0.1:9/v1",
    }
    values = {
        "instance": "runtime-test",
        "owner": "alice",
        "data_dir": tmp_path / "data",
        "state_dir": tmp_path / "state",
        "port": 18787,
        "graph_port": 18788,
        "extraction": settings,
        "embedding": settings,
    }
    return SetupConfig.model_validate({**values, **changes})


def provisioned(tmp_path, **changes):
    configured = config(tmp_path, **changes)
    ensure_private_dir(configured.data_dir)
    ensure_private_dir(configured.state_dir)
    personal = {
        "world_id": uuid4().hex,
        "scope_id": str(uuid4()),
        "credential_id": uuid4().hex,
        "agent_id": str(uuid4()),
        "token_file": str(configured.path("personal.token")),
    }
    state = {
        "instance": configured.instance,
        "authority_id": uuid4().hex,
        "user_id": "alice",
        "data_dir": str(configured.data_dir),
        "state_dir": str(configured.state_dir),
        "config_digest": configured.digest,
        "compose_project": "memory-worlds-runtime-test",
        "resources": {"personal": personal},
        "files": {},
    }
    contents = {
        "graph.token": "a" * 64 + "\n",
        "personal.token": "synthetic-personal-agent\n",
        "mounts.json": '{"version":1,"mounts":[]}',
        "compose.graph.yaml": runtime.graph_compose_bytes(configured, state),
    }
    for name, content in contents.items():
        path = configured.path(name)
        state["files"][str(path)] = write_private(path, content)
    write_private(configured.path("state.json"), json.dumps(state))
    write_private(configured.data_dir / "daemon.lock", "stopped")
    with sqlite3.connect(configured.data_dir / "control.sqlite3") as db:
        db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(
            "CREATE TABLE credentials (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, "
            "user_id TEXT NOT NULL, agent_id TEXT NOT NULL, admin INTEGER NOT NULL, "
            "revoked INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute("CREATE TABLE credential_contexts (credential_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE identity_sessions (credential_id TEXT PRIMARY KEY)")
        db.execute(
            "INSERT INTO credentials VALUES (?, ?, ?, ?, 0, 0)",
            (
                personal["credential_id"],
                hashlib.sha256(b"synthetic-personal-agent").hexdigest(),
                state["user_id"],
                personal["agent_id"],
            ),
        )
        db.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("authority_id", state["authority_id"]),
                ("bootstrapped", "1"),
                (
                    f"setup:{configured.instance}",
                    json.dumps({key: value for key, value in state.items() if key != "files"}),
                ),
            ],
        )
    return configured, state


def hybrid_provisioned(tmp_path):
    configured, state = provisioned(
        tmp_path,
        mode="hybrid",
        corporate={
            "url": "https://corporate.example.test",
            "token_file": tmp_path / "input.token",
            "world_ids": ["team"],
        },
    )
    token_path = configured.path("corporate.token")
    content = b"synthetic-corporate-user-token\n"
    write_private(token_path, content)
    binding = {
        "url": configured.corporate.url,
        "authority_id": "synthetic-corporate-authority",
        "user_id": "synthetic-corporate-user",
        "world_ids": ["team"],
        "token_file": str(token_path),
        "credential_id": "synthetic-corporate-session",
        "token_sha256": hashlib.sha256(content).hexdigest(),
    }
    write_private(configured.path("corporate.json"), json.dumps(binding))
    state["corporate"] = {
        key: binding[key] for key in ("url", "authority_id", "user_id", "world_ids", "token_file")
    }
    path = configured.path("state.json")
    write_private(
        path,
        json.dumps(state),
        expected_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return configured, state, binding


def docker_fixture(
    monkeypatch,
    state,
    *,
    exists=False,
    foreign=False,
    health="healthy",
    remote=False,
    foreign_volume=False,
):
    calls = []
    owned_labels = {label: state[key] for key, label in runtime._LABELS.items()}
    container_id = "a" * 12

    def execute(arguments, **kwargs):
        nonlocal exists
        calls.append((arguments, kwargs))
        assert kwargs["capture_output"] and kwargs["stdin"] == subprocess.DEVNULL
        assert not kwargs.get("shell")
        if arguments == ["docker", "context", "inspect"]:
            endpoint = "ssh://unrelated-host" if remote else "unix:///synthetic/docker.sock"
            output = json.dumps(
                [{"Name": "local-test", "Endpoints": {"docker": {"Host": endpoint}}}]
            )
        else:
            assert arguments[:3] == ["docker", "--context", "local-test"]
            command = arguments[3:]
            if command[0] == "compose":
                assert (
                    "--env-file" in command
                    and command[command.index("--env-file") + 1] == os.devnull
                )
                assert command[command.index("--project-name") + 1] == state["compose_project"]
                if "up" in command:
                    exists = True
                    output = ""
                elif "down" in command:
                    exists = False
                    output = ""
                else:
                    assert command[-3:] == ["ps", "--quiet", "falkordb"]
                    output = container_id if exists else ""
            elif command[:2] == ["ps", "--all"]:
                output = container_id if exists else ""
            elif command[:2] in (["volume", "ls"], ["network", "ls"]):
                output = (
                    f"{state['compose_project']}_graph-data"
                    if command[0] == "volume"
                    and (
                        exists
                        or foreign_volume
                        and any(item.startswith("name=") for item in command)
                    )
                    else ""
                )
            elif command[:2] in (
                ["container", "inspect"],
                ["volume", "inspect"],
                ["network", "inspect"],
            ):
                if "{{.State.Health.Status}}" in command:
                    output = health
                else:
                    labels = (
                        {}
                        if foreign_volume and command[0] == "volume"
                        else {**owned_labels, "memory-worlds.setup.instance": "another"}
                        if foreign
                        else owned_labels
                    )
                    output = json.dumps(labels)
            else:
                pytest.fail(f"Unexpected Docker operation: {command}")
        return SimpleNamespace(
            returncode=0, stdout=output.encode(), stderr=b"synthetic-private-docker-diagnostic"
        )

    monkeypatch.setattr(runtime.subprocess, "run", execute)
    return calls


def no_foreground(monkeypatch):
    calls = []

    def foreground(*arguments, on_started):
        calls.append(arguments)
        on_started()

    monkeypatch.setattr(runtime, "_foreground", foreground)
    monkeypatch.setattr(runtime, "_available", lambda *_arguments: None)
    return calls


def test_packaged_graph_template_is_available_without_a_checkout(tmp_path, monkeypatch):
    configured = config(tmp_path)
    monkeypatch.chdir(tmp_path)
    content = runtime.graph_compose_bytes(configured, {"instance": configured.instance})
    assert (
        content
        == files("memory_worlds").joinpath("templates/setup-graph.compose.yaml").read_bytes()
    )
    value = yaml.safe_load(content)
    graph = value["services"]["falkordb"]
    assert "@sha256:" in graph["image"]
    assert graph["ports"] == ["127.0.0.1:${MEMORY_WORLDS_FALKORDB_PORT:?}:6379"]
    assert (
        graph["labels"]
        == value["volumes"]["graph-data"]["labels"]
        == value["networks"]["default"]["labels"]
    )
    assert "${MEMORY_WORLDS_FALKORDB_PASSWORD:?}" == graph["environment"]["REDISCLI_AUTH"]
    assert not (tmp_path / "deploy").exists()


def test_runtime_environment_uses_only_configured_local_backends_and_file_credentials(
    tmp_path, monkeypatch
):
    key_file = tmp_path / "provider.token"
    write_private(key_file, "synthetic-provider-key\n")
    configured, _ = provisioned(
        tmp_path,
        extraction={"provider": "openai", "model": "chosen-model", "api_key_file": key_file},
    )
    for name in (
        "MEMORY_WORLDS_DATABASE_URL",
        "MEMORY_WORLDS_STORAGE_BACKEND",
        "MEMORY_WORLDS_AUTHORITY_ID",
        "MEMORY_WORLDS_OIDC_ISSUER",
        "MEMORY_WORLDS_MOUNTS_FILE",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PGPASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "COMPOSE_FILE",
        "DOCKER_HOST",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "synthetic-ambient-secret")
    environment = runtime.runtime_environment(configured)
    assert environment["MEMORY_WORLDS_STORAGE_BACKEND"] == "sqlite"
    assert environment["MEMORY_WORLDS_GRAPH_BACKEND"] == "falkordb"
    assert environment["MEMORY_WORLDS_LLM_MODEL"] == "chosen-model"
    assert environment["MEMORY_WORLDS_LLM_API_KEY"] == "synthetic-provider-key"
    assert environment["MEMORY_WORLDS_FALKORDB_PASSWORD"] == "a" * 64
    assert environment["MEMORY_WORLDS_FALKORDB_HOST"] == "127.0.0.1"
    assert environment["GRAPHITI_TELEMETRY_ENABLED"] == "false"
    assert not any("synthetic-ambient-secret" in value for value in environment.values())
    assert "MEMORY_WORLDS_DATABASE_URL" not in environment
    assert "MEMORY_WORLDS_MOUNTS_FILE" not in environment


@pytest.mark.parametrize(
    "filename", ["graph.token", "mounts.json", "personal.token", "compose.graph.yaml"]
)
def test_runtime_refuses_changes_to_any_managed_file(tmp_path, filename):
    configured, _ = provisioned(tmp_path)
    configured.path(filename).write_text("b" * 64 + "\n")
    with pytest.raises(Conflict, match="changed outside setup|differs from its template"):
        runtime.runtime_environment(configured)
    result = runtime.doctor_setup(configured)
    assert result["state"] == "invalid"
    assert "b" * 64 not in json.dumps(result)


@pytest.mark.parametrize("changed", ["token", "identity"])
def test_hybrid_runtime_refuses_out_of_band_connection_changes(tmp_path, changed):
    configured, _state, binding = hybrid_provisioned(tmp_path)
    assert runtime.runtime_environment(configured)["MEMORY_WORLDS_MOUNTS_FILE"] == str(
        configured.path("mounts.json")
    )
    if changed == "token":
        configured.path("corporate.token").write_text("synthetic-other-user-token\n")
    else:
        binding["user_id"] = "synthetic-other-corporate-user"
        configured.path("corporate.json").write_text(json.dumps(binding))
    with pytest.raises(Conflict, match="Corporate connection"):
        runtime.runtime_environment(configured)
    assert runtime.doctor_setup(configured)["state"] == "invalid"


def test_hybrid_runtime_accepts_explicit_same_user_connection_rotation(tmp_path):
    configured, state, binding = hybrid_provisioned(tmp_path)
    content = b"synthetic-rotated-corporate-user-token\n"
    token_path = configured.path("corporate.token")
    write_private(token_path, content, expected_hash=binding["token_sha256"])
    path = configured.path("corporate.json")
    previous = hashlib.sha256(path.read_bytes()).hexdigest()
    binding.update(
        credential_id="synthetic-rotated-session",
        token_sha256=hashlib.sha256(content).hexdigest(),
    )
    write_private(path, json.dumps(binding), expected_hash=previous)
    environment = runtime.runtime_environment(configured)
    assert environment["MEMORY_WORLDS_MOUNTS_FILE"] == str(configured.path("mounts.json"))
    assert content.decode().strip() not in environment.values()
    assert json.loads(configured.path("state.json").read_text()) == state


def test_run_starts_only_the_owned_graph_then_the_foreground_daemon(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    calls = docker_fixture(monkeypatch, state)
    foreground = no_foreground(monkeypatch)
    runtime.run_setup(configured)
    assert len(foreground) == 1
    mutations = [arguments for arguments, _ in calls if "up" in arguments or "down" in arguments]
    assert len(mutations) == 1 and "up" in mutations[0]
    assert "down" not in mutations[0]
    assert all(
        "--volumes" not in arguments
        and "-v" not in arguments
        and "--remove-orphans" not in arguments
        for arguments, _ in calls
    )


@pytest.mark.parametrize("operation", [runtime.run_setup, runtime.down_setup])
def test_foreign_graph_ownership_prevents_every_compose_mutation(tmp_path, monkeypatch, operation):
    configured, state = provisioned(tmp_path)
    calls = docker_fixture(monkeypatch, state, exists=True, foreign=True)
    no_foreground(monkeypatch)
    with pytest.raises(Conflict, match="another installation"):
        operation(configured)
    assert not any("up" in arguments or "down" in arguments for arguments, _ in calls)


def test_remote_docker_context_is_rejected_before_graph_mutation(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    calls = docker_fixture(monkeypatch, state, remote=True)
    no_foreground(monkeypatch)
    with pytest.raises(MemoryError, match="local Unix Docker context"):
        runtime.run_setup(configured)
    assert len(calls) == 1


def test_unlabelled_volume_name_collision_is_rejected_before_graph_mutation(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    calls = docker_fixture(monkeypatch, state, foreign_volume=True)
    no_foreground(monkeypatch)
    with pytest.raises(Conflict, match="another installation"):
        runtime.run_setup(configured)
    assert not any("up" in arguments for arguments, _ in calls)


def test_down_retains_owned_graph_volumes_and_checks_the_stopped_authority(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    calls = docker_fixture(monkeypatch, state, exists=True)
    assert runtime.down_setup(configured) == {
        "instance": configured.instance,
        "graph": "stopped",
        "volumes": "retained",
    }
    mutations = [arguments for arguments, _ in calls if "down" in arguments]
    assert len(mutations) == 1
    assert "--volumes" not in mutations[0] and "-v" not in mutations[0]
    assert configured.path("graph.token").exists()


@pytest.mark.parametrize("damaged", ["provider_key", "profile", "corporate_token"])
def test_down_does_not_depend_on_model_or_client_files(tmp_path, monkeypatch, damaged):
    if damaged == "corporate_token":
        configured, state, _binding = hybrid_provisioned(tmp_path)
        configured.path("corporate.token").unlink()
    elif damaged == "provider_key":
        path = tmp_path / "provider.key"
        write_private(path, "synthetic-provider-key\n")
        configured, state = provisioned(
            tmp_path,
            extraction={
                "provider": "openai-compatible",
                "model": "synthetic-model",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key_file": path,
            },
        )
        path.unlink()
    else:
        configured, state = provisioned(tmp_path)
        path = configured.path("pi-personal.json")
        state["files"][str(path)] = write_private(path, "{}\n")
        state_path = configured.path("state.json")
        write_private(
            state_path,
            json.dumps(state),
            expected_hash=hashlib.sha256(state_path.read_bytes()).hexdigest(),
        )
        path.write_text('{"user_modified":true}\n')
    with pytest.raises(MemoryError):
        runtime.runtime_environment(configured)
    calls = docker_fixture(monkeypatch, state, exists=True)
    assert runtime.down_setup(configured)["volumes"] == "retained"
    mutations = [(arguments, options) for arguments, options in calls if "down" in arguments]
    assert len(mutations) == 1
    environment = mutations[0][1]["env"]
    assert environment["MEMORY_WORLDS_FALKORDB_PASSWORD"] == "0" * 64
    assert "MEMORY_WORLDS_LLM_API_KEY" not in environment
    assert "MEMORY_WORLDS_MOUNTS_FILE" not in environment
    assert not any("--volumes" in arguments or "-v" in arguments for arguments, _ in calls)


@pytest.mark.parametrize("operation", [runtime.run_setup, runtime.down_setup])
def test_active_daemon_prevents_run_or_down_before_docker_calls(tmp_path, monkeypatch, operation):
    configured, _ = provisioned(tmp_path)
    monkeypatch.setattr(
        runtime,
        "_command",
        lambda *_args, **_kwargs: pytest.fail("Active daemon must prevent Docker calls"),
    )
    with DaemonLock(configured.data_dir), pytest.raises(Conflict):
        operation(configured)


def test_changed_authority_identity_prevents_run(tmp_path, monkeypatch):
    configured, _ = provisioned(tmp_path)
    with sqlite3.connect(configured.data_dir / "control.sqlite3") as db:
        db.execute("UPDATE metadata SET value=? WHERE key='authority_id'", (uuid4().hex,))
    monkeypatch.setattr(
        runtime,
        "_command",
        lambda *_args, **_kwargs: pytest.fail("Authority mismatch must prevent Docker calls"),
    )
    with pytest.raises(Conflict, match="ownership"):
        runtime.run_setup(configured)


def test_unowned_api_port_prevents_startup(tmp_path, monkeypatch):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        configured, _ = provisioned(tmp_path, port=occupied.getsockname()[1])
        monkeypatch.setattr(
            runtime,
            "_command",
            lambda *_args, **_kwargs: pytest.fail("Occupied API port must prevent Docker calls"),
        )
        with pytest.raises(Conflict, match="port is already occupied"):
            runtime.run_setup(configured)


def test_doctor_absent_state_creates_no_directories_or_store(tmp_path, monkeypatch):
    configured = config(tmp_path)
    monkeypatch.setattr(
        runtime,
        "_command",
        lambda *_args, **_kwargs: pytest.fail("Absent setup must not query Docker"),
    )
    result = runtime.doctor_setup(configured)
    assert result["state"] == "missing" and result["authority"] == "not_checked"
    assert not configured.state_dir.exists() and not configured.data_dir.exists()


@pytest.mark.parametrize("ready", [True, False])
def test_doctor_is_readonly_and_reports_offline_binding_and_unauthenticated_health(
    tmp_path, monkeypatch, ready
):
    configured, state = provisioned(tmp_path)
    docker_fixture(monkeypatch, state, exists=True)
    monkeypatch.setattr(runtime, "_running", lambda _config: True)
    before = {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    real_client = httpx.Client
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and not request.content
        assert "authorization" not in request.headers
        return httpx.Response(200 if ready or request.url.path == "/healthz" else 503)

    monkeypatch.setattr(
        runtime.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    result = runtime.doctor_setup(configured)
    assert result["authority"] == "valid"
    assert result["personal_credential"] == "valid"
    assert result["api"] == ("health_ready" if ready else "not_ready")
    assert result["api_identity"] == "not_verified"
    assert result["graph"] == "healthy" and result["model_processing"] == "not_checked"
    assert {request.url.path for request in requests} == {"/healthz", "/readyz"}
    assert "synthetic-personal-agent" not in json.dumps(result)
    assert "synthetic-private-response-content" not in json.dumps(result)
    after = {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_stopped_doctor_never_sends_bearer_to_an_unrelated_loopback_listener(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    docker_fixture(monkeypatch, state, exists=True)
    real_client = httpx.Client
    requests = []

    def unrelated_listener(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "unrelated listener"})

    monkeypatch.setattr(
        runtime.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(unrelated_listener)),
    )
    assert runtime._running(configured) is False
    result = runtime.doctor_setup(configured)
    assert result["daemon"] == "stopped" and result["api"] == "daemon_stopped"
    assert result["graph"] == "healthy"
    assert requests == []


@pytest.mark.parametrize("failure", ["missing", "identity", "bootstrapped", "ownership"])
def test_doctor_distinguishes_authority_storage_from_valid_managed_files(
    tmp_path, monkeypatch, failure
):
    configured, state = provisioned(tmp_path)
    path = configured.data_dir / "control.sqlite3"
    if failure == "missing":
        path.unlink()
    else:
        key, value = {
            "identity": ("authority_id", uuid4().hex),
            "bootstrapped": ("bootstrapped", "0"),
            "ownership": (f"setup:{configured.instance}", "{}"),
        }[failure]
        with sqlite3.connect(path) as db:
            db.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))
    docker_fixture(monkeypatch, state)
    before = {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    result = runtime.doctor_setup(configured)
    assert result["state"] == result["credentials"] == "valid"
    assert result["authority"] == ("missing" if failure == "missing" else "mismatch")
    assert result["daemon"] == "stopped" and result["api"] == "daemon_stopped"
    assert {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before


def test_doctor_checks_authority_even_when_a_managed_credential_is_missing(tmp_path, monkeypatch):
    configured, _ = provisioned(tmp_path)
    configured.path("personal.token").unlink()
    monkeypatch.setattr(
        runtime,
        "_command",
        lambda *_args, **_kwargs: pytest.fail("Invalid setup must not query Docker"),
    )
    result = runtime.doctor_setup(configured)
    assert result["state"] == result["credentials"] == "invalid"
    assert result["authority"] == "valid"


def test_doctor_stopped_wal_header_creates_no_source_sidecars(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    path = configured.data_dir / "control.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    db.close()
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    docker_fixture(monkeypatch, state)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert runtime.doctor_setup(configured)["authority"] == "valid"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()


@pytest.mark.parametrize("bootstrapped", ["1", "0"])
def test_doctor_reads_committed_crash_wal_without_creating_missing_source_shm(
    tmp_path, monkeypatch, bootstrapped
):
    configured, state = provisioned(tmp_path)
    path = configured.data_dir / "control.sqlite3"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sqlite3, sys; db=sqlite3.connect(sys.argv[1]); "
            "db.execute('PRAGMA journal_mode=WAL'); "
            "db.execute(\"INSERT INTO metadata VALUES ('doctor-wal-fixture', 'committed')\"); "
            "db.execute(\"UPDATE metadata SET value=? WHERE key='bootstrapped'\", "
            "(sys.argv[2],)); db.commit(); os._exit(0)",
            str(path),
            bootstrapped,
        ],
        check=True,
        timeout=10,
    )
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    assert wal.stat().st_size > 32
    shm.unlink()
    docker_fixture(monkeypatch, state)
    before = {
        str(item): (item.read_bytes(), item.stat().st_mtime_ns)
        for item in tmp_path.rglob("*")
        if item.is_file()
    }
    result = runtime.doctor_setup(configured)
    assert result["authority"] == ("valid" if bootstrapped == "1" else "mismatch")
    assert not shm.exists()
    assert {
        str(item): (item.read_bytes(), item.stat().st_mtime_ns)
        for item in tmp_path.rglob("*")
        if item.is_file()
    } == before


def test_doctor_running_authority_queries_only_binding_rows_in_one_private_snapshot(
    tmp_path, monkeypatch
):
    configured, state = provisioned(tmp_path)
    path = configured.data_dir / "control.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE excluded_content (secret TEXT)")
        db.execute("INSERT INTO excluded_content VALUES ('synthetic-excluded-content')")
        db.commit()
        with DaemonLock(configured.data_dir):
            docker_fixture(monkeypatch, state)
            real_connect = sqlite3.connect
            statements = []

            def read_snapshot(database, **kwargs):
                assert str(configured.data_dir) not in database
                assert database.endswith("?mode=ro") and kwargs["uri"]
                connection = real_connect(database, **kwargs)
                connection.set_trace_callback(statements.append)

                def authorize(action, table, *_args):
                    if action == sqlite3.SQLITE_READ:
                        assert table in {
                            "metadata",
                            "credentials",
                            "credential_contexts",
                            "identity_sessions",
                        }
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(authorize)
                return connection

            monkeypatch.setattr(runtime.sqlite3, "connect", read_snapshot)
            assert runtime._running(configured)
            before = {
                str(item): (item.read_bytes(), item.stat().st_mtime_ns)
                for item in configured.data_dir.iterdir()
                if item.is_file()
            }
            assert runtime._doctor_inspect(configured, state, "synthetic-personal-agent") == {
                "authority": "valid",
                "personal_credential": "valid",
            }
            assert len(statements) == 2 and "FROM metadata" in statements[0]
            assert "FROM credentials WHERE id=" in statements[1]
            assert {
                str(item): (item.read_bytes(), item.stat().st_mtime_ns)
                for item in configured.data_dir.iterdir()
                if item.is_file()
            } == before


@pytest.mark.parametrize("unavailable", ["size", "changing"])
def test_doctor_never_calls_an_uncertain_snapshot_valid(tmp_path, monkeypatch, unavailable):
    configured, state = provisioned(tmp_path)
    docker_fixture(monkeypatch, state)
    if unavailable == "size":
        monkeypatch.setattr(runtime, "_DOCTOR_AUTHORITY_MAXIMUM", 1)
    else:
        original = runtime.os.read
        path = configured.data_dir / "control.sqlite3"

        def changing_read(descriptor, maximum):
            content = original(descriptor, maximum)
            if os.fstat(descriptor).st_ino == path.stat().st_ino:
                info = path.stat()
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
            return content

        monkeypatch.setattr(runtime.os, "read", changing_read)
    result = runtime.doctor_setup(configured)
    assert result["state"] == result["credentials"] == "valid"
    assert result["authority"] == "unavailable"


def test_doctor_withholds_credentials_for_an_unverified_running_authority(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    (configured.data_dir / "control.sqlite3").unlink()
    docker_fixture(monkeypatch, state)
    monkeypatch.setattr(runtime, "_running", lambda _config: True)
    monkeypatch.setattr(
        runtime.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("Unverified authority must not receive bearer credentials"),
    )
    result = runtime.doctor_setup(configured)
    assert result["authority"] == "missing"
    assert result["daemon"] == "running" and result["api"] == "authority_unverified"


def test_doctor_refuses_a_hardlinked_authority_without_touching_either_alias(tmp_path, monkeypatch):
    configured, state = provisioned(tmp_path)
    path = configured.data_dir / "control.sqlite3"
    alias = tmp_path / "control-alias.sqlite3"
    os.link(path, alias)
    docker_fixture(monkeypatch, state)
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert runtime.doctor_setup(configured)["authority"] == "mismatch"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert alias.read_bytes() == before[0]


@pytest.mark.parametrize(
    "change,expected",
    [
        ("token_hash", "mismatch"),
        ("id", "missing"),
        ("user_id", "mismatch"),
        ("agent_id", "mismatch"),
        ("admin", "mismatch"),
        ("revoked", "revoked"),
        ("credential_contexts", "mismatch"),
        ("identity_sessions", "mismatch"),
    ],
)
def test_doctor_checks_the_personal_credential_binding_offline(
    tmp_path, monkeypatch, change, expected
):
    configured, state = provisioned(tmp_path)
    credential_id = state["resources"]["personal"]["credential_id"]
    with sqlite3.connect(configured.data_dir / "control.sqlite3") as db:
        if change in {"credential_contexts", "identity_sessions"}:
            db.execute(f"INSERT INTO {change} VALUES (?)", (credential_id,))
        else:
            value = 1 if change in {"admin", "revoked"} else "synthetic-mismatch"
            db.execute(f"UPDATE credentials SET {change}=? WHERE id=?", (value, credential_id))
    docker_fixture(monkeypatch, state)
    monkeypatch.setattr(
        runtime.httpx,
        "Client",
        lambda **_kwargs: pytest.fail("Stopped credential inspection must remain offline"),
    )
    result = runtime.doctor_setup(configured)
    assert result["state"] == result["credentials"] == result["authority"] == "valid"
    assert result["personal_credential"] == expected
    assert "synthetic-personal-agent" not in json.dumps(result)


def test_doctor_sends_no_bearer_when_a_replacement_listener_takes_the_daemon_port(
    tmp_path, monkeypatch
):
    requests = []

    class Listener(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    original_listener = ThreadingHTTPServer(("127.0.0.1", 0), Listener)
    port = original_listener.server_address[1]
    configured, state = provisioned(tmp_path, port=port)
    docker_fixture(monkeypatch, state)
    command = runtime._command
    replacement = None
    thread = None
    with DaemonLock(configured.data_dir) as daemon:
        assert runtime._running(configured)

        def replace_after_ownership_check(arguments, environment, **kwargs):
            nonlocal replacement, thread
            if replacement is None:
                daemon.__exit__()
                original_listener.server_close()
                replacement = ThreadingHTTPServer(("127.0.0.1", port), Listener)
                thread = threading.Thread(
                    target=replacement.serve_forever,
                    kwargs={"poll_interval": 0.05},
                    daemon=True,
                )
                thread.start()
                assert not runtime._running(configured)
            return command(arguments, environment, **kwargs)

        monkeypatch.setattr(runtime, "_command", replace_after_ownership_check)
        try:
            result = runtime.doctor_setup(configured)
        finally:
            original_listener.server_close()
            if replacement is not None:
                replacement.shutdown()
                replacement.server_close()
            if thread is not None:
                thread.join(timeout=2)
    assert result["authority"] == result["personal_credential"] == "valid"
    assert result["daemon"] == "running"  # The observation preceded the replacement.
    assert result["api"] == "health_ready" and result["api_identity"] == "not_verified"
    assert {path for path, _headers in requests} == {"/healthz", "/readyz"}
    assert all(
        all(name.lower() != "authorization" for name in headers) for _path, headers in requests
    )
    assert "synthetic-personal-agent" not in json.dumps(requests)


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
def test_runtime_authority_check_refuses_hardlinked_storage_before_sqlite_opens(
    tmp_path, monkeypatch, suffix
):
    configured, state = provisioned(tmp_path)
    path = configured.data_dir / ("control.sqlite3" + suffix)
    if suffix:
        write_private(path, "synthetic-sidecar")
    alias = tmp_path / "storage-alias"
    os.link(path, alias)
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(
        runtime.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("Linked authority storage must not reach SQLite"),
    )
    with pytest.raises(Conflict, match="one hard link"):
        runtime._authority(configured, state)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert alias.read_bytes() == before[0]


def test_foreground_handoff_allows_connection_lock_while_daemon_lock_stays_held(
    tmp_path, monkeypatch
):
    configured, state = provisioned(tmp_path)
    docker_fixture(monkeypatch, state)
    monkeypatch.setattr(runtime, "_available", lambda *_: None)
    observations = []

    def foreground(configured, _environment, *, on_started):
        with DaemonLock(configured.data_dir):
            assert runtime._running(configured)
            with pytest.raises(Conflict, match="another setup operation"):
                with setup_lock(configured):
                    pytest.fail("Startup released its setup lock before handoff")
            on_started()
            with setup_lock(configured):
                observations.append("connection_can_rotate")
            with pytest.raises(Conflict, match="Stop the foreground daemon"):
                runtime.down_setup(configured)

    monkeypatch.setattr(runtime, "_foreground", foreground)
    runtime.run_setup(configured)
    assert observations == ["connection_can_rotate"]


def test_foreground_never_hands_off_when_child_exits_before_daemon_lock(tmp_path, monkeypatch):
    configured = config(tmp_path)
    child = SimpleNamespace(poll=lambda: 1)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: child)
    callbacks = []
    with pytest.raises(MemoryError, match="before acquiring"):
        runtime._foreground(configured, {}, on_started=lambda: callbacks.append("started"))
    assert callbacks == []


def test_foreground_handoff_requires_its_own_child_pid_under_daemon_lock(tmp_path, monkeypatch):
    configured, _state = provisioned(tmp_path)
    with DaemonLock(configured.data_dir):
        runtime._wait_for_daemon(configured, SimpleNamespace(pid=os.getpid(), poll=lambda: None))
        results = iter([None, 1])
        child = SimpleNamespace(pid=os.getpid() + 1, poll=lambda: next(results))
        monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
        with pytest.raises(MemoryError, match="before acquiring"):
            runtime._wait_for_daemon(configured, child)


def test_foreground_signals_only_its_owned_child_and_keeps_graph_running(tmp_path, monkeypatch):
    configured = config(tmp_path)
    handlers, sent, invocations = {}, [], []
    child = SimpleNamespace(poll=lambda: 0, send_signal=lambda number: sent.append(number))

    def wait():
        child.poll = lambda: None
        handlers[signal.SIGINT](signal.SIGINT, None)
        child.poll = lambda: 0
        return 0

    child.wait = wait
    monkeypatch.setattr(
        runtime.signal, "signal", lambda number, handler: handlers.setdefault(number, handler)
    )
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda arguments, **kwargs: invocations.append((arguments, kwargs)) or child,
    )
    runtime._foreground(configured, {"PATH": "synthetic-path"})
    assert sent == [signal.SIGINT]
    assert invocations[0][0][1:4] == ["-P", "-m", "memory_worlds.cli"]
    assert invocations[0][1]["start_new_session"] is True


def test_docker_errors_do_not_echo_private_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=b"synthetic-private-output", stderr=b"synthetic-private-error"
        ),
    )
    with pytest.raises(MemoryError) as caught:
        runtime._command(["docker", "context", "inspect"], {})
    assert "synthetic-private" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("relative_lookup", [False, True])
def test_python_bootstrap_forwards_arguments_from_another_directory_without_trusting_source(
    tmp_path,
    relative_lookup,
):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls.jsonl"
    stub = binary / "mise"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps({\n"
        "        'args':sys.argv[1:], 'cwd':os.getcwd(),\n"
        "        'caller':os.environ.get('MEMORY_WORLDS_SETUP_CWD')\n"
        "    }) + '\\n')\n"
    )
    stub.chmod(0o700)
    arguments = ["validate", "--config", "path with spaces;$(not-a-shell).toml"]
    lookup = binary.name if relative_lookup else str(binary)
    subprocess.run(
        [sys.executable, str(root / "scripts/bootstrap.py"), *arguments],
        cwd=tmp_path,
        env={"PATH": f"{lookup}{os.pathsep}{os.environ['PATH']}"},
        check=True,
        capture_output=True,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call["args"] for call in calls] == [
        ["install", "--locked", "python", "uv"],
        ["run", "setup", "--", *arguments],
    ]
    assert all(call["cwd"] == str(root) and call["caller"] == str(tmp_path) for call in calls)
    task = tomllib.loads((root / "tasks/setup.toml").read_text())["setup"]
    assert task["raw_args"] and task["depends"] == ["python:deps"]


def test_python_bootstrap_reports_missing_mise_without_starting_setup(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "empty-bin"
    binary.mkdir()
    result = subprocess.run(
        [sys.executable, str(root / "scripts/bootstrap.py"), "init"],
        cwd=tmp_path,
        env={"PATH": str(binary)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "mise" in result.stderr.lower()
    assert "https://mise.jdx.dev/installing-mise.html" in result.stderr
    assert "Traceback" not in result.stderr
    assert not list(tmp_path.glob("**/setup.toml"))


def test_python_bootstrap_does_not_start_wizard_when_locked_tool_installation_fails(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "calls.jsonl"
    stub = binary / "mise"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(log)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(37 if sys.argv[1:2] == ['install'] else 0)\n"
    )
    stub.chmod(0o700)
    result = subprocess.run(
        [sys.executable, str(root / "scripts/bootstrap.py"), "init"],
        cwd=tmp_path,
        env={"PATH": str(binary)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 37
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ["install", "--locked", "python", "uv"]
    ]
    assert not list(tmp_path.glob("**/setup.toml"))
