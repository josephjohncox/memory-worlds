#!/usr/bin/env python3
"""Create one disposable kind/Cilium cluster without touching the default kubeconfig."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
CILIUM_VERSION = "1.20.3"
CILIUM_CHART = "oci://quay.io/cilium/charts/cilium"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.state_dir.absolute()
    if directory.exists():
        parser.error("Choose a new private state directory")
    os.umask(0o077)
    directory.mkdir(mode=0o700)
    name = "memory-worlds-qualification-" + uuid4().hex[:8]
    config = directory / "kind.yaml"
    config.write_bytes((ROOT / "deploy/qualification/kind.yaml").read_bytes())
    kubeconfig = directory / "kubeconfig"
    state = {
        "directory": str(directory),
        "name": name,
        "context": "kind-" + name,
        "kubeconfig": str(kubeconfig),
        "cilium": CILIUM_VERSION,
        "status": "creating",
        "kind_config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    }
    state_file = directory / "state.json"

    def save():
        state_file.write_text(json.dumps(state, indent=2) + "\n")

    save()
    with (directory / "cluster.log").open("w") as log:

        def run(*command):
            result = subprocess.run(command, stdout=log, stderr=log, timeout=600)
            log.flush()
            if result.returncode:
                raise RuntimeError(f"Local qualification cluster {command[0]} step failed")

        try:
            run(
                "kind",
                "create",
                "cluster",
                "--name",
                name,
                "--config",
                str(config),
                "--kubeconfig",
                str(kubeconfig),
                "--retain",
                "--wait",
                "0s",
            )
            kubeconfig.chmod(0o600)
            run(
                "helm",
                "pull",
                CILIUM_CHART,
                "--version",
                CILIUM_VERSION,
                "--destination",
                str(directory),
            )
            chart = directory / f"cilium-{CILIUM_VERSION}.tgz"
            state["cilium_chart_sha256"] = hashlib.sha256(chart.read_bytes()).hexdigest()
            run(
                "helm",
                "install",
                "cilium",
                str(chart),
                "--kubeconfig",
                str(kubeconfig),
                "--kube-context",
                state["context"],
                "--namespace",
                "kube-system",
                "-f",
                str(ROOT / "deploy/qualification/cilium.yaml"),
                "--wait",
                "--timeout",
                "5m",
            )
            run(
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "--context",
                state["context"],
                "wait",
                "--for=condition=Ready",
                "nodes",
                "--all",
                "--timeout=120s",
            )
            state["status"] = "ready"
        except Exception:
            state["status"] = "failed; cluster retained for local diagnosis"
            raise
        finally:
            save()
    print(json.dumps(state))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Local cluster setup failed; inspect its private cluster.log.") from None
