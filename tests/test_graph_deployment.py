"""Opt-in authentication and persistence check of the shipped graph Compose file."""

import os
import secrets
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MEMORY_WORLDS_RUN_CONTAINER_TEST") != "1",
    reason="set MEMORY_WORLDS_RUN_CONTAINER_TEST=1 to test the isolated Compose deployment",
)


def test_graph_compose_authentication_configuration_and_restart():
    redis = pytest.importorskip("redis")
    falkordb = pytest.importorskip("falkordb")
    compose_file = Path(__file__).resolve().parents[1] / "deploy" / "compose.graph.yaml"
    project = "mw-graph-test-" + uuid4().hex[:12]
    password = secrets.token_hex(32)
    marker = uuid4().hex
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    environment = {
        **os.environ,
        "MEMORY_WORLDS_FALKORDB_PASSWORD": password,
        "MEMORY_WORLDS_FALKORDB_PORT": str(port),
    }
    command = [
        "docker",
        "compose",
        "--env-file",
        os.devnull,
        "--project-name",
        project,
        "--file",
        str(compose_file),
    ]

    def compose(*arguments, env=None, check=True, timeout=60):
        result = subprocess.run(
            [*command, *arguments],
            env=environment if env is None else env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            # Keep failure diagnostics useful without ever printing the credential
            # supplied through the container environment and its private config.
            detail = (result.stdout + result.stderr).replace(password, "[redacted]")
            raise AssertionError(f"Isolated graph Compose operation failed: {detail}")
        return result

    missing_password = dict(environment)
    missing_password.pop("MEMORY_WORLDS_FALKORDB_PASSWORD")
    invalid = compose("config", "--quiet", env=missing_password, check=False)
    assert invalid.returncode != 0, "Compose must reject a missing database password"

    authenticated = redis.Redis(
        host="127.0.0.1",
        port=port,
        password=password,
        socket_timeout=2,
        socket_connect_timeout=2,
        decode_responses=True,
    )
    anonymous = redis.Redis(host="127.0.0.1", port=port, socket_timeout=2, socket_connect_timeout=2)
    graph_client = None

    def ready():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if authenticated.ping():
                    return
            except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
                pass
            time.sleep(0.1)
        raise AssertionError("Authenticated graph service did not become ready")

    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "45", "falkordb")
        ready()
        mapping = compose("port", "falkordb", "6379").stdout.strip()
        assert mapping == f"127.0.0.1:{port}"
        with pytest.raises(redis.exceptions.AuthenticationError):
            anonymous.ping()
        assert authenticated.config_get("appendonly") == {"appendonly": "yes"}
        assert authenticated.config_get("appendfsync") == {"appendfsync": "everysec"}
        assert authenticated.config_get("dir") == {"dir": "/var/lib/falkordb/data"}
        command_line = compose("exec", "-T", "falkordb", "cat", "/proc/1/cmdline").stdout
        if password in command_line:
            raise AssertionError("The database password must not appear in process arguments")
        config_mode = compose(
            "exec", "-T", "falkordb", "stat", "-c", "%a", "/run/memory-worlds/redis.conf"
        ).stdout.strip()
        directory_mode = compose(
            "exec", "-T", "falkordb", "stat", "-c", "%a", "/run/memory-worlds"
        ).stdout.strip()
        assert config_mode == "600"
        assert directory_mode == "700"
        mounts = compose("exec", "-T", "falkordb", "cat", "/proc/mounts").stdout
        assert any(
            fields[1:3] == ["/run/memory-worlds", "tmpfs"]
            for line in mounts.splitlines()
            if len(fields := line.split()) >= 3
        )

        graph_client = falkordb.FalkorDB(
            host="127.0.0.1",
            port=port,
            password=password,
            socket_timeout=5,
            socket_connect_timeout=2,
        )
        graph = graph_client.select_graph("deployment_probe")
        graph.query("CREATE (:PersistenceProbe {marker: $marker})", params={"marker": marker})
        query = "MATCH (n:PersistenceProbe) RETURN n.marker"
        assert graph.query(query).result_set == [[marker]]

        compose("restart", "--timeout", "10", "falkordb")
        ready()
        with pytest.raises(redis.exceptions.AuthenticationError):
            anonymous.ping()
        assert authenticated.config_get("appendonly") == {"appendonly": "yes"}
        assert graph.query(query).result_set == [[marker]]
    finally:
        authenticated.close()
        anonymous.close()
        if graph_client is not None:
            graph_client.connection.close()
        # The unique project owns these containers, network, and volume. Preserve
        # all images and every resource belonging to any other Compose project.
        compose("down", "--volumes", "--remove-orphans", "--timeout", "10")


def test_default_compose_queues_graphiti_and_retains_sources_during_model_outage(tmp_path):
    """Run the default topology; unreachable local model endpoints cause no paid calls."""
    import json

    import httpx
    import yaml

    root = Path(__file__).resolve().parents[1]
    definition = yaml.safe_load((root / "deploy/compose.yaml").read_text())
    definition["services"]["api"].pop("build")
    definition["services"]["api"]["image"] = os.getenv(
        "MEMORY_WORLDS_TEST_IMAGE", "memory-worlds:local"
    )
    definition["services"]["api"]["ports"] = ["127.0.0.1::8787"]
    definition["services"]["api"]["environment"].update(
        MEMORY_WORLDS_LLM_PROVIDER="openai-compatible",
        MEMORY_WORLDS_LLM_MODEL="synthetic-unreachable-model",
        MEMORY_WORLDS_LLM_BASE_URL="http://127.0.0.1:9/v1",
        MEMORY_WORLDS_LLM_API_KEY="synthetic-no-auth",
        MEMORY_WORLDS_EMBEDDING_PROVIDER="openai-compatible",
        MEMORY_WORLDS_EMBEDDING_MODEL="synthetic-unreachable-embedder",
        MEMORY_WORLDS_EMBEDDING_BASE_URL="http://127.0.0.1:9/v1",
        MEMORY_WORLDS_EMBEDDING_API_KEY="synthetic-no-auth",
        MEMORY_WORLDS_EMBEDDING_DIMENSIONS="1536",
    )
    password = secrets.token_hex(32)
    definition["services"]["api"]["environment"]["MEMORY_WORLDS_FALKORDB_PASSWORD"] = password
    definition["services"]["falkordb"]["environment"]["REDISCLI_AUTH"] = password
    assert "ports" not in definition["services"]["falkordb"]
    path = tmp_path / "compose.json"
    path.write_text(json.dumps(definition))
    path.chmod(0o600)
    project = "mw-default-graph-" + uuid4().hex[:12]
    base = ["docker", "compose", "--project-name", project, "--file", str(path)]

    def compose(*arguments, timeout=60):
        result = subprocess.run(
            [*base, *arguments], capture_output=True, text=True, timeout=timeout, check=False
        )
        assert result.returncode == 0, (result.stdout + result.stderr).replace(
            password, "[redacted]"
        )
        return result.stdout.strip()

    try:
        initialized = json.loads(compose("run", "--rm", "--no-deps", "api", "init"))
        compose("up", "--detach", "--wait", "--wait-timeout", "60", timeout=90)
        url = "http://" + compose("port", "api", "8787")
        world = initialized["world"]["id"]
        with httpx.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + initialized["token"]},
            trust_env=False,
            timeout=10,
        ) as client:
            receipt = client.post(
                f"/v1/worlds/{world}/episodes",
                json={
                    "source_id": "graph-default-outage",
                    "content": "Ada operates a lathe during a synthetic model outage",
                    "occurred_at": "2026-10-20T00:00:00Z",
                },
            )
            assert receipt.status_code == 200
            assert receipt.json()["episode"]["project"] is True
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                stored = client.get(
                    f"/v1/worlds/{world}/episodes/{receipt.json()['episode']['id']}"
                ).json()
                if stored["projection_state"] == "unknown":
                    break
                time.sleep(0.1)
            assert stored["projection_state"] == "unknown"
            graph = client.post("/v1/search", json={"world_ids": [world], "query": "lathe"})
            assert graph.status_code == 503
            source = client.post(
                "/v1/search",
                json={"world_ids": [world], "query": "lathe", "mode": "source"},
            )
            assert source.status_code == 200
            assert source.json()["results"][0]["id"] == receipt.json()["episode"]["id"]
            continued = client.post(
                f"/v1/worlds/{world}/episodes",
                json={
                    "source_id": "source-during-graph-outage",
                    "content": "Durable capture continues",
                    "occurred_at": "2026-10-20T00:00:01Z",
                },
            )
            assert continued.status_code == 200
            assert continued.json()["episode"]["projection_state"] == "pending"
    finally:
        subprocess.run(
            [*base, "down", "--volumes", "--remove-orphans", "--timeout", "10"],
            capture_output=True,
            timeout=60,
        )
