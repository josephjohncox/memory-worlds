#!/usr/bin/env python3
"""Install and upgrade the Helm chart on an explicitly selected disposable kind cluster.

Verifies authentication, pod replacement, and PVC retention across uninstall/reinstall.
All source and graph records are synthetic. Credentials stay in the private state directory.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--image", required=True, help="Unique image tag already loaded in kind")
    parser.add_argument("--namespace", default="memory-worlds-helm")
    parser.add_argument("--release", default="memory-worlds")
    args = parser.parse_args()
    if not re.fullmatch(r"kind-memory-worlds-[a-z0-9-]+", args.context):
        raise SystemExit("Select an isolated Memory Worlds kind cluster explicitly")
    for name in (args.namespace, args.release):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,38}[a-z0-9]", name):
            raise SystemExit("Use a short DNS-compatible namespace and release name")
    if not re.fullmatch(r"memory-worlds:[a-zA-Z0-9_.-]+", args.image):
        raise SystemExit("Smoke requires a uniquely tagged, locally loaded Memory Worlds image")
    args.state_dir = args.state_dir.resolve()
    if args.state_dir.is_relative_to(ROOT):
        raise SystemExit("Keep smoke credentials outside the source repository")
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.state_dir.chmod(0o700)
    bootstrap_file = args.state_dir / "helm-bootstrap.json"
    if bootstrap_file.exists():
        raise SystemExit("Smoke requires fresh private state")
    kube = [
        os.environ.get("KUBECTL", "kubectl"),
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
        "-n",
        args.namespace,
    ]
    helm = [
        os.environ.get("HELM", "helm"),
        "--kubeconfig",
        str(args.kubeconfig),
        "--kube-context",
        args.context,
        "--namespace",
        args.namespace,
    ]

    def command(arguments, *, check=True):
        result = subprocess.run(arguments, text=True, capture_output=True, timeout=420)
        if check and result.returncode:
            diagnostic = args.state_dir / "helm-failed-command.txt"
            diagnostic.write_text(result.stdout + result.stderr)
            diagnostic.chmod(0o600)
            raise RuntimeError("Deployment command failed; inspect the private state directory")
        return result

    def kubectl(*arguments):
        return command([*kube, *arguments]).stdout

    if command([*kube, "get", "namespace", args.namespace], check=False).returncode == 0:
        raise SystemExit("Smoke refuses to reuse an existing namespace")
    kubectl("create", "namespace", args.namespace)
    password = args.state_dir / "helm-graph-password"
    password.write_text(secrets.token_hex(32))
    password.chmod(0o600)
    kubectl(
        "create",
        "secret",
        "generic",
        "memory-worlds-falkordb",
        f"--from-file=password={password}",
    )
    repository, tag = args.image.rsplit(":", 1)
    values = args.state_dir / "helm-values.json"
    values.write_text(
        json.dumps(
            {
                "image": {"repository": repository, "tag": tag, "pullPolicy": "Never"},
                "falkordb": {"image": {"pullPolicy": "IfNotPresent"}},
            }
        )
    )
    chart = ROOT / "deploy/helm/memory-worlds"
    settings = ["--values", str(values), "--wait", "--timeout", "5m"]

    def install():
        command([*helm, "install", args.release, str(chart), *settings])

    install()
    labels = f"app.kubernetes.io/instance={args.release}"
    workloads = json.loads(kubectl("get", "statefulsets", "-l", labels, "-o", "json"))["items"]
    by_component = {
        item["metadata"]["labels"]["app.kubernetes.io/component"]: item["metadata"]["name"]
        for item in workloads
    }
    if set(by_component) != {"api", "falkordb"}:
        raise RuntimeError("Smoke requires the default API and graph workloads")
    api, graph = by_component["api"], by_component["falkordb"]

    def rollout():
        for name in (graph, api):
            kubectl("rollout", "status", f"statefulset/{name}", "--timeout=240s")

    def pvc_ids():
        return {
            item["metadata"]["name"]: item["metadata"]["uid"]
            for item in json.loads(kubectl("get", "pvc", "-o", "json"))["items"]
        }

    claims = pvc_ids()
    if len(claims) != 2:
        raise RuntimeError("Expected separate API and graph PVCs")
    # Helm test runs without service credentials and verifies readiness and 401 behavior.
    command([*helm, "test", args.release, "--timeout", "3m"])
    bootstrap_file.write_text(kubectl("exec", f"{api}-0", "--", "memory-worlds", "init"))
    bootstrap_file.chmod(0o600)
    bootstrap = json.loads(bootstrap_file.read_text())
    token, world = bootstrap["token"], bootstrap["world"]["id"]
    authority = bootstrap["world"]["authority_id"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log_file = args.state_dir / "helm-port-forward.log"
    log_file.touch(mode=0o600)

    def call(path, payload=None, *, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None,
        )
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    @contextlib.contextmanager
    def forwarded():
        with log_file.open("a") as log:
            process = subprocess.Popen(
                [*kube, "port-forward", "--address=127.0.0.1", f"service/{api}", f"{port}:8787"],
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
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("API forwarding failed") from None
                        time.sleep(0.2)
                yield
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

    source = {
        "source_id": "helm-pvc-1",
        "content": "Synthetic nickel fixture from Helm",
        "occurred_at": "2026-10-20T00:00:00Z",
        "project": False,
    }
    with forwarded():
        receipt = call(f"/v1/worlds/{world}/episodes", source)
        replay = call(f"/v1/worlds/{world}/episodes", source)
        assert receipt["episode"]["id"] == replay["episode"]["id"] and replay["replayed"]

    def graph_query(query):
        return kubectl("exec", f"{graph}-0", "--", "redis-cli", "GRAPH.QUERY", "helm-smoke", query)

    graph_query("CREATE (:Smoke {value:'synthetic-helm-retention'})")
    assert kubectl("exec", f"{graph}-0", "--", "redis-cli", "SAVE").strip() == "OK"
    unauthenticated = kubectl(
        "exec", f"{graph}-0", "--", "env", "-u", "REDISCLI_AUTH", "redis-cli", "PING"
    )
    assert "NOAUTH" in unauthenticated

    def verify():
        assert pvc_ids() == claims
        with forwarded():
            assert call("/v1/me")["authority_id"] == authority
            recovered = call(f"/v1/worlds/{world}/episodes/{receipt['episode']['id']}")
            assert recovered["content"] == source["content"]
            assert call(f"/v1/worlds/{world}/episodes", source)["replayed"]
        assert "synthetic-helm-retention" in graph_query("MATCH (n:Smoke) RETURN n.value")

    kubectl("delete", "pod", f"{api}-0", f"{graph}-0", "--wait=true", "--timeout=120s")
    rollout()
    verify()
    command(
        [
            *helm,
            "upgrade",
            args.release,
            str(chart),
            *settings,
            "--set-string",
            "resources.requests.cpu=110m",
        ]
    )
    verify()
    command([*helm, "uninstall", args.release, "--wait", "--timeout", "3m"])
    assert pvc_ids() == claims
    install()
    verify()
    command([*helm, "test", args.release, "--timeout", "3m"])
    command([*helm, "uninstall", args.release, "--wait", "--timeout", "3m"])
    assert pvc_ids() == claims
    versions = json.loads(kubectl("version", "-o", "json"))
    chart_digest = hashlib.sha256()
    for file in sorted(chart.rglob("*")):
        if file.is_file():
            chart_digest.update(str(file.relative_to(chart)).encode() + b"\0" + file.read_bytes())
    record = {
        "result": "pass",
        "tested_at": datetime.now(UTC).isoformat(),
        "helm": command([helm[0], "version", "--short"]).stdout.strip(),
        "kubernetes": versions["serverVersion"]["gitVersion"],
        "api_image": args.image,
        "chart_tree_sha256": chart_digest.hexdigest(),
        "helm_test_authentication": True,
        "source_replay_idempotent": True,
        "source_and_graph_after_pod_replacement": True,
        "source_and_graph_after_upgrade": True,
        "source_and_graph_after_uninstall_reinstall": True,
        "pvc_uids_retained": True,
        "graph_authentication": True,
        "network_policy_enforcement": "not tested; kindnet does not enforce policies",
        "node_or_volume_loss_recovery": "not tested",
        "real_model_quality": "not tested",
    }
    (args.state_dir / "helm-smoke.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
