#!/usr/bin/env python3
"""Prove local Cilium ingress/egress enforcement with healthy synthetic Pods.

Each run creates and removes two uniquely named namespaces. It never switches
the current context, changes an existing namespace, or contacts an external API.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

SERVER = """from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'local-network-fixture')
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""
PROBE = """import sys,urllib.request
try:
 with urllib.request.urlopen('http://'+sys.argv[1]+':8080',timeout=2) as response:
  assert response.read()==b'local-network-fixture'
except Exception:sys.exit(1)
"""


def pod(name: str, namespace: str, image: str, *, server: bool = False) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": {"fixture": name}},
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [
                {
                    "name": "fixture",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "python",
                        "-u",
                        "-c",
                        SERVER if server else "import time;time.sleep(1800)",
                    ],
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"memory": "128Mi"},
                    },
                }
            ],
        },
    }


def policy(name: str, namespace: str, selector: str, direction: str, rules: list) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {"fixture": selector}},
            "policyTypes": [direction.title()],
            direction: rules,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.context.startswith("kind-memory-worlds-"):
        parser.error("Choose the explicitly selected isolated Memory Worlds kind context")
    if args.output.exists():
        parser.error("Output already exists")
    kube = [
        os.environ.get("KUBECTL", "kubectl"),
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
    ]
    suffix = uuid4().hex[:10]
    namespaces = [f"memory-worlds-network-{suffix}", f"memory-worlds-outsider-{suffix}"]
    created = []

    def run(*command, body=None, check=True):
        result = subprocess.run(
            [*kube, *command],
            input=None if body is None else json.dumps(body),
            text=True,
            capture_output=True,
            timeout=180,
        )
        if check and result.returncode:
            raise RuntimeError(f"Local network qualification kubectl {command[0]} failed")
        return result

    def apply(resource):
        run("apply", "-f", "-", body=resource)

    def probe(namespace, name, address):
        return (
            run(
                "-n", namespace, "exec", name, "--", "python", "-c", PROBE, address, check=False
            ).returncode
            == 0
        )

    def settle(expectations):
        deadline = time.monotonic() + 45
        # Require multiple consistent observations: one dropped packet is not
        # evidence of a policy, and one successful connection may predate it.
        consistent = 0
        while time.monotonic() < deadline:
            outcomes = [
                probe(ns, name, address) == expected for ns, name, address, expected in expectations
            ]
            consistent = consistent + 1 if all(outcomes) else 0
            if consistent >= 3:
                return
            time.sleep(0.25)
        raise RuntimeError("Local policy did not converge to the expected allow/deny behavior")

    try:
        cilium = json.loads(
            run("-n", "kube-system", "get", "daemonset", "cilium", "-o", "json").stdout
        )
        if not cilium["status"].get("numberReady"):
            raise RuntimeError("Cilium is not ready in the selected local cluster")
        for namespace in namespaces:
            run("create", "namespace", namespace)
            created.append(namespace)
        local, outsider = namespaces
        for resource in (
            pod("target", local, args.api_image, server=True),
            pod("allowed", local, args.api_image),
            pod("denied", local, args.api_image),
            pod("outsider", outsider, args.api_image),
        ):
            apply(resource)
        for namespace in namespaces:
            run("-n", namespace, "wait", "--for=condition=Ready", "pods", "--all", "--timeout=120s")
        target = json.loads(run("-n", local, "get", "pod", "target", "-o", "json").stdout)
        address = target["status"]["podIP"]
        clients = [
            (local, "allowed", address),
            (local, "denied", address),
            (outsider, "outsider", address),
        ]
        settle([(*item, True) for item in clients])
        apply(
            policy(
                "selected-ingress",
                local,
                "target",
                "ingress",
                [
                    {
                        "from": [{"podSelector": {"matchLabels": {"fixture": "allowed"}}}],
                        "ports": [{"port": 8080, "protocol": "TCP"}],
                    }
                ],
            )
        )
        settle([(*clients[0], True), (*clients[1], False), (*clients[2], False)])
        apply(policy("selected-egress", local, "allowed", "egress", []))
        settle([(*clients[0], False)])
        apply(
            policy(
                "selected-egress",
                local,
                "allowed",
                "egress",
                [
                    {
                        "to": [{"podSelector": {"matchLabels": {"fixture": "target"}}}],
                        "ports": [{"port": 8080, "protocol": "TCP"}],
                    }
                ],
            )
        )
        settle([(*clients[0], True), (*clients[1], False), (*clients[2], False)])
        report = {
            "kind": "kind-cilium-network-policy",
            "passed": True,
            "api_image": args.api_image,
            "target_image_id": target["status"]["containerStatuses"][0]["imageID"],
            "cilium_images": [c["image"] for c in cilium["spec"]["template"]["spec"]["containers"]],
            "checks": {
                "healthy_baseline_all_clients": True,
                "selected_ingress_allowed": True,
                "other_same_namespace_denied": True,
                "other_namespace_denied": True,
                "selected_egress_denied": True,
                "explicit_egress_allowed": True,
            },
            "fixture_sha256": hashlib.sha256((SERVER + PROBE).encode()).hexdigest(),
            "limitation": (
                "Local CNI enforcement; production routing and firewall configuration "
                "are not provisioned."
            ),
        }
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print(json.dumps(report))
    finally:
        for namespace in reversed(created):
            with contextlib.suppress(Exception):
                run("delete", "namespace", namespace, "--wait=true", "--timeout=90s")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Local network qualification failed; verify the isolated Cilium cluster."
        ) from None
