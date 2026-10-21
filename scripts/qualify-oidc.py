#!/usr/bin/env python3
"""Exercise an actual Keycloak container and Memory Worlds in an isolated kind Pod.

All identities and passwords are generated synthetic fixtures. The issuer is
pod-loopback and forwarded to host loopback at the same port. This avoids any
test-only relaxation of the verifier's trusted-URL or TLS rules.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import selectors
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent.parent
KEYCLOAK_IMAGE = (
    "quay.io/keycloak/keycloak:26.8.1@"
    "sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244"
)


def realm_document(password: str) -> dict:
    realm = json.loads((ROOT / "deploy/qualification/realm.json").read_text())
    groups = {
        "name": "groups",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-group-membership-mapper",
        "config": {
            "claim.name": "groups",
            "full.path": "false",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "userinfo.token.claim": "true",
        },
    }
    for name, audience in (
        ("mw-native", "api://memory-worlds"),
        ("mw-untrusted-client", "api://memory-worlds"),
        ("mw-wrong-audience", "api://different-api"),
    ):
        realm["clients"].append(
            {
                "clientId": name,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": True,
                "fullScopeAllowed": False,
                "directAccessGrantsEnabled": True,
                "standardFlowEnabled": True,
                "consentRequired": False,
                "attributes": {
                    "oauth2.device.authorization.grant.enabled": "true",
                    "oauth2.device.polling.interval": "1",
                },
                "optionalClientScopes": ["memory-worlds"],
                "protocolMappers": [
                    groups,
                    {
                        "name": "sub",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-sub-mapper",
                        "config": {"access.token.claim": "true"},
                    },
                    {
                        "name": "memory-worlds-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.custom.audience": audience,
                            "access.token.claim": "true",
                            "id.token.claim": "false",
                        },
                    },
                ],
            }
        )
    for name, memberships in (
        ("alice", ["/mw-company", "/mw-data-team"]),
        ("bob", ["/mw-company"]),
    ):
        realm["users"].append(
            {
                "username": name,
                "enabled": True,
                "emailVerified": True,
                "firstName": "Synthetic",
                "lastName": name.title(),
                "email": f"{name}@example.invalid",
                "groups": memberships,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            }
        )
    return realm


def deployment_document(namespace: str, api_image: str, issuer: str) -> dict:
    idp_port = urlsplit(issuer).port
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "oidc-qualification", "namespace": namespace},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "oidc-qualification"}},
            "template": {
                "metadata": {"labels": {"app": "oidc-qualification"}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"fsGroup": 10001},
                    "volumes": [
                        {"name": "data", "emptyDir": {}},
                        {"name": "runtime", "configMap": {"name": "qualification-runtime"}},
                        {"name": "realm", "secret": {"secretName": "qualification-idp"}},
                    ],
                    "initContainers": [
                        {
                            "name": "bootstrap",
                            "image": api_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "/qualification/runtime.py", "bootstrap"],
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "runtime", "mountPath": "/qualification"},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "api",
                            "image": api_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "/qualification/runtime.py", "serve"],
                            "env": [{"name": "QUALIFICATION_ISSUER", "value": issuer}],
                            "ports": [{"name": "api", "containerPort": 8787}],
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "runtime", "mountPath": "/qualification"},
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/readyz", "port": 8787},
                                "periodSeconds": 2,
                            },
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"memory": "512Mi"},
                            },
                        },
                        {
                            "name": "keycloak",
                            "image": KEYCLOAK_IMAGE,
                            "args": [
                                "start-dev",
                                "--import-realm",
                                f"--http-port={idp_port}",
                                f"--hostname=http://127.0.0.1:{idp_port}",
                                "--health-enabled=true",
                            ],
                            "env": [
                                {"name": "KC_BOOTSTRAP_ADMIN_USERNAME", "value": "fixture-admin"},
                                {
                                    "name": "KC_BOOTSTRAP_ADMIN_PASSWORD",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "qualification-idp",
                                            "key": "admin",
                                        }
                                    },
                                },
                            ],
                            "ports": [{"name": "idp", "containerPort": idp_port}],
                            "volumeMounts": [
                                {
                                    "name": "realm",
                                    "mountPath": "/opt/keycloak/data/import",
                                    "readOnly": True,
                                }
                            ],
                            "startupProbe": {
                                "httpGet": {"path": "/health/ready", "port": 9000},
                                "periodSeconds": 2,
                                "failureThreshold": 120,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health/ready", "port": 9000},
                                "periodSeconds": 2,
                            },
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "512Mi"},
                                "limits": {"memory": "1Gi"},
                            },
                        },
                    ],
                },
            },
        },
    }


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.current = [], None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""), "inputs": {}}
            self.forms.append(self.current)
        elif tag in {"input", "button"} and self.current is not None and attrs.get("name"):
            if "disabled" in attrs or (
                attrs.get("type") in {"checkbox", "radio"} and "checked" not in attrs
            ):
                return
            self.current["inputs"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def approve_device(verification: str, code: str, password: str) -> None:
    origin = urlsplit(verification)
    if origin.hostname != "127.0.0.1" or origin.scheme != "http":
        raise RuntimeError("Device emulation may only use its explicit loopback issuer")

    def local_request(request):
        if (request.url.scheme, request.url.host, request.url.port) != (
            origin.scheme,
            origin.hostname,
            origin.port,
        ):
            raise RuntimeError("Emulated browser redirect changed origin")

    def loopback_cookies(response):
        local_request(response.request)
        # Browsers treat loopback as a secure context. Python's cookie jar does
        # not send Keycloak's Secure cookies on HTTP loopback, so emulate that
        # browser behavior only for this explicitly bounded synthetic issuer.
        for cookie in browser.cookies.jar:
            if cookie.domain.lstrip(".") == "127.0.0.1":
                cookie.secure = False

    with httpx.Client(
        timeout=15,
        follow_redirects=True,
        trust_env=False,
        event_hooks={"request": [local_request], "response": [loopback_cookies]},
    ) as browser:
        # Use Keycloak's documented verification_uri_complete equivalent to
        # submit the code displayed by the real native CLI.
        response = browser.get(verification, params={"user_code": code})
        for _ in range(6):
            response.raise_for_status()
            forms = Forms()
            forms.feed(response.text)
            chosen = next(
                (
                    form
                    for form in forms.forms
                    if any(
                        name in form["inputs"]
                        for name in ("device_user_code", "username", "accept")
                    )
                ),
                None,
            )
            if chosen is None:
                if "Device Login Successful" not in response.text:
                    raise RuntimeError("Local browser did not confirm device approval")
                return
            target = urljoin(str(response.url), chosen["action"])
            if urlsplit(target)[:2] != origin[:2]:
                raise RuntimeError("Emulated login form changed origin")
            values = chosen["inputs"]
            values.pop("cancel", None)
            if "device_user_code" in values:
                values["device_user_code"] = code
            if "username" in values:
                values.update(username="alice", password=password)
            response = browser.post(target, data=values)
        raise RuntimeError("Emulated device approval exceeded its form budget")


def check_status(response: httpx.Response, expected: int) -> dict | list | None:
    if response.status_code != expected:
        raise RuntimeError(
            f"Local qualification expected HTTP {expected}, received {response.status_code}"
        )
    return response.json() if response.content else None


def device_cli(api: str, issuer: str, password: str, directory: Path) -> tuple[str, dict]:
    target = directory / "device.token"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "memory_worlds.cli",
            "--url",
            api,
            "login",
            "--issuer",
            issuer,
            "--client-id",
            "mw-native",
            "--scope",
            "openid",
            "--scope",
            "memory-worlds",
            "--token-output",
            str(target),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            if not selector.select(timeout=30):
                raise RuntimeError("Local CLI device challenge did not arrive")
            line = process.stderr.readline()
        match = re.fullmatch(
            r"Open (http://127\.0\.0\.1:\d+/\S+) and enter code ([A-Za-z0-9 -]+)\n", line
        )
        if match is None:
            raise RuntimeError("Local CLI device challenge had an unexpected shape")
        approve_device(match[1], match[2], password)
        stdout, stderr = process.communicate(timeout=40)
        if process.returncode:
            for safe_error in (
                "Memory service did not validate the access token",
                "Identity provider rejected the device token request",
                "Device authorization was denied",
                "Device authorization expired; start login again",
            ):
                if safe_error in stderr:
                    raise RuntimeError(f"Local CLI device flow: {safe_error}")
            raise RuntimeError("Native CLI device flow failed against the local IdP")
        identity = json.loads(stdout)
        if target.stat().st_mode & 0o077:
            raise RuntimeError("Device token was not stored privately")
        return target.read_text().strip(), identity
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def exercise(
    api: str, issuer: str, password: str, admin_password: str, bootstrap: dict, directory: Path
) -> dict:
    with httpx.Client(timeout=15, trust_env=False) as http:
        discovery = check_status(http.get(f"{issuer}/.well-known/openid-configuration"), 200)
        if discovery["issuer"] != issuer:
            raise RuntimeError("Container IdP discovery issuer mismatch")
        jwks = check_status(http.get(discovery["jwks_uri"]), 200)
        if not jwks.get("keys"):
            raise RuntimeError("Container IdP returned no JWKS")

        def token(user="alice", client="mw-native", scopes="openid memory-worlds"):
            return check_status(
                http.post(
                    discovery["token_endpoint"],
                    data={
                        "grant_type": "password",
                        "client_id": client,
                        "username": user,
                        "password": password,
                        "scope": scopes,
                    },
                ),
                200,
            )["access_token"]

        def call(path, credential, *, method="GET", body=None, expected=200):
            return check_status(
                http.request(
                    method, api + path, json=body, headers={"Authorization": f"Bearer {credential}"}
                ),
                expected,
            )

        # Separate token verification from browser emulation failures. Password
        # grant exists only in this generated local realm, never the Okta guide.
        call("/v1/me", token())
        original, login_receipt = device_cli(api, issuer, password, directory)
        who = call("/v1/me", original)
        if who["admin"] or who["user_id"] != login_receipt["user_id"]:
            raise RuntimeError("External identity acquired operator authority")
        worlds = call("/v1/worlds", original)
        team = bootstrap["worlds"]["data"]
        company = bootstrap["worlds"]["company"]
        if not {team["id"], company["id"]}.issubset({world["id"] for world in worlds}):
            raise RuntimeError("Signed local groups did not map to shared worlds")
        private = next(world for world in worlds if world["kind"] == "private-work")
        bob = token("bob")
        call(f"/v1/worlds/{private['id']}/scopes", bob, expected=403)
        call(
            f"/v1/worlds/{bootstrap['operator_world']}/scopes/{bootstrap['operator_scope']}",
            original,
            expected=403,
        )
        for bad in (
            token(client="mw-untrusted-client"),
            token(client="mw-wrong-audience"),
            token(scopes="openid"),
        ):
            call("/v1/me", bad, expected=401)
        delegated = call(
            "/v1/credentials/delegate",
            original,
            method="POST",
            expected=201,
            body={
                "private_world_id": private["id"],
                "grants": [
                    {"world_id": team["id"], "scope_id": team["scope_id"], "operations": ["read"]}
                ],
            },
        )
        call(f"/v1/worlds/{team['id']}/scopes/{team['scope_id']}", delegated["token"])
        host = f"http://127.0.0.1:{urlsplit(issuer).port}"
        admin = check_status(
            http.post(
                f"{host}/realms/master/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": "fixture-admin",
                    "password": admin_password,
                },
            ),
            200,
        )["access_token"]
        headers = {"Authorization": f"Bearer {admin}"}
        users = check_status(
            http.get(
                f"{host}/admin/realms/memory-worlds/users",
                params={"username": "alice", "exact": "true"},
                headers=headers,
            ),
            200,
        )
        groups = check_status(
            http.get(f"{host}/admin/realms/memory-worlds/groups", headers=headers), 200
        )
        group = next(item for item in groups if item["name"] == "mw-data-team")
        check_status(
            http.delete(
                f"{host}/admin/realms/memory-worlds/users/{users[0]['id']}/groups/{group['id']}",
                headers=headers,
            ),
            204,
        )
        # JWT iat is second-granular. Avoid an equal-iat conflicting group state.
        time.sleep(1.1)
        newer = token()
        updated = call("/v1/worlds", newer)
        if team["id"] in {world["id"] for world in updated}:
            raise RuntimeError("Group removal did not remove live shared membership")
        if private["id"] not in {world["id"] for world in updated}:
            raise RuntimeError("Group changes replaced the stable private identity")
        call("/v1/me", original, expected=401)
        call(f"/v1/worlds/{team['id']}/scopes/{team['scope_id']}", delegated["token"], expected=403)
        call(
            f"/v1/credentials/{who['credential_id']}",
            bootstrap["operator_token"],
            method="DELETE",
            expected=204,
        )
        call("/v1/me", delegated["token"], expected=401)
        call(
            f"/v1/users/{who['user_id']}",
            bootstrap["operator_token"],
            method="PATCH",
            body={"enabled": False},
            expected=204,
        )
        call("/v1/me", newer, expected=401)
    return {
        "discovery": True,
        "jwks": True,
        "native_cli_device_flow": True,
        "group_mapping": True,
        "private_world_isolation": True,
        "private_scope_isolation": True,
        "client_audience_scope_rejection": True,
        "new_token_group_removal": True,
        "old_group_replay_rejection": True,
        "delegated_membership_attenuation": True,
        "service_revocation_descendants": True,
        "service_user_disable": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--api-image", required=True)
    parser.add_argument("--idp-port", type=int, default=18080)
    parser.add_argument("--api-port", type=int, default=18787)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.context.startswith("kind-memory-worlds-"):
        parser.error("Only an explicitly selected isolated Memory Worlds kind context is allowed")
    if (
        not 1024 <= args.idp_port <= 65535
        or not 1024 <= args.api_port <= 65535
        or args.idp_port == args.api_port
    ):
        parser.error("Choose distinct unprivileged loopback ports")
    if args.output.exists():
        parser.error("Output already exists")
    kube = [
        os.environ.get("KUBECTL", "kubectl"),
        "--kubeconfig",
        str(args.kubeconfig),
        "--context",
        args.context,
    ]
    namespace = "memory-worlds-oidc-" + uuid4().hex[:10]
    issuer = f"http://127.0.0.1:{args.idp_port}/realms/memory-worlds"
    api = f"http://127.0.0.1:{args.api_port}"
    created = False
    with tempfile.TemporaryDirectory(prefix="memory-worlds-local-oidc-") as temporary:
        directory = Path(temporary)
        password, admin_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)

        def run(*command, body=None):
            result = subprocess.run(
                [*kube, *command],
                input=None if body is None else json.dumps(body),
                text=True,
                capture_output=True,
                timeout=300,
            )
            if result.returncode:
                raise RuntimeError(f"Isolated kubectl {command[0]} failed")
            return result.stdout

        try:
            run("create", "namespace", namespace)
            created = True
            for resource in (
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "qualification-runtime", "namespace": namespace},
                    "data": {"runtime.py": (ROOT / "deploy/qualification/runtime.py").read_text()},
                },
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "qualification-idp", "namespace": namespace},
                    "stringData": {
                        "realm.json": json.dumps(realm_document(password)),
                        "admin": admin_password,
                    },
                },
                deployment_document(namespace, args.api_image, issuer),
            ):
                run("apply", "-f", "-", body=resource)
            run(
                "-n",
                namespace,
                "rollout",
                "status",
                "deployment/oidc-qualification",
                "--timeout=240s",
            )
            pod = json.loads(
                run("-n", namespace, "get", "pods", "-l", "app=oidc-qualification", "-o", "json")
            )["items"][0]
            image_ids = {
                item["name"]: item["imageID"] for item in pod["status"]["containerStatuses"]
            }
            bootstrap = json.loads(
                run(
                    "-n",
                    namespace,
                    "exec",
                    "deployment/oidc-qualification",
                    "-c",
                    "api",
                    "--",
                    "cat",
                    "/data/qualification.json",
                )
            )
            with (directory / "forward.log").open("w") as log:
                forward = subprocess.Popen(
                    [
                        *kube,
                        "-n",
                        namespace,
                        "port-forward",
                        "--address=127.0.0.1",
                        "deployment/oidc-qualification",
                        f"{args.api_port}:8787",
                        f"{args.idp_port}:{args.idp_port}",
                    ],
                    stdout=log,
                    stderr=log,
                )
                try:
                    deadline = time.monotonic() + 30
                    with httpx.Client(timeout=2, trust_env=False) as http:
                        while True:
                            try:
                                if http.get(api + "/readyz").status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            if forward.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError("Isolated port forwarding failed")
                            time.sleep(0.1)
                    checks = exercise(api, issuer, password, admin_password, bootstrap, directory)
                finally:
                    forward.terminate()
                    forward.wait(timeout=10)
            report = {
                "kind": "kind-keycloak-oidc-emulation",
                "passed": True,
                "api_image": args.api_image,
                "container_image_ids": image_ids,
                "keycloak_image": KEYCLOAK_IMAGE,
                "checks": checks,
                "fixture_sha256": hashlib.sha256(
                    (ROOT / "deploy/qualification/realm.json").read_bytes()
                ).hexdigest(),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "runtime_sha256": hashlib.sha256(
                    (ROOT / "deploy/qualification/runtime.py").read_bytes()
                ).hexdigest(),
                "external_identity_provider": "assumed; no live Okta qualification performed",
            }
            descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(report, output, indent=2)
                output.write("\n")
            print(json.dumps(report))
        finally:
            if created:
                with contextlib.suppress(Exception):
                    run("delete", "namespace", namespace, "--wait=true", "--timeout=90s")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Local OIDC qualification failed; inspect the isolated emulator configuration.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
