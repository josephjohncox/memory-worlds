#!/usr/bin/env python3
"""Exercise a new, isolated deployment; writes only synthetic fixtures.

Pod deletion verifies PVC continuity, not recovery from volume or node loss.
The native graph test uses deterministic Cypher; it does not test model quality.
"""

import argparse
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not args.context.startswith("kind-memory-worlds-"):
        raise SystemExit("Smoke may only target an isolated Memory Worlds kind cluster")
    kube = [
        os.environ.get("KUBECTL", "kubectl"),
        "--kubeconfig",
        args.kubeconfig,
        "--context",
        args.context,
        "-n",
        "memory-worlds",
    ]

    def run(*command, input=None):
        result = subprocess.run(
            [*kube, *command], input=input, text=True, capture_output=True, check=False
        )
        if result.returncode:
            # Output can contain secrets if bootstrap fails. Retain it privately.
            (args.state_dir / "failed-command.txt").write_text(result.stdout + result.stderr)
            raise RuntimeError(f"kubectl {command[0]} failed; see private state directory")
        return result.stdout

    bootstrap_file = args.state_dir / "bootstrap.json"
    if bootstrap_file.exists():
        raise SystemExit("Smoke requires a fresh deployment state directory")
    bootstrap_file.write_text(run("exec", "memory-worlds-0", "--", "memory-worlds", "init"))
    bootstrap_file.chmod(0o600)
    bootstrap = json.loads(bootstrap_file.read_text())
    token = bootstrap["token"]
    world_id = bootstrap["world"]["id"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log = (args.state_dir / "port-forward.log").open("w")

    @contextlib.contextmanager
    def forwarded():
        process = subprocess.Popen(
            [*kube, "port-forward", "--address=127.0.0.1", "service/memory-worlds", f"{port}:8787"],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 40
            while True:
                try:
                    call("/readyz", authenticated=False)
                    break
                except (OSError, urllib.error.HTTPError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("API port forwarding did not become ready") from None
                    time.sleep(0.2)
            yield
        finally:
            process.terminate()
            process.wait(timeout=10)

    def call(path, body=None, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    source = {
        "source_id": "kind-pvc-smoke-1",
        "content": "Synthetic copper fixture retained in PVC",
        "occurred_at": "2026-10-20T00:00:00Z",
        "project": False,
    }
    with forwarded():
        try:
            call("/v1/worlds", authenticated=False)
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Unauthenticated source access succeeded")
        receipt = call(f"/v1/worlds/{world_id}/episodes", source)
        duplicate = call(f"/v1/worlds/{world_id}/episodes", source)
        assert receipt["episode"]["id"] == duplicate["episode"]["id"]
    graph_fixture = "CREATE (:Smoke {value: 'synthetic-pvc-proof'})"
    run("exec", "falkordb-0", "--", "redis-cli", "GRAPH.QUERY", "smoke", graph_fixture)
    assert run("exec", "falkordb-0", "--", "redis-cli", "SAVE").strip() == "OK"
    run("delete", "pod", "memory-worlds-0", "falkordb-0", "--wait=true", "--timeout=120s")
    for resource in ("falkordb", "memory-worlds"):
        run("rollout", "status", f"statefulset/{resource}", "--timeout=240s")
    with forwarded():
        episode_id = receipt["episode"]["id"]
        recovered = call(f"/v1/worlds/{world_id}/episodes/{episode_id}")
        assert recovered["content"] == source["content"]
        results = call(
            "/v1/search",
            {
                "world_ids": [world_id],
                "query": "copper",
                "mode": "source",
                "limit": 10,
            },
        )
        assert any(item["id"] == episode_id for item in results["results"])
    graph_result = run(
        "exec",
        "falkordb-0",
        "--",
        "redis-cli",
        "GRAPH.QUERY",
        "smoke",
        "MATCH (n:Smoke) RETURN n.value",
    )
    assert "synthetic-pvc-proof" in graph_result
    unauthenticated_graph = run(
        "exec", "falkordb-0", "--", "env", "-u", "REDISCLI_AUTH", "redis-cli", "PING"
    )
    assert "NOAUTH" in unauthenticated_graph
    versions = json.loads(run("version", "-o", "json"))
    pods = json.loads(run("get", "pods", "-o", "json"))
    record = {
        "result": "pass",
        "tested_at": datetime.now(UTC).isoformat(),
        "kubernetes": versions["serverVersion"]["gitVersion"],
        "kubectl": versions["clientVersion"]["gitVersion"],
        "images": {
            pod["metadata"]["name"]: [
                container["imageID"] for container in pod["status"]["containerStatuses"]
            ]
            for pod in pods["items"]
        },
        "source_capture": True,
        "source_replay_idempotent": True,
        "source_after_pod_replacement": True,
        "native_graph_after_pod_replacement": True,
        "api_authentication": True,
        "graph_authentication": True,
        "network_policy_enforcement": "not tested; kindnet does not enforce policies",
        "node_or_volume_loss_recovery": "not tested",
        "real_model_quality": "not tested",
    }
    (args.state_dir / "smoke.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    log.close()


if __name__ == "__main__":
    main()
