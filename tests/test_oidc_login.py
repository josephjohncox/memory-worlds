"""Exercise current OAuth device login boundaries using HTTP request/response transport."""

import json
import stat
from urllib.parse import parse_qs

import httpx
import pytest

from memory_worlds import login
from memory_worlds.errors import MemoryError

ISSUER = "https://identity.example.test/oauth2/memory"
SERVICE = "https://memory.example.test"
ACCESS = "synthetic-api-access-token"


@pytest.fixture
def provider(monkeypatch, tmp_path):
    state = {
        "metadata": {
            "issuer": ISSUER,
            "device_authorization_endpoint": ISSUER + "/v1/device/authorize",
            "token_endpoint": ISSUER + "/v1/token",
        },
        "challenge": {
            "device_code": "private-device-code",
            "user_code": "ABCD-1234",
            "verification_uri": "https://identity.example.test/activate",
            "expires_in": 60,
            "interval": 1,
        },
        "tokens": [
            (
                200,
                {
                    "access_token": ACCESS,
                    "token_type": "Bearer",
                    "expires_in": 300,
                    "id_token": "discarded-id-token",
                    "refresh_token": "discarded-refresh-token",
                },
            )
        ],
        "identity": {"authority_id": "authority", "user_id": "alice", "credential_id": "session"},
        "identity_status": 200,
        "requests": [],
        "sleeps": [],
        "time": 0.0,
        "output": tmp_path / "identity.token",
    }

    def handle(request):
        state["requests"].append(request)
        assert not state["output"].exists(), "access-token file precedes API validation"
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=state["metadata"])
        if request.url.path.endswith("/device/authorize"):
            assert parse_qs(request.content.decode()) == {
                "client_id": ["native-client"],
                "scope": ["openid memory-worlds"],
            }
            return httpx.Response(200, json=state["challenge"])
        if request.url.path.endswith("/token"):
            assert parse_qs(request.content.decode()) == {
                "client_id": ["native-client"],
                "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
                "device_code": ["private-device-code"],
            }
            item = state["tokens"].pop(0)
            if isinstance(item, Exception):
                raise item
            return httpx.Response(item[0], json=item[1])
        assert str(request.url) == SERVICE + "/v1/me"
        assert request.headers["Authorization"] == "Bearer " + ACCESS
        return httpx.Response(state["identity_status"], json=state["identity"])

    original = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["time"] += seconds

    monkeypatch.setattr(login.httpx, "Client", client)
    monkeypatch.setattr(login.time, "monotonic", lambda: state["time"])
    monkeypatch.setattr(login.time, "sleep", sleep)
    return state


def invoke(provider, **changes):
    args = {"issuer": ISSUER, "client_id": "native-client", "token_output": provider["output"]}
    args.update(changes)
    return login.device_login(SERVICE, **args)


def test_device_login_validates_api_before_private_file_and_never_emits_tokens(provider, capsys):
    result = invoke(provider)
    assert result == {
        **provider["identity"],
        "token_file": str(provider["output"]),
        "expires_in": 300,
    }
    assert provider["output"].read_text() == ACCESS + "\n"
    assert stat.S_IMODE(provider["output"].stat().st_mode) == 0o600
    output = capsys.readouterr()
    assert not output.out
    assert "https://identity.example.test/activate" in output.err and "ABCD-1234" in output.err
    assert all(
        secret not in output.err + json.dumps(result)
        for secret in (
            ACCESS,
            "private-device-code",
            "discarded-id-token",
            "discarded-refresh-token",
        )
    )


def test_pending_slowdown_and_connection_timeout_follow_polling_rules(provider):
    provider["tokens"][:0] = [
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        httpx.ConnectTimeout("synthetic timeout"),
    ]
    invoke(provider)
    assert provider["sleeps"] == [1, 1, 6, 12]


@pytest.mark.parametrize(
    "error", ["access_denied", "expired_token", "invalid_client", "unexpected"]
)
def test_token_errors_stop_without_secret_file(provider, error):
    provider["tokens"] = [(400, {"error": error, "error_description": ACCESS})]
    with pytest.raises(MemoryError) as caught:
        invoke(provider)
    assert ACCESS not in str(caught.value)
    assert not provider["output"].exists()


def test_lifetime_expires_without_additional_polling(provider):
    provider["challenge"]["expires_in"] = 1
    with pytest.raises(MemoryError, match="expired"):
        invoke(provider)
    assert not any(request.url.path.endswith("/token") for request in provider["requests"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://other.example.test"),
        ("token_endpoint", "https://other.example.test/token"),
        ("device_authorization_endpoint", "http://identity.example.test/device"),
        ("token_endpoint", "https://identity.example.test/token?secret=1"),
    ],
)
def test_discovery_cannot_select_other_issuers_or_credential_destinations(provider, field, value):
    provider["metadata"][field] = value
    with pytest.raises((MemoryError, ValueError)):
        invoke(provider)
    assert len(provider["requests"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("verification_uri", "https://phishing.example.test/activate"),
        ("user_code", "code\x1b[31m"),
        ("device_code", "secret\nheader"),
        ("expires_in", True),
        ("expires_in", 1801),
        ("interval", 0),
    ],
)
def test_invalid_device_challenge_does_not_poll(provider, field, value):
    provider["challenge"][field] = value
    with pytest.raises((MemoryError, ValueError)):
        invoke(provider)
    assert len(provider["requests"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_type", "Basic"),
        ("access_token", "secret\r\nheader"),
        ("expires_in", -1),
    ],
)
def test_invalid_token_response_is_not_sent_to_service(provider, field, value):
    provider["tokens"][0][1][field] = value
    with pytest.raises(MemoryError):
        invoke(provider)
    assert len(provider["requests"]) == 3
    assert not provider["output"].exists()


@pytest.mark.parametrize("status", [401, 403, 302])
def test_service_must_validate_before_any_token_file_is_written(provider, status):
    provider["identity_status"] = status
    with pytest.raises(MemoryError):
        invoke(provider)
    assert not provider["output"].exists()


def test_login_does_not_overwrite_existing_files_or_request_refresh_credentials(provider):
    provider["output"].write_text("keep existing")
    with pytest.raises(MemoryError, match="already exists"):
        invoke(provider)
    assert not provider["requests"]
    assert provider["output"].read_text() == "keep existing"
    provider["output"].unlink()
    with pytest.raises(MemoryError, match="offline_access"):
        invoke(provider, scopes=("openid", "offline_access"))
    assert not provider["requests"]


def test_login_rejects_remote_plaintext_and_invalid_scopes_before_network(provider):
    with pytest.raises(ValueError):
        login.device_login(
            "http://remote.example.test",
            issuer=ISSUER,
            client_id="native-client",
            token_output=provider["output"],
        )
    with pytest.raises(MemoryError, match="scope"):
        invoke(provider, scopes=("invalid scope",))
    assert not provider["requests"]


def test_login_identity_document_is_bounded_and_is_an_object(provider, monkeypatch):
    original = httpx.Client

    # Reuse the original class through the fixture's factory, replacing only its transport.
    def oversized(**kwargs):
        result = original(**kwargs)
        result._transport = httpx.MockTransport(
            lambda _: httpx.Response(200, content=b" " * 1_048_577)
        )
        return result

    monkeypatch.setattr(login.httpx, "Client", oversized)
    with pytest.raises(MemoryError, match="one MiB"):
        invoke(provider)
    assert not provider["output"].exists()


def test_actual_cli_device_flow_with_signed_oidc_and_loopback_daemon(tmp_path):
    import os
    import socket
    import subprocess
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from memory_worlds.store import MemoryStore

    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(signing.public_key()))
    jwk.update(kid="device-flow", alg="RS256", use="sig")
    state = {"token": None, "token_requests": 0}

    class Provider(BaseHTTPRequestHandler):
        def reply(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            if self.path.endswith("/.well-known/openid-configuration"):
                self.reply(
                    {
                        "issuer": issuer,
                        "device_authorization_endpoint": issuer + "/device",
                        "token_endpoint": issuer + "/token",
                    }
                )
            else:
                assert self.path == "/keys"
                self.reply({"keys": [jwk]})

        def do_POST(self):
            fields = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            assert fields["client_id"] == ["native-client"]
            if self.path == "/device":
                assert fields["scope"] == ["openid memory-worlds"]
                self.reply(
                    {
                        "device_code": "only-fixture-code",
                        "user_code": "TEST-1234",
                        "verification_uri": issuer + "/activate",
                        "expires_in": 60,
                        "interval": 1,
                    }
                )
            else:
                assert self.path == "/token" and fields["device_code"] == ["only-fixture-code"]
                state["token_requests"] += 1
                self.reply(
                    {"token_type": "Bearer", "access_token": state["token"], "expires_in": 300}
                )

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    issuer = f"http://127.0.0.1:{server.server_port}"
    state["token"] = jwt.encode(
        {
            "iss": issuer,
            "aud": "api://memory-worlds",
            "sub": "00u-fixed-user",
            "cid": "native-client",
            "scp": ["openid", "memory-worlds"],
            "exp": time.time() + 300,
            "iat": time.time(),
        },
        signing,
        algorithm="RS256",
        headers={"kid": "device-flow"},
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data_dir = tmp_path / "authority"
    MemoryStore(data_dir).bootstrap("operator")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MEMORY_WORLDS_")
    }
    organization = tmp_path / "organization.toml"
    organization.write_text(
        "[identity]\n"
        f'issuer = "{issuer}"\n'
        'audience = "api://memory-worlds"\n'
        f'jwks_url = "{issuer}/keys"\n'
        'client_ids = ["native-client"]\n'
        'required_scopes = ["memory-worlds"]\n'
    )
    environment.update(
        {
            "MEMORY_WORLDS_GRAPH_BACKEND": "disabled",
            "MEMORY_WORLDS_ORGANIZATION_FILE": str(organization),
        }
    )
    command = [sys.executable, "-m", "memory_worlds.cli", "--data-dir", str(data_dir), "--url", url]
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        command + ["serve", "--port", str(port)],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        with httpx.Client(timeout=0.2, trust_env=False) as client:
            while time.monotonic() < deadline:
                assert process.poll() is None, "local daemon exited before login"
                try:
                    if client.get(url + "/readyz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("local daemon did not become ready")
        target = tmp_path / "user.token"
        completed = subprocess.run(
            command
            + [
                "login",
                "--issuer",
                issuer,
                "--client-id",
                "native-client",
                "--token-output",
                str(target),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert completed.returncode == 0, completed.stderr
        receipt = json.loads(completed.stdout)
        assert state["token_requests"] == 1 and receipt["user_id"].startswith("user_")
        assert target.read_text().strip() == state["token"]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert state["token"] not in completed.stdout + completed.stderr
        worlds = subprocess.run(
            command + ["--token-file", str(target), "worlds"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert worlds.returncode == 0, worlds.stderr
        selected = json.loads(worlds.stdout)
        assert len(selected) == 1 and selected[0]["kind"] == "private-work"
        assert selected[0]["owner_id"] == receipt["user_id"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
