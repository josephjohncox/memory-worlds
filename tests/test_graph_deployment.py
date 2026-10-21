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
