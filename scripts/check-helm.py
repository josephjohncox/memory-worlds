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
    arguments += ["--set", "runtime.existingSecret=synthetic-runtime"]
    if profile == "operations":
        arguments += [
            "--api-versions",
            "monitoring.coreos.com/v1/ServiceMonitor",
            "--api-versions",
            "monitoring.coreos.com/v1/PrometheusRule",
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


def api_workload(objects: list[dict]) -> dict:
    return next(
        item
        for item in objects
        if item["kind"] in {"StatefulSet", "Deployment"}
        and item["metadata"]["labels"].get("app.kubernetes.io/component") == "api"
    )


def check_pod(pod: dict) -> None:
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    for container in pod["containers"]:
        security = container["securityContext"]
        assert security["readOnlyRootFilesystem"] is True
        assert security["allowPrivilegeEscalation"] is False
        assert security["capabilities"]["drop"] == ["ALL"]
        assert container["resources"]["requests"] and container["resources"]["limits"]


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
        if item["kind"] not in {"StatefulSet", "Deployment"}:
            continue
        spec = item["spec"]
        if item["kind"] == "StatefulSet":
            assert spec["replicas"] == 1
            assert spec["persistentVolumeClaimRetentionPolicy"] == {
                "whenDeleted": "Retain",
                "whenScaled": "Retain",
            }
        else:
            assert 1 <= spec["replicas"] <= 32
            assert "volumeClaimTemplates" not in spec and "serviceName" not in spec
        assert spec["selector"]["matchLabels"]["app.kubernetes.io/instance"] == release
        pod = spec["template"]["spec"]
        check_pod(pod)
        for claim in spec.get("volumeClaimTemplates", []):
            assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    api = api_workload(objects)["spec"]["template"]["spec"]
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


def check_operations(objects: list[dict]) -> None:
    workload = api_workload(objects)
    api = workload["spec"]["template"]["spec"]
    container = api["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["MEMORY_WORLDS_BACKUP_DIRECTORY"]["value"] == "/backups"
    assert env["MEMORY_WORLDS_BACKUP_KEY_FILE"]["value"] == "/secrets/backup/key"
    key = next(mount for mount in container["volumeMounts"] if mount["name"] == "backup-key")
    assert key["readOnly"] is True and key["subPath"] == "key"
    cron = component(objects, "CronJob", "backup")["spec"]
    assert cron["concurrencyPolicy"] == "Forbid" and cron["timeZone"] == "Etc/UTC"
    assert cron["jobTemplate"]["spec"]["backoffLimit"] == 0
    pod = cron["jobTemplate"]["spec"]["template"]["spec"]
    check_pod(pod)
    job = pod["containers"][0]
    assert job["command"] == ["python", "-c"]
    assert not job.get("env") and not job.get("envFrom")
    compile(job["args"][0], "<rendered-backup-job>", "exec")
    assert '"authority-backup"' in job["args"][0]
    assert '"--token-file", "/secrets/backup/token"' in job["args"][0]
    assert {volume["secret"]["secretName"] for volume in pod["volumes"] if "secret" in volume} == {
        "synthetic-backup-admin"
    }
    assert not (
        {volume.get("persistentVolumeClaim", {}).get("claimName") for volume in pod["volumes"]}
        & {volume.get("persistentVolumeClaim", {}).get("claimName") for volume in api["volumes"]}
        - {None}
    )
    monitor = component(objects, "ServiceMonitor", "metrics")["spec"]
    assert monitor["selector"]["matchLabels"]["app.kubernetes.io/component"] == "api"
    assert monitor["namespaceSelector"] == {"matchNames": ["synthetic"]}
    endpoint = monitor["endpoints"][0]
    assert endpoint["path"] == "/metrics" and endpoint["port"] == "http"
    assert endpoint["authorization"] == {
        "type": "Bearer",
        "credentials": {"name": "synthetic-metrics-admin", "key": "token"},
    }
    rules = component(objects, "PrometheusRule", "metrics")["spec"]["groups"][0]["rules"]
    assert {item["alert"] for item in rules} >= {
        "MemoryWorldsScrapeUnavailable",
        "MemoryWorldsGraphPurgeFailed",
        "MemoryWorldsProjectionOutcomeUnknown",
        "MemoryWorldsPublicationBlocked",
    }
    policy = next(
        item
        for item in objects
        if item["kind"] == "NetworkPolicy" and item["metadata"]["name"].endswith("-backup")
    )
    assert policy["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 8787}]
    assert (
        policy["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"][
            "app.kubernetes.io/component"
        ]
        == "api"
    )


def validate(helm: str) -> dict:
    run(helm, "lint", "--strict", str(CHART), "--set", "runtime.existingSecret=synthetic-runtime")
    profiles = ["", "source", "graph", "work", "neptune", "existing-pvc", "postgres", "operations"]
    for profile in profiles:
        objects = render(helm, profile=profile)
        check_objects(objects, "alpha")
        states = [item for item in objects if item["kind"] == "StatefulSet"]
        postgres = profile in {"postgres", "operations"}
        assert len(states) == (1 if profile in {"source", "neptune"} or postgres else 2)
        workload = api_workload(objects)
        assert workload["kind"] == ("Deployment" if postgres else "StatefulSet")
        api = workload["spec"]
        env = {
            item["name"]: item.get("value")
            for item in api["template"]["spec"]["containers"][0]["env"]
        }
        expected_backend = (
            "falkordb"
            if profile not in {"source", "neptune"}
            else "neptune"
            if profile == "neptune"
            else "disabled"
        )
        assert env["MEMORY_WORLDS_GRAPH_BACKEND"] == expected_backend
        assert env["MEMORY_WORLDS_STORAGE_BACKEND"] == ("postgres" if postgres else "sqlite")
        pod = api["template"]["spec"]
        database = next(
            item
            for item in pod["containers"][0]["env"]
            if item["name"] == "MEMORY_WORLDS_DATABASE_URL"
        )
        if postgres:
            assert api["replicas"] == 2
            assert database["valueFrom"]["secretKeyRef"] == {
                "name": "synthetic-postgres",
                "key": "database-url",
            }
            assert env["MEMORY_WORLDS_AUTHORITY_ID"] == "7cfebda9-5ac1-4292-8242-06a0b605b700"
            assert next(volume for volume in pod["volumes"] if volume["name"] == "data")[
                "emptyDir"
            ] == {"sizeLimit": "128Mi"}
        else:
            assert database["value"] == "" and env["MEMORY_WORLDS_AUTHORITY_ID"] == ""
        if profile == "operations":
            check_operations(objects)
        if profile == "postgres":
            assert env["PGSSLROOTCERT"] == "/secrets/postgres/ca.pem"
            ca = next(
                mount
                for mount in pod["containers"][0]["volumeMounts"]
                if mount["name"] == "postgres-ca"
            )
            assert ca["readOnly"] is True and ca["subPath"] == "ca.pem"
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
        ["runtime.existingSecret="],
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
        ["storage.backend=postgres"],
        ["storage.backend=unknown"],
        ["backup.enabled=true"],
        ["backup.cronJob.enabled=true"],
        ["metrics.serviceMonitor.enabled=true"],
        ["metrics.prometheusRule.enabled=true"],
        ["replicaCount=33"],
        [
            "ingress.enabled=true",
            "ingress.className=nginx",
            "ingress.hosts[0]=memory.example.org",
            "ingress.tls[0].secretName=synthetic",
            "ingress.tls[0].hosts[0]=other.example.org",
        ],
    ]
    for settings in negatives:
        arguments = [
            "template",
            "synthetic",
            str(CHART),
            "--set",
            "runtime.existingSecret=synthetic-runtime",
        ]
        for setting in settings:
            arguments.extend(["--set", setting])
        run(helm, *arguments, success=False)
    # These failures are isolated from the default runtime-secret requirement.
    isolated = [
        ["--set", "storage.postgres.existingSecret="],
        ["--set", "storage.postgres.authorityId=not-a-uuid"],
        ["--set-json", "networkPolicy.postgresEgress=[]"],
        ["--set", "persistence.existingClaim=unused-sqlite-claim"],
        ["--set", "backup.keyExistingSecret="],
        ["--set", "backup.cronJob.adminTokenExistingSecret="],
        ["--set", "backup.cronJob.outputExistingClaim=synthetic-backup-artifacts"],
        ["--set", "metrics.serviceMonitor.existingSecret="],
        ["--set-json", 'metrics.serviceMonitor.labels={"app.kubernetes.io/instance":"foreign"}'],
        ["--set", "graph.backend=disabled"],
    ]
    for arguments in isolated:
        run(
            helm,
            "template",
            "synthetic",
            str(CHART),
            "--set",
            "runtime.existingSecret=synthetic-runtime",
            "--values",
            str(CHART / "ci/operations.yaml"),
            "--api-versions",
            "monitoring.coreos.com/v1/ServiceMonitor",
            "--api-versions",
            "monitoring.coreos.com/v1/PrometheusRule",
            *arguments,
            success=False,
        )
    run(
        helm,
        "template",
        "synthetic",
        str(CHART),
        "--set",
        "runtime.existingSecret=synthetic-runtime",
        "--values",
        str(CHART / "ci/operations.yaml"),
        success=False,
    )
    run(
        helm,
        "template",
        "synthetic",
        str(CHART),
        "--set",
        "runtime.existingSecret=synthetic-runtime",
        "--values",
        str(CHART / "ci/operations.yaml"),
        "--api-versions",
        "monitoring.coreos.com/v1/ServiceMonitor",
        "--api-versions",
        "monitoring.coreos.com/v1/PrometheusRule",
        "--set",
        "graph.backend=disabled",
        "--set",
        "falkordb.enabled=false",
        "--set",
        "backup.cronJob.graphMode=rebuild",
    )
    for value in (
        "networkPolicy.clientPodSelector.matchLabels=null",
        "networkPolicy.clientNamespaceSelector.matchLabels=null",
    ):
        run(
            helm,
            "template",
            "synthetic",
            str(CHART),
            "--set",
            "runtime.existingSecret=synthetic-runtime",
            "--set-json",
            value,
            success=False,
        )
    run(
        helm,
        "template",
        "synthetic",
        str(CHART),
        "--set",
        "runtime.existingSecret=synthetic-runtime",
        "--kube-version",
        "1.31.0",
        success=False,
    )
    return {
        "helm": run(helm, "version", "--short").strip(),
        "profiles": [profile or "default" for profile in profiles],
        "rejected_configurations": len(negatives) + len(isolated) + 4,
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
