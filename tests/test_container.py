"""Opt-in deployment smoke test against the built local image."""

import json
import os
import subprocess
import time
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("MEMORY_WORLDS_RUN_CONTAINER_TEST") != "1",
    reason="set MEMORY_WORLDS_RUN_CONTAINER_TEST=1 after building memory-worlds:local",
)


def docker(*args):
    completed = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, check=False
    )
    if completed.returncode:
        raise AssertionError(f"Docker operation failed: {completed.stderr}")
    return completed.stdout.strip()


def test_container_private_context_restart_and_package_contents():
    image = os.getenv("MEMORY_WORLDS_TEST_IMAGE", "memory-worlds:local")
    identity = "memory-worlds-smoke-" + uuid4().hex[:12]
    container, volume = identity + "-api", identity + "-data"
    docker("volume", "create", volume)
    try:
        initialized = json.loads(
            docker(
                "run",
                "--rm",
                "--mount",
                f"type=volume,source={volume},target=/data",
                image,
                "init",
                "--user",
                "test-user",
            )
        )
        docker(
            "run",
            "--detach",
            "--name",
            container,
            "--publish",
            "127.0.0.1::8787",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--mount",
            f"type=volume,source={volume},target=/data",
            image,
        )
        mapping = docker("port", container, "8787/tcp")
        url = "http://" + mapping

        def ready():
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if (
                        httpx.get(url + "/healthz", timeout=0.25, trust_env=False).status_code
                        == 200
                    ):
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            raise AssertionError("container did not become healthy: " + docker("logs", container))

        ready()
        headers = {"Authorization": "Bearer " + initialized["token"]}
        world = initialized["world"]["id"]
        with httpx.Client(base_url=url, headers=headers, trust_env=False) as client:
            assert httpx.get(url + "/v1/worlds", trust_env=False).status_code == 401
            receipt = client.post(
                f"/v1/worlds/{world}/episodes",
                json={
                    "source_id": "container-restart",
                    "content": "Synthetic persistent container evidence",
                    "occurred_at": "2026-10-20T00:00:00Z",
                },
            )
            assert receipt.status_code == 200
            episode_id = receipt.json()["episode"]["id"]
            docker("restart", container)
            # Docker can reassign an automatically published host port on restart.
            url = "http://" + docker("port", container, "8787/tcp")
            client.base_url = url
            ready()
            found = client.post(
                "/v1/search",
                json={
                    "world_ids": [world],
                    "query": "persistent container",
                },
            )
            assert [row["id"] for row in found.json()["results"]] == [episode_id]
        contents = docker(
            "exec",
            container,
            "python",
            "-c",
            (
                "from pathlib import Path; "
                "p=Path('/app'); "
                "assert not list(p.rglob('ChatGPT-*.json')); "
                "assert not list(p.rglob('*.token')); "
                "assert not (p/'.aws').exists(); "
                "assert not (p/'.codex').exists(); "
                "assert not (p/'.git').exists(); "
                "assert not list((p/'src').rglob('*.pyc')); "
                "print('package context clean')"
            ),
        )
        assert contents == "package context clean"
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)
