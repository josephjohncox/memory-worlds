"""Two real CLI daemons: durable background federation without opening daemon databases."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-10-20T00:00:00+00:00"


def _environment(**extra):
    # Do not inherit graph providers, OIDC, proxy settings, mounts, or user credentials.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GRAPHITI_TELEMETRY_ENABLED": "false",
        **extra,
    }


def _command(data_dir, *arguments, url=None):
    command = [sys.executable, "-m", "memory_worlds.cli", "--data-dir", str(data_dir)]
    if url is not None:
        command += ["--url", url]
    return [*command, *arguments]


def _cli(data_dir, *arguments, url=None, token=None, text=None, denied=False):
    result = subprocess.run(
        _command(data_dir, *arguments, url=url),
        cwd=ROOT,
        env=_environment(**({"MEMORY_WORLDS_TOKEN": token} if token else {})),
        input=text,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if denied:
        assert result.returncode != 0, "Unauthorized CLI command unexpectedly succeeded"
        assert not result.stdout, "Failed command exposed a response body"
        return result.stderr
    assert result.returncode == 0, "Memory CLI command failed"
    return json.loads(result.stdout)


def _port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


@contextmanager
def _daemon(data_dir, port, *, mounts=None):
    url = f"http://127.0.0.1:{port}"
    settings = {"MEMORY_WORLDS_MOUNTS_FILE": str(mounts)} if mounts else {}
    process = subprocess.Popen(
        _command(data_dir, "serve", "--host", "127.0.0.1", "--port", str(port)),
        cwd=ROOT,
        env=_environment(**settings),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(base_url=url, timeout=0.2, trust_env=False) as client:
            while time.monotonic() < deadline:
                assert process.poll() is None, "Memory daemon exited before readiness"
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("Memory daemon did not become ready")
        yield url, process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise AssertionError("Owned memory daemon did not stop") from None


def _delivery(url, token, delivery_id, predicate):
    deadline = time.monotonic() + 20
    with httpx.Client(
        base_url=url, headers={"Authorization": "Bearer " + token}, timeout=1, trust_env=False
    ) as client:
        while time.monotonic() < deadline:
            response = client.get("/v1/router/deliveries")
            assert response.status_code == 200
            for row in response.json():
                if row["id"] == delivery_id and predicate(row):
                    return row
            time.sleep(0.1)
    raise AssertionError("Background publication did not reach its expected delivery state")


def test_cli_daemons_queue_offline_recover_after_kill_and_deliver_once(tmp_path):
    local_data, remote_data = tmp_path / "local", tmp_path / "remote"
    local_init = _cli(local_data, "init", "--user", "alice")
    remote_init = _cli(remote_data, "init", "--user", "remote-owner")
    local_port, remote_port = _port(), _port()
    while remote_port == local_port:
        remote_port = _port()
    # Prepare the shared authority through its real authenticated CLI, then take it offline.
    with _daemon(remote_data, remote_port) as (remote_url, _):
        team = _cli(
            remote_data,
            "world-create",
            "Data team",
            "--kind",
            "team",
            url=remote_url,
            token=remote_init["token"],
        )
        connector = _cli(
            remote_data,
            "token-create",
            "--user",
            "connector",
            "--agent",
            "local-router",
            "--grant",
            team["id"] + ":writer",
            url=remote_url,
            token=remote_init["token"],
        )
        _cli(
            remote_data,
            "capture",
            remote_init["world"]["id"],
            "--source-id",
            "remote-private",
            "--at",
            AT,
            text="remoteprivateonlymarker",
            url=remote_url,
            token=remote_init["token"],
        )
        denied = _cli(
            remote_data,
            "search",
            "remoteprivateonlymarker",
            "--world",
            remote_init["world"]["id"],
            url=remote_url,
            token=connector["token"],
            denied=True,
        )
        assert "HTTP 403" in denied

    secret = tmp_path / "connector.token"
    secret.write_text(connector["token"])
    secret.chmod(0o600)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps(
            {
                "version": 1,
                "mounts": [
                    {
                        "id": "work",
                        "name": "Data team",
                        "url": remote_url,
                        "authority_id": team["authority_id"],
                        "world_ids": [team["id"]],
                        "token_file": secret.name,
                        "user_grants": {"alice": "publisher"},
                        "publish": True,
                    }
                ],
            }
        )
    )
    mounts.chmod(0o600)

    with _daemon(local_data, local_port, mounts=mounts) as (local_url, original_process):
        publisher = _cli(
            local_data,
            "token-create",
            "--user",
            "alice",
            "--agent",
            "publisher",
            "--grant",
            local_init["world"]["id"] + ":writer",
            url=local_url,
            token=local_init["token"],
        )
        peer = _cli(
            local_data,
            "token-create",
            "--user",
            "alice",
            "--agent",
            "peer",
            url=local_url,
            token=local_init["token"],
        )
        other = _cli(
            local_data,
            "token-create",
            "--user",
            "bob",
            "--agent",
            "other",
            url=local_url,
            token=local_init["token"],
        )
        _cli(
            local_data,
            "capture",
            local_init["world"]["id"],
            "--source-id",
            "local-private",
            "--at",
            AT,
            text="localprivateonlymarker must remain private",
            url=local_url,
            token=publisher["token"],
        )
        selected = "Selected capacity evidence approved for the data team"
        publication_args = (
            "route-publish",
            "work",
            team["id"],
            "--source-id",
            "selected-note",
            "--at",
            AT,
        )
        queued = _cli(
            local_data, *publication_args, text=selected, url=local_url, token=publisher["token"]
        )
        assert queued["state"] == "pending"
        pending = _delivery(
            local_url,
            publisher["token"],
            queued["id"],
            lambda row: row["attempts"] >= 1 and row["state"] == "pending",
        )
        assert pending["error"] == "remote_unavailable"
        denied = _cli(
            local_data,
            "route-search",
            "capacity",
            "--mount",
            "work",
            url=local_url,
            token=other["token"],
            denied=True,
        )
        assert "HTTP 403" in denied
        assert _cli(local_data, "deliveries", url=local_url, token=other["token"]) == []
        # Simulate an abrupt local daemon failure with a durable, undelivered envelope.
        original_process.kill()
        original_process.wait(timeout=5)

    with _daemon(remote_data, remote_port) as (remote_url, _):
        with _daemon(local_data, local_port, mounts=mounts) as (local_url, restarted_process):
            assert restarted_process.pid != original_process.pid
            delivered = _delivery(
                local_url, peer["token"], queued["id"], lambda row: row["state"] == "delivered"
            )
            assert delivered["agent_id"] == "publisher"
            assert delivered["attempts"] >= 2
            receipt = _cli(local_data, "deliveries", url=local_url, token=peer["token"])[0]
            assert receipt["id"] == queued["id"] and receipt["remote_episode_id"]
            duplicate = _cli(
                local_data, *publication_args, text=selected, url=local_url, token=peer["token"]
            )
            assert duplicate["id"] == queued["id"] and duplicate["state"] == "delivered"
            recalled = _cli(
                local_data,
                "route-search",
                "capacity",
                "--mount",
                "work",
                url=local_url,
                token=peer["token"],
            )
            assert recalled["complete"] and len(recalled["results"]) == 1
            source = recalled["results"][0]
            assert source["id"] == receipt["remote_episode_id"]
            assert source["content"] == selected and source["source_type"] == "publication"
            assert source["authority_id"] == team["authority_id"]
            assert source["world_id"] == team["id"] and source["mount_id"] == "work"
            assert (
                source["source_id"]
                == f"publication:{local_init['world']['authority_id']}:{queued['id']}"
            )
            assert source["source_ref"] is None and source["metadata"] == {}
            status = _cli(
                remote_data, "status", team["id"], url=remote_url, token=connector["token"]
            )
            assert status["episodes"] == 1
            remote_private_search = _cli(
                remote_data,
                "search",
                "localprivateonlymarker",
                "--world",
                team["id"],
                url=remote_url,
                token=connector["token"],
            )
            assert remote_private_search["results"] == []
            local_private_search = _cli(
                local_data,
                "search",
                "localprivateonlymarker",
                "--world",
                local_init["world"]["id"],
                url=local_url,
                token=publisher["token"],
            )
            assert len(local_private_search["results"]) == 1
