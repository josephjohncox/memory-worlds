#!/usr/bin/env python3
"""Qualify the real Helm/PostgreSQL deployment and native lost-volume recovery.

This destructive test owns a newly generated namespace. Only that namespace's
PostgreSQL and FalkorDB PVCs are destroyed. All inputs, accounts and keys are
synthetic; no real identity or model provider is contacted.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
POSTGRES_IMAGE = (
    "postgres:18.6-bookworm@sha256:ed9475dab2438f7a8ee063fa29a0740e7fbf7185e5549db1866eb722f12b49f8"
)
FALKOR_IMAGE = (
    "falkordb/falkordb-server:6.0.0@"
    "sha256:09928c7f8335147f13eec1d686e10a5a9a5a53f2e6df520f2847f94cb7e14925"
)
CLIENT = """import json,sys,hashlib,httpx
request=json.load(sys.stdin)
with httpx.Client(timeout=180,trust_env=False) as client:
 response=client.request(request.get('method','GET'),request['url'],json=request.get('body'),
  headers={'Authorization':'Bearer '+request['token']} if request.get('token') else {})
 result={'status':response.status_code}
 if request.get('download') and response.status_code==200:
  import os
  with open(request['download'],'xb') as out:out.write(response.content)
  os.chmod(request['download'],0o600)
  result.update(bytes=len(response.content),sha256=hashlib.sha256(response.content).hexdigest())
 else:
  try:result['body']=response.json()
  except ValueError:result['body']=response.text
 print(json.dumps(result))
"""
BOOTSTRAP = """import json,os
from pathlib import Path
from memory_worlds.store import MemoryStore
store=MemoryStore.from_env(Path('/tmp/bootstrap'))
world,token=store.bootstrap('synthetic-kind-operator')
Path('/tmp/operator.token').write_text(token);os.chmod('/tmp/operator.token',0o600)
print(json.dumps({'world':world.model_dump(mode='json'),'token':token}))
"""
MODEL_SERVER = """import importlib.util,json
from http.server import ThreadingHTTPServer
spec=importlib.util.spec_from_file_location('fixture','/qualification/qualify-graph.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class Handler(module.ModelHandler):
 def do_GET(self):
  if self.path!='/qualification/status':self.send_error(404);return
  body=json.dumps(dict(self.operations)).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(body)))
  self.end_headers();self.wfile.write(body)
ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
"""


def pvc(name: str, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
    }


def service(name: str, namespace: str, port: int) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": {"fixture": name}, "ports": [{"port": port}]},
    }


def chart_values(image: str, namespace: str, authority: str) -> dict:
    if "@" in image:
        repository, digest = image.split("@", 1)
        image_config = {"repository": repository, "digest": digest, "tag": ""}
    else:
        repository, tag = image.rsplit(":", 1)
        image_config = {"repository": repository, "tag": tag}
    peer = {"podSelector": {"matchLabels": {"fixture": "postgres"}}}
    return {
        "fullnameOverride": "qualification",
        "image": image_config,
        "replicaCount": 2,
        "runtime": {"existingSecret": "runtime"},
        "storage": {
            "backend": "postgres",
            "postgres": {"existingSecret": "postgres", "authorityId": authority},
        },
        "backup": {
            "enabled": True,
            "existingClaim": "backup-artifacts",
            "keyExistingSecret": "backup-key",
            "cronJob": {
                "enabled": True,
                "suspend": True,
                "outputExistingClaim": "downloaded-archives",
                "adminTokenExistingSecret": "backup-admin",
                "graphMode": "native",
            },
        },
        "metrics": {"enabled": True},
        "falkordb": {"auth": {"existingSecret": "falkordb"}, "persistence": {"size": "1Gi"}},
        "tests": {"enabled": False},
        "networkPolicy": {
            "postgresEgress": [{"to": [peer], "ports": [{"protocol": "TCP", "port": 5432}]}],
            "extraAPIEgress": [
                {
                    "to": [{"podSelector": {"matchLabels": {"fixture": "model"}}}],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                }
            ],
        },
    }


def helper_pod(namespace: str, image: str, authority: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "operator",
            "namespace": namespace,
            "labels": {"fixture": "operator", "memory-worlds-client": "true"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "securityContext": {"fsGroup": 10001},
            "containers": [
                {
                    "name": "operator",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-c", "import time;time.sleep(3600)"],
                    "envFrom": [{"secretRef": {"name": "runtime"}}],
                    "env": [
                        {"name": "MEMORY_WORLDS_STORAGE_BACKEND", "value": "postgres"},
                        {"name": "MEMORY_WORLDS_AUTHORITY_ID", "value": authority},
                        {
                            "name": "MEMORY_WORLDS_DATABASE_URL",
                            "valueFrom": {
                                "secretKeyRef": {"name": "postgres", "key": "database-url"}
                            },
                        },
                        {"name": "MEMORY_WORLDS_GRAPH_BACKEND", "value": "falkordb"},
                        {"name": "MEMORY_WORLDS_FALKORDB_HOST", "value": "qualification-falkordb"},
                        {
                            "name": "MEMORY_WORLDS_FALKORDB_PASSWORD",
                            "valueFrom": {"secretKeyRef": {"name": "falkordb", "key": "password"}},
                        },
                    ],
                    "volumeMounts": [
                        {"name": "archives", "mountPath": "/archives"},
                        {
                            "name": "key",
                            "mountPath": "/secrets/key",
                            "subPath": "key",
                            "readOnly": True,
                        },
                    ],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "128Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                }
            ],
            "volumes": [
                {"name": "archives", "persistentVolumeClaim": {"claimName": "downloaded-archives"}},
                {"name": "key", "secret": {"secretName": "backup-key", "defaultMode": 288}},
            ],
        },
    }


def postgres_deployment(namespace: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "postgres", "namespace": namespace},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"fixture": "postgres"}},
            "template": {
                "metadata": {"labels": {"fixture": "postgres"}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "postgres",
                            "image": POSTGRES_IMAGE,
                            "env": [
                                {
                                    "name": "POSTGRES_PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {"name": "postgres", "key": "password"}
                                    },
                                },
                                {"name": "POSTGRES_DB", "value": "memory_worlds"},
                                {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                            ],
                            "ports": [{"containerPort": 5432}],
                            "readinessProbe": {
                                "exec": {"command": ["pg_isready", "-U", "postgres"]},
                                "periodSeconds": 2,
                            },
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/var/lib/postgresql/data"}
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"memory": "512Mi"},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "postgres-data"}}
                    ],
                },
            },
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.context.startswith("kind-memory-worlds-qualification-"):
        parser.error("Select the explicitly owned isolated qualification kind context")
    if args.output.exists():
        parser.error("Output already exists")
    kube = [
        os.environ.get("KUBECTL", "kubectl"),
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
    ]
    namespace = "memory-worlds-deployment-" + uuid4().hex[:10]
    authority = str(uuid4())
    created = False
    stage = "setup"

    def run(*command, body=None, check=True, timeout=300):
        result = subprocess.run(
            [*kube, *command], input=body, text=True, capture_output=True, timeout=timeout
        )
        if check and result.returncode:
            raise RuntimeError(f"Local deployment qualification failed during {stage}")
        return result

    def apply(resource):
        run("apply", "-f", "-", body=json.dumps(resource))

    def secret(name, values):
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace},
                "data": {
                    k: base64.b64encode(v if isinstance(v, bytes) else v.encode()).decode()
                    for k, v in values.items()
                },
            }
        )

    def execute(code, body=None):
        return run(
            "-n",
            namespace,
            "exec",
            "-i",
            "operator",
            "--",
            "python",
            "-c",
            code,
            body=None if body is None else json.dumps(body),
        ).stdout

    def call(path, *, token=None, body=None, method="GET", expected=200, host=None, download=None):
        result = json.loads(
            execute(
                CLIENT,
                {
                    "url": (host or "http://qualification:8787") + path,
                    "token": token,
                    "body": body,
                    "method": method,
                    "download": download,
                },
            )
        )
        if result["status"] != expected:
            raise RuntimeError(
                f"Local deployment HTTP assertion failed during {stage}: "
                f"expected {expected}, received {result['status']}"
            )
        return result if download else result.get("body")

    def get_pods():
        return json.loads(
            run(
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/instance=qualification,app.kubernetes.io/component=api",
                "-o",
                "json",
            ).stdout
        )["items"]

    def wait_app():
        run("-n", namespace, "rollout", "status", "deployment/qualification", "--timeout=180s")
        pods = get_pods()
        if len(pods) != 2 or not all(
            all(s["ready"] for s in p["status"].get("containerStatuses", [])) for p in pods
        ):
            raise RuntimeError("Two real API replicas were not ready")
        return pods

    def scale(kind, name, replicas):
        run("-n", namespace, "scale", f"{kind}/{name}", f"--replicas={replicas}")

    def drain_apis():
        scale("deployment", "qualification", 0)
        run(
            "-n",
            namespace,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app.kubernetes.io/instance=qualification,app.kubernetes.io/component=api",
            "--timeout=120s",
        )

    try:
        run("create", "namespace", namespace)
        created = True
        run("label", "namespace", namespace, "memory-worlds-client=true")
        password = secrets.token_hex(32)
        secret(
            "postgres",
            {
                "password": password,
                "database-url": f"postgresql+psycopg://postgres:{password}@postgres:5432/memory_worlds",
            },
        )
        secret("falkordb", {"password": secrets.token_hex(32)})
        secret("backup-key", {"key": secrets.token_bytes(32)})
        secret(
            "runtime",
            {
                "MEMORY_WORLDS_LLM_PROVIDER": "openai-compatible",
                "MEMORY_WORLDS_LLM_MODEL": "local-scripted-extractor",
                "MEMORY_WORLDS_LLM_API_KEY": "local-synthetic-fixture",
                "MEMORY_WORLDS_LLM_BASE_URL": "http://model:8080/v1",
                "MEMORY_WORLDS_EMBEDDING_PROVIDER": "openai-compatible",
                "MEMORY_WORLDS_EMBEDDING_MODEL": "local-scripted-embedder",
                "MEMORY_WORLDS_EMBEDDING_API_KEY": "local-synthetic-fixture",
                "MEMORY_WORLDS_EMBEDDING_BASE_URL": "http://model:8080/v1",
                "MEMORY_WORLDS_EMBEDDING_DIMENSIONS": "16",
            },
        )
        for name in ("postgres-data", "backup-artifacts", "downloaded-archives"):
            apply(pvc(name, namespace))
        apply(service("postgres", namespace, 5432))
        apply(postgres_deployment(namespace))
        run("-n", namespace, "rollout", "status", "deployment/postgres", "--timeout=180s")
        apply(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "model", "namespace": namespace},
                "data": {"qualify-graph.py": (ROOT / "scripts/qualify-graph.py").read_text()},
            }
        )
        apply(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "model",
                    "namespace": namespace,
                    "labels": {"fixture": "model"},
                },
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "model",
                            "image": args.api_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-u", "-c", MODEL_SERVER],
                            "volumeMounts": [{"name": "code", "mountPath": "/qualification"}],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "128Mi"},
                                "limits": {"memory": "512Mi"},
                            },
                        }
                    ],
                    "volumes": [{"name": "code", "configMap": {"name": "model"}}],
                },
            }
        )
        apply(service("model", namespace, 8080))
        apply(helper_pod(namespace, args.api_image, authority))
        run(
            "-n",
            namespace,
            "wait",
            "--for=condition=Ready",
            "pod/operator",
            "pod/model",
            "--timeout=120s",
        )
        bootstrap = json.loads(execute(BOOTSTRAP))
        world, admin = bootstrap["world"], bootstrap["token"]
        secret("backup-admin", {"token": admin})
        # This maintenance client is separate from the chart's replica selector.
        apply(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "operator-graph", "namespace": namespace},
                "spec": {
                    "podSelector": {
                        "matchLabels": {
                            "app.kubernetes.io/instance": "qualification",
                            "app.kubernetes.io/component": "falkordb",
                        }
                    },
                    "policyTypes": ["Ingress"],
                    "ingress": [
                        {
                            "from": [{"podSelector": {"matchLabels": {"fixture": "operator"}}}],
                            "ports": [{"protocol": "TCP", "port": 6379}],
                        }
                    ],
                },
            }
        )
        with tempfile.TemporaryDirectory(prefix="memory-worlds-helm-qualification-") as temporary:
            values = Path(temporary) / "values.json"
            values.write_text(json.dumps(chart_values(args.api_image, namespace, authority)))
            stage = "Helm installation"
            result = subprocess.run(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    "qualification",
                    str(ROOT / "deploy/helm/memory-worlds"),
                    "--kubeconfig",
                    str(args.kubeconfig),
                    "--kube-context",
                    args.context,
                    "--namespace",
                    namespace,
                    "-f",
                    str(values),
                    "--wait",
                    "--timeout",
                    "5m",
                ],
                text=True,
                capture_output=True,
                timeout=330,
            )
            if result.returncode:
                raise RuntimeError("Local PostgreSQL Helm deployment failed")
        pods = wait_app()
        hosts = ["http://" + p["status"]["podIP"] + ":8787" for p in pods]
        images = sorted({p["status"]["containerStatuses"][0]["imageID"] for p in pods})
        stage = "replica writes and live authorization"
        scoped = f"/v1/worlds/{world['id']}/scopes/{world['scope_id']}"
        for index, host in enumerate(hosts):
            call(
                scoped + "/episodes",
                token=admin,
                host=host,
                method="POST",
                body={
                    "source_id": f"replica-{index}",
                    "content": f"Synthetic replica {index} source.",
                    "occurred_at": "2026-10-20T12:00:00Z",
                    "project": False,
                },
            )
        if len(call(scoped + "/episodes", token=admin, host=hosts[0])) != 2:
            raise RuntimeError("Replicas did not share source journals")
        child = call(
            "/v1/credentials/delegate",
            token=admin,
            host=hosts[0],
            method="POST",
            expected=201,
            body={
                "private_world_id": world["id"],
                "grants": [
                    {"world_id": world["id"], "scope_id": world["scope_id"], "operations": ["read"]}
                ],
            },
        )
        call("/v1/me", token=child["token"], host=hosts[1])
        call("/metrics", expected=401)
        call("/metrics", token=child["token"], expected=403)
        metrics = call("/metrics", token=admin)
        if "memory_worlds_" not in metrics:
            raise RuntimeError("Authenticated metrics were absent")
        who = call("/v1/me", token=child["token"], host=hosts[0])
        call(
            f"/v1/credentials/{who['credential_id']}",
            token=admin,
            host=hosts[1],
            method="DELETE",
            expected=204,
        )
        call("/v1/me", token=child["token"], host=hosts[0], expected=401)
        stage = "real Graphiti extraction"
        scopes, records = [], []
        for label, fixture in (("keep", "aster-cobalt"), ("erase", "beryl-ivory")):
            scope = call(
                f"/v1/worlds/{world['id']}/scopes",
                token=admin,
                method="POST",
                expected=201,
                body={"kind": "invocation-private", "name": label},
            )
            scopes.append(scope)
            fact = (
                "Aster uses the CobaltQueue." if label == "keep" else "Beryl owns the IvoryLathe."
            )
            records.append(
                call(
                    f"/v1/worlds/{world['id']}/scopes/{scope['id']}/episodes",
                    token=admin,
                    method="POST",
                    body={
                        "source_id": fixture,
                        "content": f"QFIX-{fixture}: {fact}",
                        "occurred_at": "2026-10-20T12:00:00Z",
                    },
                )["episode"]
            )

        def graph(scope, query):
            return call(
                "/v1/search",
                token=admin,
                method="POST",
                body={
                    "query": query,
                    "scopes": [{"world_id": world["id"], "scope_id": scope["id"]}],
                    "mode": "graph",
                    "include_entities": True,
                },
            )["results"]

        deadline = time.monotonic() + 120
        while True:
            try:
                hits = graph(scopes[0], "Aster queue")
                erased_hits = graph(scopes[1], "Beryl lathe")
                if hits and erased_hits:
                    break
            except RuntimeError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("Real scoped graph extraction did not become ready")
            time.sleep(1)
        if not any(records[0]["id"] in h.get("source_episode_ids", []) for h in hits):
            raise RuntimeError("Real graph recall lacks source provenance")
        stage = "API failover"
        old_name = pods[0]["metadata"]["name"]
        run("-n", namespace, "delete", "pod", old_name, "--wait=true")
        wait_app()
        if len(call(scoped + "/episodes", token=admin)) != 2 or not graph(scopes[0], "Aster queue"):
            raise RuntimeError("API replacement lost durable memory")
        stage = "erase and drained graph purge"
        erase_path = f"/v1/worlds/{world['id']}/scopes/{scopes[1]['id']}"
        operation = {
            "operation_id": str(uuid4()),
            "action": "erase",
            "episode_ids": [records[1]["id"]],
        }
        call(erase_path + "/lifecycle", token=admin, method="POST", body=operation)
        call(erase_path + f"/episodes/{records[1]['id']}", token=admin, expected=404)
        drain_apis()
        purge = run(
            "-n",
            namespace,
            "exec",
            "operator",
            "--",
            "memory-worlds",
            "--data-dir",
            "/tmp/maintenance",
            "--token-file",
            "/tmp/operator.token",
            "graph-purge",
            world["id"],
            "--scope",
            scopes[1]["id"],
            "--workers-drained",
        )
        purge_receipt = json.loads(purge.stdout)
        if purge_receipt.get("failed_generations") or not purge_receipt.get("purged_generations"):
            raise RuntimeError("Drained graph purge did not confirm removed generations")
        scale("deployment", "qualification", 2)
        wait_app()
        stage = "native online backup and CronJob"
        backup = call(
            "/v1/authority/backups",
            token=admin,
            method="POST",
            expected=201,
            body={"graph_mode": "native"},
        )
        archive = "/archives/native-recovery.mwauthority"
        downloaded = call(
            f"/v1/authority/backups/{backup['artifact_id']}", token=admin, download=archive
        )
        run(
            "-n",
            namespace,
            "create",
            "job",
            "scheduled-backup-proof",
            "--from=cronjob/qualification-backup",
        )
        run(
            "-n",
            namespace,
            "wait",
            "--for=condition=Complete",
            "job/scheduled-backup-proof",
            "--timeout=180s",
        )
        stage = "destroy owned SQL and graph volumes"
        drain_apis()
        scale("statefulset", "qualification-falkordb", 0)
        scale("deployment", "postgres", 0)
        run(
            "-n",
            namespace,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "fixture=postgres",
            "--timeout=120s",
        )
        run(
            "-n",
            namespace,
            "wait",
            "--for=delete",
            "pod",
            "-l",
            "app.kubernetes.io/instance=qualification,app.kubernetes.io/component=falkordb",
            "--timeout=120s",
        )
        claims = ("postgres-data", "data-qualification-falkordb-0")
        old_uids = {
            name: json.loads(run("-n", namespace, "get", "pvc", name, "-o", "json").stdout)[
                "metadata"
            ]["uid"]
            for name in claims
        }
        run("-n", namespace, "delete", "pvc", *claims, "--wait=true", "--timeout=120s")
        apply(pvc("postgres-data", namespace))
        scale("deployment", "postgres", 1)
        scale("statefulset", "qualification-falkordb", 1)
        run("-n", namespace, "rollout", "status", "deployment/postgres", "--timeout=180s")
        run(
            "-n",
            namespace,
            "rollout",
            "status",
            "statefulset/qualification-falkordb",
            "--timeout=180s",
        )
        for name, uid in old_uids.items():
            if (
                json.loads(run("-n", namespace, "get", "pvc", name, "-o", "json").stdout)[
                    "metadata"
                ]["uid"]
                == uid
            ):
                raise RuntimeError("Lost-volume test reused the original PVC")
        stage = "native recovery into empty SQL and graph backends"
        recovered = json.loads(
            run(
                "-n",
                namespace,
                "exec",
                "operator",
                "--",
                "memory-worlds",
                "--data-dir",
                "/tmp/recovered",
                "authority-restore",
                archive,
                "--key-file",
                "/secrets/key",
                "--native-graph",
            ).stdout
        )
        if not recovered.get("restored_graphs") or recovered.get("source_backend") != "postgres":
            raise RuntimeError("Native recovery did not restore PostgreSQL and graph namespaces")
        operations_before = call("/qualification/status", host="http://model:8080")
        scale("deployment", "qualification", 2)
        wait_app()
        if len(call(scoped + "/episodes", token=admin)) != 2:
            raise RuntimeError("Recovered source journals are incomplete")
        hits = graph(scopes[0], "Aster queue")
        if not any(records[0]["id"] in h.get("source_episode_ids", []) for h in hits):
            raise RuntimeError("Native graph recovery lost source provenance")
        operations_after = call("/qualification/status", host="http://model:8080")
        if operations_after["chat"] != operations_before["chat"]:
            raise RuntimeError("Native restore unexpectedly re-extracted graph facts")
        call("/v1/me", token=child["token"], expected=401)
        call(erase_path + f"/episodes/{records[1]['id']}", token=admin, expected=404)
        call(
            erase_path + "/episodes",
            token=admin,
            method="POST",
            expected=409,
            body={
                "source_id": "beryl-ivory",
                "content": "QFIX-beryl-ivory: Beryl owns the IvoryLathe.",
                "occurred_at": "2026-10-20T12:00:00Z",
            },
        )
        lifecycle = call(erase_path + "/lifecycle/" + operation["operation_id"], token=admin)
        report = {
            "kind": "kind-helm-postgres-native-recovery",
            "passed": True,
            "api_image": args.api_image,
            "api_image_ids": images,
            "postgres_image": POSTGRES_IMAGE,
            "archive_sha256": downloaded["sha256"],
            "archive_bytes": downloaded["bytes"],
            "restored_graphs": recovered["restored_graphs"],
            "model_operations": operations_after,
            "external_model_calls": 0,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "checks": {
                "two_replica_shared_writes": True,
                "cross_replica_live_revocation": True,
                "authenticated_admin_metrics": True,
                "real_graphiti_and_provenance": True,
                "pod_replacement_durability": True,
                "drained_graph_purge": True,
                "online_native_encrypted_backup": True,
                "backup_cronjob": True,
                "fresh_sql_and_graph_pvcs": True,
                "native_restore_without_reextraction": True,
                "revocation_survives_restore": True,
                "erased_source_does_not_resurrect": True,
            },
            "lifecycle_source_state": lifecycle["source_state"],
            "limitations": [
                "One local kind node; no multi-zone availability claim.",
                "Backup RWO claims are shared on one node; multiple-node replicas require RWX.",
                "Scripted local model; external model quality and RDS/Okta behavior are assumed.",
            ],
        }
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print(json.dumps(report))
    finally:
        if created:
            with contextlib.suppress(Exception):
                run("delete", "namespace", namespace, "--wait=true", "--timeout=120s")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Only our fixed operational messages are safe to report; third-party
        # exceptions may include source values or request credentials.
        message = (
            str(error) if type(error) is RuntimeError else "Local deployment qualification failed"
        )
        raise SystemExit(message) from None
