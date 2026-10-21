"""Opt-in proof of the setup controller, owned Docker graph and real Graphiti HTTP path."""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from memory_worlds.client import Client
from memory_worlds.models import EpisodeInput, SearchRequest
from memory_worlds.setup_config import SetupConfig, save_config
from memory_worlds.setup_files import private_read, read_json, setup_lock
from memory_worlds.setup_provision import apply_setup
from memory_worlds.setup_runtime import doctor_setup, down_setup

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.getenv("MEMORY_WORLDS_RUN_SETUP_TEST") != "1",
    reason="requires explicit isolated local Docker setup acceptance",
)


def _port():
    with socket.socket() as descriptor:
        descriptor.bind(("127.0.0.1", 0))
        return descriptor.getsockname()[1]


def _wait(predicate, *, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise AssertionError("setup acceptance deadline exceeded")


def test_owned_runtime_projection_restart_and_retained_graph(tmp_path):
    docker_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")
    }
    inspected_context = subprocess.run(
        ["docker", "context", "inspect"],
        env=docker_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    selected_context = json.loads(inspected_context.stdout)[0]["Name"]
    spec = importlib.util.spec_from_file_location(
        "setup_model_fixture", ROOT / "scripts/qualify-graph.py"
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    model = ThreadingHTTPServer(("127.0.0.1", 0), fixture.ModelHandler)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{model.server_port}/v1"
    config = SetupConfig(
        instance="acceptance-" + uuid4().hex[:8],
        owner="acceptance",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        port=_port(),
        graph_port=_port(),
        embedding_dimensions=16,
        extraction={
            "provider": "openai-compatible",
            "model": "scripted-extractor",
            "base_url": endpoint,
        },
        embedding={
            "provider": "openai-compatible",
            "model": "scripted-embedder",
            "base_url": endpoint,
        },
    )
    path = tmp_path / "config" / "setup.toml"
    save_config(config, path)
    initial = apply_setup(config)
    state = read_json(config.path("state.json"))
    resource = state["resources"]["personal"]
    credential = private_read(config.path("personal.token")).decode().strip()
    children = []
    log_path = tmp_path / "runtime.log"
    environment = {
        **os.environ,
        # These must never become the host daemon's runtime settings.
        "MEMORY_WORLDS_STORAGE_BACKEND": "postgres",
        "MEMORY_WORLDS_DATABASE_URL": "postgresql://wrong:PRIVATE-SENTINEL@127.0.0.1:1/wrong",
        "MEMORY_WORLDS_OIDC_CONFIG": "/nonexistent/corporate.json",
        "OPENAI_API_KEY": "PRIVATE-SENTINEL",
        "DOCKER_HOST": "tcp://192.0.2.1:2375",
    }
    with log_path.open("wb") as log:
        log_path.chmod(0o600)

        def start():
            child = subprocess.Popen(
                [sys.executable, "-m", "memory_worlds.cli", "setup", "run", "--config", str(path)],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=log,
            )
            children.append(child)

            def ready():
                assert child.poll() is None, "setup runtime exited before readiness"
                with httpx.Client(trust_env=False, timeout=2) as http:
                    return http.get(config.url + "/readyz").status_code == 200

            _wait(ready)
            return child

        try:
            child = start()
            # Explicit reconnect can acquire the controller lock while the API owns its data.
            with setup_lock(config):
                assert child.poll() is None
            diagnosis = doctor_setup(config)
            assert diagnosis["api"] == "health_ready"
            assert diagnosis["api_identity"] == "not_verified"
            assert diagnosis["personal_credential"] == "valid"
            with Client(config.url, credential) as client:
                receipt = client.capture(
                    resource["world_id"],
                    EpisodeInput(
                        scope_id=resource["scope_id"],
                        source_id="setup-acceptance",
                        content="QFIX-aster-cobalt: Aster uses the CobaltQueue.",
                        occurred_at=datetime.now(UTC),
                    ),
                )
                _wait(
                    lambda: (
                        client.get_episode(
                            resource["world_id"], receipt.episode.id, scope_id=resource["scope_id"]
                        ).projection_state
                        == "projected"
                    )
                )
                hits = client.search(
                    SearchRequest(
                        world_ids=[resource["world_id"]],
                        scope_id=resource["scope_id"],
                        query="Aster queue",
                    )
                )
                assert "CobaltQueue" in json.dumps(hits)
                assert receipt.episode.id in json.dumps(hits)
            child.send_signal(signal.SIGINT)
            assert child.wait(timeout=30) == 0
            assert doctor_setup(config)["graph"] == "healthy"
            assert apply_setup(config) == {**initial, "replayed": True}
            child = start()
            with Client(config.url, credential) as client:
                assert (
                    client.get_episode(
                        resource["world_id"], receipt.episode.id, scope_id=resource["scope_id"]
                    ).projection_state
                    == "projected"
                )
                assert "CobaltQueue" in json.dumps(
                    client.search(
                        SearchRequest(
                            world_ids=[resource["world_id"]],
                            scope_id=resource["scope_id"],
                            query="Aster queue",
                        )
                    )
                )
            child.send_signal(signal.SIGINT)
            assert child.wait(timeout=30) == 0
            assert down_setup(config)["volumes"] == "retained"
            assert fixture.ModelHandler.operations["chat"] > 0
            assert fixture.ModelHandler.operations["embedding"] > 0
            logs = log_path.read_text()
            assert "PRIVATE-SENTINEL" not in logs
            assert credential not in logs
            assert private_read(config.path("graph.token")).decode().strip() not in logs
        finally:
            for child in children:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    try:
                        child.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
            down_setup(config)
            # This acceptance fixture alone owns the retained volume; normal down keeps it.
            volume = state["compose_project"] + "_graph-data"
            inspected = subprocess.run(
                ["docker", "--context", selected_context, "volume", "inspect", volume],
                check=True,
                capture_output=True,
                text=True,
                env=docker_environment,
            )
            labels = json.loads(inspected.stdout)[0]["Labels"]
            assert labels["memory-worlds.setup.authority"] == state["authority_id"]
            subprocess.run(
                ["docker", "--context", selected_context, "volume", "rm", volume],
                check=True,
                capture_output=True,
                env=docker_environment,
            )
            model.shutdown()
            model.server_close()
            thread.join(timeout=5)
