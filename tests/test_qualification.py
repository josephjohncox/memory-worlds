"""Qualification contracts use synthetic evidence and reject unsafe local routing."""

import importlib.util
import json
import subprocess
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def script(name):
    specification = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.fixture
def oidc():
    return script("qualify-oidc")


@pytest.fixture
def graph():
    return script("qualify-graph")


def test_keycloak_fixture_has_real_claim_mappers_and_no_saved_credentials(oidc):
    fixture = oidc.realm_document("runtime-only-synthetic-password")
    assert len(fixture["users"]) == 2
    assert fixture["users"][0]["groups"] == ["/mw-company", "/mw-data-team"]
    assert fixture["users"][1]["groups"] == ["/mw-company"]
    clients = {client["clientId"]: client for client in fixture["clients"]}
    assert set(clients) == {"mw-native", "mw-untrusted-client", "mw-wrong-audience"}
    for client in clients.values():
        assert client["publicClient"] and client["directAccessGrantsEnabled"]
        assert client["attributes"]["oauth2.device.authorization.grant.enabled"] == "true"
        assert client["optionalClientScopes"] == ["memory-worlds"]
        assert any(
            mapper["protocolMapper"] == "oidc-group-membership-mapper"
            for mapper in client["protocolMappers"]
        )
        assert any(
            mapper["protocolMapper"] == "oidc-sub-mapper" for mapper in client["protocolMappers"]
        )
    template = json.loads((ROOT / "deploy/qualification/realm.json").read_text())
    assert template["users"] == [] and template["clients"] == []
    assert "runtime-only-synthetic-password" not in json.dumps(template)


def test_local_idp_pod_keeps_issuer_on_shared_loopback_and_pins_image(oidc):
    deployment = oidc.deployment_document(
        "memory-worlds-test", "synthetic-api:current", "http://127.0.0.1:18080/realms/memory-worlds"
    )
    pod = deployment["spec"]["template"]["spec"]
    assert not pod["automountServiceAccountToken"]
    containers = {container["name"]: container for container in pod["containers"]}
    assert containers["keycloak"]["image"].startswith("quay.io/keycloak/keycloak:26.8.1@sha256:")
    assert "--http-port=18080" in containers["keycloak"]["args"]
    assert "--hostname=http://127.0.0.1:18080" in containers["keycloak"]["args"]
    assert containers["api"]["env"] == [
        {"name": "QUALIFICATION_ISSUER", "value": "http://127.0.0.1:18080/realms/memory-worlds"}
    ]
    admin = next(
        item
        for item in containers["keycloak"]["env"]
        if item["name"] == "KC_BOOTSTRAP_ADMIN_PASSWORD"
    )
    assert "valueFrom" in admin and "value" not in admin
    assert pod["initContainers"][0]["command"][-1] == "bootstrap"


def test_html_forms_preserve_action_hidden_state_and_select_only_named_inputs(oidc):
    forms = oidc.Forms()
    forms.feed(
        '<form action="/login?state=synthetic&amp;step=2">'
        '<input type="hidden" name="code" value="fixture">'
        '<input name="username"><input name="password" type="password">'
        '<input type="submit" value="Login"></form>'
    )
    assert forms.forms == [
        {
            "action": "/login?state=synthetic&step=2",
            "inputs": {"code": "fixture", "username": "", "password": ""},
        }
    ]


@pytest.mark.parametrize(
    "address",
    ["https://example.invalid/device", "http://localhost/device", "http://192.0.2.10/device"],
)
def test_device_approval_cannot_be_pointed_at_an_external_provider(oidc, address):
    with pytest.raises(RuntimeError, match="loopback"):
        oidc.approve_device(address, "SYNTHETIC", "runtime-only-password")


def test_local_qualification_errors_do_not_echo_tokens_or_response_bodies(oidc):
    response = httpx.Response(401, json={"private": "do not echo this fixture"})
    with pytest.raises(RuntimeError) as error:
        oidc.check_status(response, 200)
    assert "do not echo" not in str(error.value)


def test_device_emulator_preserves_loopback_secure_cookies_across_redirects(oidc, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.url.params["user_code"] == "SYNTHETIC"
            return httpx.Response(
                302,
                headers={
                    "Location": "/login",
                    "Set-Cookie": "KC_RESTART=synthetic-cookie; Path=/; Secure; HttpOnly",
                },
            )
        if len(requests) == 2:
            assert request.headers["cookie"] == "KC_RESTART=synthetic-cookie"
            return httpx.Response(
                200,
                text=(
                    '<form action="/login"><input name="username"><input name="password"></form>'
                ),
            )
        if len(requests) == 3:
            assert request.content == b"username=alice&password=runtime-only-password"
            return httpx.Response(
                200,
                text=(
                    '<form action="/consent"><input name="code" value="fixture">'
                    '<button name="accept" value="yes">Yes</button>'
                    '<button name="cancel" value="no">No</button></form>'
                ),
            )
        assert request.content == b"code=fixture&accept=yes"
        return httpx.Response(200, text="Device Login Successful")

    client = httpx.Client
    monkeypatch.setattr(
        oidc.httpx,
        "Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    oidc.approve_device(
        "http://127.0.0.1:18080/realms/memory-worlds/device", "SYNTHETIC", "runtime-only-password"
    )
    assert len(requests) == 4


def test_device_emulator_stops_cross_origin_redirect_before_network(oidc, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/device"})

    client = httpx.Client
    monkeypatch.setattr(
        oidc.httpx,
        "Client",
        lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="redirect changed origin"):
        oidc.approve_device("http://127.0.0.1:18080/device", "SYNTHETIC", "unused-fixture")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "schema",
    [
        "ExtractedEntities",
        "ExtractedEdges",
        "NodeResolutions",
        "SummarizedEntities",
        "EdgeDuplicate",
        "EdgeTimestamps",
    ],
)
def test_local_graph_model_implements_actual_structured_response_schemas(graph, schema):
    response = graph.model_response(schema, "QFIX-aster-cobalt: Aster uses the CobaltQueue.")
    assert isinstance(response, dict) and response
    if schema == "ExtractedEntities":
        assert {item["name"] for item in response["extracted_entities"]} == {"Aster", "CobaltQueue"}
    elif schema == "ExtractedEdges":
        assert response["edges"][0]["fact"] == "Aster uses the CobaltQueue."


def test_local_graph_model_rejects_unknown_or_nonfixture_requests(graph):
    with pytest.raises(ValueError, match="fixture marker"):
        graph.model_response("ExtractedEntities", "Unrecognized input")
    with pytest.raises(ValueError, match="Unknown"):
        graph.model_response("InventedSchema", "QFIX-aster-cobalt")


def test_graph_evaluator_requires_correct_scope_fact_and_source_provenance(graph):
    fixture = graph.CORPUS[0]
    hit = {"scope_id": "own", "fact": fixture["fact"], "source_episode_ids": ["original"]}
    assert graph.score_retrieval(fixture, [hit], "own", "original")["passed"]
    assert not graph.score_retrieval(fixture, [], "own", "original")["passed"]
    for changed in (
        {**hit, "scope_id": "foreign"},
        {**hit, "source_episode_ids": []},
        {**hit, "fact": "Aster uses the JadeQueue."},
        {**hit, "fact": fixture["fact"] + " Also JadeQueue."},
    ):
        assert not graph.score_retrieval(fixture, [changed], "own", "original")["passed"]


def test_sharing_corpus_and_scoring_detect_unsafe_and_attribution_losing_outputs():
    source = """
import assert from 'node:assert/strict';
import {SHARING_CASES,validateCases} from './scripts/qualify-sharing-cases.mjs';
import {scoreSharing,summarize} from './scripts/qualify-sharing.mjs';
const cases=validateCases(SHARING_CASES);
assert(cases.length>=30);
for(const topic of ['hr','sentiment','credential','explicit-share','technical-finding',
  'team-discussion','team-preference','sentiment-multilingual','injection'])
  assert(cases.some(item=>item.topic===topic));
const privateCase=cases.find(item=>item.id==='hr-review');
assert(scoreSharing(privateCase,[{visibility:'shareable',confidence:.99,
  category:'team-discussion',excerpt:'An unsafe selected text.'}]).falsePositive);
const unverified=cases.find(item=>item.id==='hypothesis-attribution');
assert(scoreSharing(unverified,[{visibility:'shareable',confidence:.99,
  category:unverified.category,excerpt:'batching may reduce connection overhead'}]).falsePositive);
assert(scoreSharing(unverified,[]).falseNegative);
assert(!summarize([{status:'classification_failed'}]).passed);
assert.equal(summarize([]).precision,null);
assert(!summarize([]).passed);
assert.throws(()=>validateCases([cases[0],cases[0]]));
assert.throws(()=>validateCases([{...cases[0],expected:'guessed'}]));
console.log(JSON.stringify({cases:cases.length,unsafeDetected:true,attributionLossDetected:true}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-"],
        input=source,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cases"] >= 30


def test_network_policy_fixture_selects_only_its_own_pods():
    network = script("qualify-network")
    fixture = network.pod("target", "synthetic-private-ns", "synthetic-api:current", server=True)
    assert fixture["metadata"]["labels"] == {"fixture": "target"}
    assert not fixture["spec"]["automountServiceAccountToken"]
    assert "hostNetwork" not in fixture["spec"]
    rule = network.policy("deny", "synthetic-private-ns", "target", "ingress", [])
    assert rule["spec"] == {
        "podSelector": {"matchLabels": {"fixture": "target"}},
        "policyTypes": ["Ingress"],
        "ingress": [],
    }


def test_application_qualification_uses_shared_sql_secret_and_bounded_local_egress():
    deployment = script("qualify-deployment")
    values = deployment.chart_values("synthetic-api:current", "synthetic-private-ns", "fixed-id")
    assert values["replicaCount"] == 2
    assert values["storage"]["backend"] == "postgres"
    assert values["storage"]["postgres"]["existingSecret"] == "postgres"
    assert "database-url" not in json.dumps(values)
    assert values["backup"]["cronJob"]["suspend"]
    assert values["backup"]["cronJob"]["graphMode"] == "native"
    assert values["networkPolicy"]["postgresEgress"][0]["ports"] == [
        {"protocol": "TCP", "port": 5432}
    ]
    assert "allowExternalHTTPS" not in values["networkPolicy"]
    helper = deployment.helper_pod("synthetic-private-ns", "synthetic-api:current", "fixed-id")
    # Maintenance must never accidentally become a Deployment replica.
    assert "app.kubernetes.io/instance" not in helper["metadata"]["labels"]
    assert not helper["spec"]["automountServiceAccountToken"]
    assert helper["spec"]["volumes"][1]["secret"]["defaultMode"] == 0o440
    postgres = deployment.postgres_deployment("synthetic-private-ns")
    assert postgres["spec"]["strategy"]["type"] == "Recreate"
    assert "@sha256:" in postgres["spec"]["template"]["spec"]["containers"][0]["image"]


@pytest.mark.parametrize("script_name", ["qualify-oidc.py", "qualify-network.py"])
def test_cluster_qualification_refuses_unrelated_context_before_any_kubectl(script_name, tmp_path):
    import sys

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / script_name),
            "--context",
            "kind-unrelated",
            "--kubeconfig",
            str(tmp_path / "absent"),
            "--api-image",
            "synthetic:current",
            "--output",
            str(tmp_path / "receipt.json"),
        ],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert "isolated Memory Worlds kind context" in result.stderr
    assert not (tmp_path / "receipt.json").exists()


@pytest.mark.parametrize(
    "script_name",
    [
        "qualify-cluster.py",
        "qualify-oidc.py",
        "qualify-graph.py",
        "qualify-network.py",
        "qualify-deployment.py",
    ],
)
def test_qualification_scripts_have_help_without_contacting_services(script_name):
    import sys

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0 and "usage:" in result.stdout
