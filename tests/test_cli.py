"""Exercise installed command entry points through real HTTP and stdio processes."""

import json
import os
import socket
import subprocess
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-10-20T00:00:00+00:00"


def test_cli_token_file_is_private_bounded_and_takes_precedence_over_environment(
    tmp_path, monkeypatch
):
    from memory_worlds.cli import read_token

    path = tmp_path / "agent.token"
    path.write_bytes(b"synthetic-file-token\r\n")
    path.chmod(0o600)
    monkeypatch.setenv("MEMORY_WORLDS_TOKEN", "synthetic-environment-token")
    assert read_token(Namespace(token_file=path)) == "synthetic-file-token"
    assert read_token(Namespace(token_file=None)) == "synthetic-environment-token"
    path.write_bytes(b"x" * 16_384 + b"\r\n")
    assert len(read_token(Namespace(token_file=path))) == 16_384


@pytest.mark.parametrize("kind", ["oversize", "non-ascii", "multiline", "permissions", "symlink"])
def test_cli_token_files_reject_unsafe_inputs_without_exposing_their_contents(tmp_path, kind):
    from memory_worlds.cli import read_token
    from memory_worlds.errors import MemoryError

    path = tmp_path / "agent.token"
    path.write_bytes(b"synthetic-private-token")
    path.chmod(0o600)
    if kind == "oversize":
        path.write_bytes(b"synthetic-private-token" + b"x" * 16_386)
    elif kind == "non-ascii":
        path.write_bytes(b"synthetic-private-token\xff")
    elif kind == "multiline":
        path.write_bytes(b"synthetic-private-token\nanother-line")
    elif kind == "permissions":
        path.chmod(0o644)
    else:
        linked = tmp_path / "linked.token"
        linked.symlink_to(path)
        path = linked
    with pytest.raises(MemoryError) as error:
        read_token(Namespace(token_file=path))
    assert "synthetic-private-token" not in str(error.value)
    assert str(path) not in str(error.value)


@pytest.mark.parametrize("value", ["x" * 16_385, "synthetic\nprivate", "synthetic\tprivate", "é"])
def test_cli_environment_tokens_are_bounded_ascii_without_diagnostic_leaks(monkeypatch, value):
    from memory_worlds.cli import read_token
    from memory_worlds.errors import MemoryError

    monkeypatch.setenv("MEMORY_WORLDS_TOKEN", value)
    with pytest.raises(MemoryError, match="ASCII bearer token") as error:
        read_token(Namespace(token_file=None))
    assert value not in str(error.value)


def test_cli_fifo_token_fails_before_mcp_launch_instead_of_blocking(tmp_path):
    fifo = tmp_path / "credential-fifo"
    os.mkfifo(fifo, mode=0o600)
    result = subprocess.run(
        [sys.executable, "-I", "-m", "memory_worlds.cli", "--token-file", str(fifo), "mcp"],
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "owned private regular file" in result.stderr
    assert str(fifo) not in result.stderr


def _environment():
    # Tests never inherit graph provider configuration or live authority credentials.
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("MEMORY_WORLDS_")},
        "MEMORY_WORLDS_GRAPH_BACKEND": "disabled",
    }


def _command(data_dir, *arguments, url=None):
    command = [sys.executable, "-m", "memory_worlds.cli", "--data-dir", str(data_dir)]
    if url is not None:
        command += ["--url", url]
    return command + list(arguments)


def _invoke(data_dir, *arguments, url=None, token=None, input_text=None, success=True):
    environment = _environment()
    if token is not None:
        environment["MEMORY_WORLDS_TOKEN"] = token
    completed = subprocess.run(
        _command(data_dir, *arguments, url=url),
        cwd=ROOT,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if success:
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)
    assert completed.returncode != 0
    return completed


@contextmanager
def _daemon(data_dir):
    from memory_worlds.recovery_archive import generate_key

    key_file = data_dir.parent / "backup.key"
    if not key_file.exists():
        generate_key(key_file)
    environment = {
        **_environment(),
        "MEMORY_WORLDS_BACKUP_DIRECTORY": str(data_dir.parent / "backups"),
        "MEMORY_WORLDS_BACKUP_KEY_FILE": str(key_file),
    }
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        _command(data_dir, "serve", "--port", str(port)),
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=url, timeout=0.2, trust_env=False) as health:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("CLI daemon exited before becoming ready")
                try:
                    if health.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("CLI daemon did not become ready")
        yield url
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


@pytest.fixture
def running_authority(tmp_path):
    data_dir = tmp_path / "authority"
    initialized = _invoke(data_dir, "init", "--user", "alice")
    with _daemon(data_dir) as url:
        yield data_dir, url, initialized["token"], initialized["world"]["id"]


def test_cli_agents_capture_search_idempotency_and_revocation(running_authority):
    data_dir, url, administrator, world = running_authority
    listed = _invoke(data_dir, "worlds", url=url, token=administrator)
    assert len(listed) == 1 and listed[0]["id"] == world
    agents = []
    credential_ids = []
    for name in ("first", "second"):
        credential = _invoke(
            data_dir,
            "token-create",
            "--user",
            "alice",
            "--agent",
            name,
            "--grant",
            f"{world}:writer",
            url=url,
            token=administrator,
        )
        agents.append(credential["token"])
        credential_ids.append(credential["credential_id"])

    def capture(index):
        return _invoke(
            data_dir,
            "capture",
            world,
            "--source-id",
            f"cli-{index}",
            "--at",
            AT,
            input_text=f"Synthetic fixture from agent {index}",
            url=url,
            token=agents[index],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        captures = list(pool.map(capture, range(2)))
    assert {item["episode"]["agent_id"] for item in captures} == {"first", "second"}
    found = _invoke(
        data_dir,
        "search",
        "--source",
        "Synthetic fixture",
        "--world",
        world,
        url=url,
        token=agents[1],
    )
    assert {item["source_id"] for item in found["results"]} == {"cli-0", "cli-1"}
    repeated = capture(0)
    assert repeated["replayed"] is True
    assert repeated["episode"]["id"] == captures[0]["episode"]["id"]
    conflict = _invoke(
        data_dir,
        "capture",
        world,
        "--source-id",
        "cli-0",
        "--at",
        AT,
        "--text",
        "Conflicting synthetic text",
        url=url,
        token=agents[0],
        success=False,
    )
    assert "HTTP 409" in conflict.stderr and "Conflicting synthetic text" not in conflict.stderr
    _invoke(data_dir, "token-revoke", credential_ids[0], url=url, token=administrator)
    revoked = _invoke(data_dir, "worlds", url=url, token=agents[0], success=False)
    assert "HTTP 401" in revoked.stderr


def test_cli_export_restore_and_online_authority_backup(running_authority, tmp_path):
    data_dir, url, administrator, world = running_authority
    captured = _invoke(
        data_dir,
        "capture",
        world,
        "--source-id",
        "portable-cli",
        "--at",
        AT,
        "--text",
        "Synthetic portable evidence",
        url=url,
        token=administrator,
    )
    archive = tmp_path / "sync" / "world.mworld"
    result = _invoke(data_dir, "export", world, str(archive), token=administrator)
    assert Path(result["path"]).is_file()
    backup = tmp_path / "authority.mwauthority"
    result = _invoke(
        data_dir,
        "authority-backup",
        str(backup),
        "--graph-mode",
        "rebuild",
        url=url,
        token=administrator,
    )
    assert result["graph_mode"] == "rebuild" and result["bytes"] == backup.stat().st_size
    recovered = tmp_path / "authority-recovered"
    restored_authority = _invoke(
        recovered, "authority-restore", str(backup), "--key-file", str(tmp_path / "backup.key")
    )
    assert restored_authority["graph_mode"] == "rebuild"
    with _daemon(recovered) as recovered_url:
        found = _invoke(
            recovered,
            "search",
            "Synthetic portable",
            "--source",
            "--world",
            world,
            url=recovered_url,
            token=administrator,
        )
        assert found["results"][0]["id"] == captured["episode"]["id"]

    destination_dir = tmp_path / "destination"
    destination = _invoke(destination_dir, "init", "--user", "bob")
    destination_world = destination["world"]["id"]
    restored = _invoke(
        destination_dir, "restore", destination_world, str(archive), token=destination["token"]
    )
    assert restored == {"imported": 1, "replayed": 0}
    with _daemon(destination_dir) as destination_url:
        found = _invoke(
            destination_dir,
            "search",
            "--source",
            "portable evidence",
            "--world",
            destination_world,
            url=destination_url,
            token=destination["token"],
        )
        assert len(found["results"]) == 1
        episode = found["results"][0]
        assert episode["actor_id"] == "bob" and episode["world_id"] == destination_world
        assert (
            episode["metadata"]["memory_worlds_origin"]["episode_id"] == captured["episode"]["id"]
        )
        assert episode["projection_state"] == "pending"


def test_cli_acknowledged_capture_survives_daemon_restart(tmp_path):
    data_dir = tmp_path / "restart"
    initialized = _invoke(data_dir, "init", "--user", "alice")
    world, token = initialized["world"]["id"], initialized["token"]
    with _daemon(data_dir) as url:
        receipt = _invoke(
            data_dir,
            "capture",
            world,
            "--source-id",
            "restart-cli",
            "--at",
            AT,
            "--text",
            "Durable restart fixture",
            url=url,
            token=token,
        )
    with _daemon(data_dir) as url:
        found = _invoke(
            data_dir,
            "search",
            "--source",
            "restart fixture",
            "--world",
            world,
            url=url,
            token=token,
        )
        assert [item["id"] for item in found["results"]] == [receipt["episode"]["id"]]
        status = _invoke(data_dir, "status", world, url=url, token=token)
        assert status["episodes"] == 1


def test_cli_rejects_unapproved_network_binding(tmp_path):
    result = _invoke(tmp_path / "network", "serve", "--host", "0.0.0.0", success=False)
    assert "non-loopback binding requires --allow-network" in result.stderr


async def test_real_stdio_mcp_client_captures_into_same_http_authority(running_authority):
    from mcp import Client as MCPClient
    from mcp import StdioServerParameters

    data_dir, url, administrator, world = running_authority
    agent = _invoke(
        data_dir,
        "token-create",
        "--user",
        "alice",
        "--agent",
        "mcp-agent",
        "--grant",
        f"{world}:writer",
        url=url,
        token=administrator,
    )["token"]
    environment = _environment()
    environment["MEMORY_WORLDS_TOKEN"] = agent
    parameters = StdioServerParameters(
        command=sys.executable,
        args=_command(data_dir, "mcp", url=url)[1:],
        env=environment,
        cwd=ROOT,
    )
    async with MCPClient(parameters, read_timeout_seconds=10, raise_exceptions=True) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "memory_discover",
            "memory_worlds",
            "memory_search",
            "memory_capture",
            "memory_publish",
            "memory_mounts",
            "memory_remote_search",
            "memory_deliveries",
            "memory_scopes",
            "memory_scope_status",
            "memory_scope_flow",
            "memory_context",
            "memory_bind_context",
            "memory_episodes",
            "memory_threads",
            "memory_thread",
            "memory_context_bundle",
            "memory_maintenance_submit",
            "memory_maintenance_status",
            "memory_maintenance_wait",
            "memory_maintenance_cancel",
            "memory_maintenance_artifact",
        }
        discovered = await client.call_tool("memory_discover", {})
        assert not discovered.is_error
        assert [row["id"] for row in discovered.structured_content["worlds"]] == [world]
        assert [row["kind"] for row in discovered.structured_content["scopes"][world]] == [
            "world-shared"
        ]
        assert agent not in discovered.model_dump_json()
        captured = await client.call_tool(
            "memory_capture",
            {
                "world_id": world,
                "source_id": "stdio-mcp-1",
                "content": "Synthetic stdio transport evidence",
                "occurred_at": AT,
            },
        )
        assert not captured.is_error
        assert captured.structured_content["episode"]["agent_id"] == "mcp-agent"
        found = await client.call_tool(
            "memory_search",
            {
                "world_ids": [world],
                "query": "stdio transport",
                "graph": False,
                "limit": 5,
            },
        )
        assert not found.is_error
        assert found.structured_content["results"][0]["source_id"] == "stdio-mcp-1"
    found = _invoke(
        data_dir,
        "search",
        "--source",
        "stdio transport",
        "--world",
        world,
        url=url,
        token=administrator,
    )
    assert found["results"][0]["agent_id"] == "mcp-agent"
