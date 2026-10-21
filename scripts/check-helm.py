#!/usr/bin/env python3
"""Render supported profiles and check storage, authentication, and release isolation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/memory-worlds"


def run(helm: str, *arguments: str, success: bool = True) -> str:
    result = subprocess.run(
        [helm, *arguments], cwd=ROOT, text=True, capture_output=True, timeout=60
    )
    if success and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    if not success and not result.returncode:
        raise AssertionError(f"Unsafe configuration was accepted: {arguments!r}")
    return result.stdout


def render(helm: str, release: str = "alpha", profile: str = "") -> list[dict]:
    arguments = [
        "template",
        release,
        str(CHART),
        "--namespace",
        "synthetic",
        "--kube-version",
        "1.37.0",
    ]
    if profile:
        arguments += ["--values", str(CHART / "ci" / f"{profile}.yaml")]
    return [item for item in yaml.safe_load_all(run(helm, *arguments)) if item]


def component(objects: list[dict], kind: str, name: str) -> dict:
    return next(
        item
        for item in objects
        if item["kind"] == kind
        and item["metadata"].get("labels", {}).get("app.kubernetes.io/component") == name
    )


def check_objects(objects: list[dict], release: str) -> None:
    assert not {"Secret", "ConfigMap"} & {item["kind"] for item in objects}
    assert len({(item["kind"], item["metadata"]["name"]) for item in objects}) == len(objects)
    for item in objects:
        assert len(item["metadata"]["name"]) <= 63
        assert item["metadata"]["labels"]["app.kubernetes.io/instance"] == release
        if item["kind"] == "Service":
            assert item["spec"]["type"] == "ClusterIP"
            assert item["spec"]["selector"]["app.kubernetes.io/instance"] == release
        if item["kind"] == "NetworkPolicy":
            assert (
                item["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"] == release
            )
        if item["kind"] != "StatefulSet":
            continue
        spec = item["spec"]
        assert spec["replicas"] == 1
        assert spec["persistentVolumeClaimRetentionPolicy"] == {
            "whenDeleted": "Retain",
            "whenScaled": "Retain",
        }
        assert spec["selector"]["matchLabels"]["app.kubernetes.io/instance"] == release
        pod = spec["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for container in pod["containers"]:
            security = container["securityContext"]
            assert security["readOnlyRootFilesystem"] is True
            assert security["allowPrivilegeEscalation"] is False
            assert security["capabilities"]["drop"] == ["ALL"]
            assert container["resources"]["requests"] and container["resources"]["limits"]
        for claim in spec.get("volumeClaimTemplates", []):
            assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    api = component(objects, "StatefulSet", "api")["spec"]["template"]["spec"]
    container = api["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    for volume in api["volumes"]:
        if "secret" in volume:
            assert volume["secret"]["defaultMode"] == 0o440
            mount = next(
                mount for mount in container["volumeMounts"] if mount["name"] == volume["name"]
            )
            assert mount["readOnly"] is True
    test = component(objects, "Pod", "test")
    assert test["metadata"]["annotations"]["helm.sh/hook"] == "test"
    assert test["spec"]["automountServiceAccountToken"] is False
    test_container = test["spec"]["containers"][0]
    assert not test_container.get("env") and not test_container.get("envFrom")
    assert "/readyz" in test_container["args"][0] and "== 401" in test_container["args"][0]


def validate(helm: str) -> dict:
    run(helm, "lint", "--strict", str(CHART))
    profiles = ["", "source", "graph", "work", "neptune", "existing-pvc"]
    for profile in profiles:
        objects = render(helm, profile=profile)
        check_objects(objects, "alpha")
        states = [item for item in objects if item["kind"] == "StatefulSet"]
        assert len(states) == (1 if profile in {"source", "neptune"} else 2)
        api = component(objects, "StatefulSet", "api")["spec"]
        env = {
            item["name"]: item.get("value")
            for item in api["template"]["spec"]["containers"][0]["env"]
        }
        expected_backend = (
            "falkordb"
            if profile in {"graph", "work"}
            else "neptune"
            if profile == "neptune"
            else ""
        )
        assert env["MEMORY_WORLDS_GRAPH_BACKEND"] == expected_backend
        if profile == "existing-pvc":
            assert all("volumeClaimTemplates" not in item["spec"] for item in states)
            assert all(
                any(
                    "persistentVolumeClaim" in volume
                    for volume in item["spec"]["template"]["spec"]["volumes"]
                )
                for item in states
            )
        if profile == "work":
            ingress = next(item for item in objects if item["kind"] == "Ingress")
            assert ingress["spec"]["tls"][0]["hosts"] == ["memory.example.org"]
            assert env["MEMORY_WORLDS_MOUNTS_FILE"] == "/config/mounts.json"
    first, second = render(helm, release="alpha"), render(helm, release="beta")
    check_objects(second, "beta")
    first_names = {item["metadata"]["name"] for item in first}
    second_names = {item["metadata"]["name"] for item in second}
    assert not first_names & second_names
    # Long release names must retain distinguishing suffixes after truncation.
    long_a = render(helm, release="a" * 48 + "one")
    long_b = render(helm, release="a" * 48 + "two")
    assert {item["metadata"]["name"] for item in long_a}.isdisjoint(
        item["metadata"]["name"] for item in long_b
    )
    numeric_release = render(helm, release="123")
    check_objects(numeric_release, "123")
    assert all(
        item["metadata"]["name"][0].isalpha()
        for item in numeric_release
        if item["kind"] == "Service"
    )
    negatives = [
        ["replicaCount=2"],
        ["replicaCount=0"],
        ["falkordb.replicaCount=2"],
        ["graph.backend=falkordb", "falkordb.enabled=false", "runtime.existingSecret=synthetic"],
        ["graph.backend=falkordb"],
        ["graph.backend=neptune", "runtime.existingSecret=synthetic"],
        ["router.enabled=true"],
        ["router.enabled=true", "router.mountsExistingSecret=synthetic"],
        ["falkordb.auth.existingSecret="],
        ["falkordb.auth.passwordKey="],
        ["ingress.enabled=true"],
        ["image.digest=sha256:bad"],
        ["image.repository=bad repository"],
        ["persistence.size=0Gi"],
        ["resources.limits.cpu=0"],
        ["resources.limits.memory=0Gi"],
        [
            "persistence.existingClaim=synthetic-shared-pvc",
            "falkordb.persistence.existingClaim=synthetic-shared-pvc",
        ],
        ["unknownSetting=true"],
        [
            "ingress.enabled=true",
            "ingress.className=nginx",
            "ingress.hosts[0]=memory.example.org",
            "ingress.tls[0].secretName=synthetic",
            "ingress.tls[0].hosts[0]=other.example.org",
        ],
    ]
    for settings in negatives:
        arguments = ["template", "synthetic", str(CHART)]
        for setting in settings:
            arguments.extend(["--set", setting])
        run(helm, *arguments, success=False)
    for value in (
        "networkPolicy.clientPodSelector.matchLabels=null",
        "networkPolicy.clientNamespaceSelector.matchLabels=null",
    ):
        run(helm, "template", "synthetic", str(CHART), "--set-json", value, success=False)
    run(helm, "template", "synthetic", str(CHART), "--kube-version", "1.31.0", success=False)
    return {
        "helm": run(helm, "version", "--short").strip(),
        "profiles": [profile or "default" for profile in profiles],
        "rejected_configurations": len(negatives) + 3,
        "release_isolation": True,
        "chart_license_matches_root": (CHART / "LICENSE").read_bytes()
        == (ROOT / "LICENSE").read_bytes(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helm", default=os.environ.get("HELM", "helm"))
    args = parser.parse_args()
    if not shutil.which(args.helm):
        parser.error("Helm is required; use --helm to select a private tool installation")
    receipt = validate(args.helm)
    assert receipt["chart_license_matches_root"]
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
