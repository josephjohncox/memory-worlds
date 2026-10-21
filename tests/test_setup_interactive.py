"""Ordinary-user setup never gains corporate privileges or starts local services."""

import hashlib
import json
import stat
import subprocess
import sys
import warnings
from getpass import GetPassWarning
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from memory_worlds import setup_interactive as setup
from memory_worlds.errors import MemoryError
from memory_worlds.setup_config import (
    CorporateSettings,
    ProviderSettings,
    SetupConfig,
    load_config,
    save_config,
)

AUTHORITY = "d03648fa-6157-449b-9e59-9a4dca002163"
SERVICE = "https://memory.example.test"
ISSUER = "https://identity.example.test/oauth2/memory"
FIRST_TOKEN = "synthetic-ordinary-user-access"
NEXT_TOKEN = "synthetic-rotated-user-access"


def private(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value)
    path.chmod(0o600)
    return path


def world(world_id, kind="team", **changes):
    return {
        "id": world_id,
        "authority_id": AUTHORITY,
        "name": "Synthetic " + world_id,
        "kind": kind,
        "owner_id": "alice",
        "created_at": "2026-10-21T00:00:00Z",
        "scope_id": str(UUID(int=1 if world_id == "team" else 2)),
        **changes,
    }


@pytest.fixture
def corporate(monkeypatch, tmp_path):
    provider = ProviderSettings(
        provider="openai-compatible",
        model="explicit-synthetic-model",
        base_url="http://127.0.0.1:9000/v1",
    )
    source = private(tmp_path / "source.token", FIRST_TOKEN + "\n")
    config = SetupConfig(
        instance="test",
        mode="hybrid",
        owner="alice",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        extraction=provider,
        embedding=provider,
        corporate=CorporateSettings(url=SERVICE, token_file=source, world_ids=["team"]),
    )
    state = {
        "config": config,
        "source": source,
        "identity": {
            "authority_id": AUTHORITY,
            "user_id": "alice",
            "credential_id": "ordinary-session",
            "admin": False,
        },
        "worlds": [world("team"), world("company", "company"), world("private", "private-work")],
        "requests": [],
        "status": 200,
        "response": None,
    }
    original = httpx.Client

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET", "setup must not mutate the corporate service"
        assert request.headers["authorization"] == "Bearer " + source.read_text().strip()
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"private": "never echo this body"})
        if state["response"] is not None:
            return httpx.Response(200, content=state["response"])
        if request.url.path == "/v1/me":
            return httpx.Response(200, json=state["identity"])
        assert request.url.path == "/v1/worlds"
        return httpx.Response(200, json=state["worlds"])

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 10
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(setup.httpx, "Client", client)
    return state


def connection_bytes(config):
    return config.path("corporate.token").read_bytes(), config.path("corporate.json").read_bytes()


def test_connect_uses_user_read_apis_and_keeps_token_out_of_binding(corporate, capsys):
    config = corporate["config"]
    result = setup.connect_setup(config)
    token, binding = connection_bytes(config)
    assert [request.url.path for request in corporate["requests"]] == ["/v1/me", "/v1/worlds"]
    assert result == json.loads(binding)
    assert result["world_ids"] == ["team"]
    assert result["token_file"] == str(config.path("corporate.token"))
    assert result["token_sha256"] == hashlib.sha256(token).hexdigest()
    assert token == (FIRST_TOKEN + "\n").encode()
    assert FIRST_TOKEN not in binding.decode()
    assert stat.S_IMODE(config.path("corporate.token").stat().st_mode) == 0o600
    assert stat.S_IMODE(config.path("corporate.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(config.state_dir.stat().st_mode) == 0o700
    assert not capsys.readouterr().out


@pytest.mark.parametrize("selected", [["private"], ["inaccessible"]])
def test_connect_rejects_private_or_inaccessible_worlds(corporate, selected):
    config = corporate["config"]
    config.corporate.world_ids = selected
    with pytest.raises(MemoryError, match="accessible shared"):
        setup.connect_setup(config)
    assert not config.path("corporate.token").exists()
    assert not config.path("corporate.json").exists()


def test_connect_rejects_operator_credentials(corporate):
    corporate["identity"]["admin"] = True
    with pytest.raises(MemoryError, match="ordinary corporate user"):
        setup.connect_setup(corporate["config"])
    assert len(corporate["requests"]) == 1


def test_corporate_discovery_authority_mismatch_is_not_saved(corporate):
    corporate["worlds"][0]["authority_id"] = "another-authority"
    with pytest.raises(MemoryError, match="connected authority"):
        setup.connect_setup(corporate["config"])
    assert not corporate["config"].path("corporate.token").exists()


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_corporate_errors_do_not_echo_response_bodies(corporate, status, capsys):
    corporate["status"] = status
    with pytest.raises(MemoryError, match=f"HTTP {status}") as failure:
        setup.connect_setup(corporate["config"])
    assert "never echo" not in str(failure.value)
    assert not capsys.readouterr().out
    assert len(corporate["requests"]) == 1


@pytest.mark.parametrize(
    "response", [b"[]", b"null", b"invalid-json", b"x" * (4 * 1024 * 1024 + 1)]
)
def test_corporate_response_is_bounded_and_typed(corporate, response):
    corporate["response"] = response
    with pytest.raises(MemoryError):
        setup.connect_setup(corporate["config"])
    assert not corporate["config"].path("corporate.token").exists()


def test_rotation_preserves_selection_and_replaces_credential_binding(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    private(corporate["source"], NEXT_TOKEN + "\n")
    corporate["identity"]["credential_id"] = "new-ordinary-session"
    corporate["worlds"].append(world("new-team"))
    receipt = setup.connect_setup(config)
    assert receipt["world_ids"] == ["team"]
    assert receipt["credential_id"] == "new-ordinary-session"
    assert config.path("corporate.token").read_text() == NEXT_TOKEN + "\n"
    assert not list(config.state_dir.glob("corporate-login-*.token"))


def test_explicit_configuration_can_change_selection_without_catalog_broadening(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    config.corporate.world_ids = ["company"]
    receipt = setup.connect_setup(config)
    assert receipt["world_ids"] == ["company"]


@pytest.mark.parametrize("changed", ["user_id", "authority_id"])
def test_identity_changes_preserve_previous_connection(corporate, changed):
    config = corporate["config"]
    setup.connect_setup(config)
    original = connection_bytes(config)
    corporate["identity"][changed] = "different-identity"
    if changed == "authority_id":
        for item in corporate["worlds"]:
            item["authority_id"] = corporate["identity"][changed]
    private(corporate["source"], NEXT_TOKEN + "\n")
    with pytest.raises(MemoryError, match="authority or user changed"):
        setup.connect_setup(config)
    assert connection_bytes(config) == original


def test_removed_membership_does_not_replace_previous_token(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    previous = connection_bytes(config)
    corporate["worlds"] = [world("company", "company")]
    private(corporate["source"], NEXT_TOKEN + "\n")
    with pytest.raises(MemoryError, match="accessible shared"):
        setup.connect_setup(config)
    assert connection_bytes(config) == previous


def test_token_or_url_tampering_fails_before_new_network_requests(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    count = len(corporate["requests"])
    private(config.path("corporate.token"), "not-the-bound-token\n")
    with pytest.raises(MemoryError, match="changed outside"):
        setup.connect_setup(config)
    assert len(corporate["requests"]) == count


def test_unknown_managed_token_is_not_overwritten(corporate):
    config = corporate["config"]
    private(config.path("corporate.token"), "unowned-token\n")
    with pytest.raises(MemoryError, match="unbound"):
        setup.connect_setup(config)
    assert config.path("corporate.token").read_text() == "unowned-token\n"
    assert not corporate["requests"]


def test_failed_binding_write_restores_previous_token(corporate, monkeypatch):
    config = corporate["config"]
    setup.connect_setup(config)
    previous = connection_bytes(config)
    private(corporate["source"], NEXT_TOKEN + "\n")
    original = setup.write_private

    def fail_binding(path, data, **kwargs):
        if path == config.path("corporate.json"):
            raise OSError("synthetic write interruption")
        return original(path, data, **kwargs)

    monkeypatch.setattr(setup, "write_private", fail_binding)
    with pytest.raises(OSError, match="synthetic write"):
        setup.connect_setup(config)
    assert connection_bytes(config) == previous


def test_device_login_stages_privately_and_cleans_up_after_failed_discovery(corporate, monkeypatch):
    config = corporate["config"]
    config.corporate.token_file = None
    config.corporate.issuer = ISSUER
    config.corporate.client_id = "native-public-client"
    stages = []

    def login(url, *, issuer, client_id, scopes, token_output):
        assert (url, issuer, client_id, scopes) == (
            SERVICE,
            ISSUER,
            "native-public-client",
            ("openid", "memory-worlds"),
        )
        assert not token_output.exists()
        assert token_output.parent == config.state_dir
        private(token_output, FIRST_TOKEN + "\n")
        stages.append(token_output)

    monkeypatch.setattr(setup, "device_login", login)
    corporate["status"] = 401
    with pytest.raises(MemoryError, match="HTTP 401"):
        setup.connect_setup(config)
    assert stages and all(not path.exists() for path in stages)
    assert not config.path("corporate.token").exists()


def answers(values):
    iterator = iter(values)
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        try:
            return next(iterator)
        except StopIteration as error:
            raise AssertionError("Unexpected wizard question: " + prompt) from error

    return respond, prompts


def local_answers(tmp_path, *, authentication="none"):
    return [
        "local",
        "personal",
        "alice",
        str(tmp_path / "data"),
        "new",
        str(tmp_path / "state"),
        "8787",
        "16379",
        "openai-compatible",
        "explicit-extraction",
        "http://127.0.0.1:9000/v1",
        authentication,
        "openai-compatible",
        "explicit-embedding",
        "http://127.0.0.1:9001/v1",
        "none",
        "1536",
        "yes",
        "no",
        "yes",
    ]


def test_local_wizard_creates_only_configuration_with_explicit_models(tmp_path):
    destination = tmp_path / "config" / "setup.toml"
    respond, prompts = answers(local_answers(tmp_path))
    output = []
    config = setup.wizard(destination, input_fn=respond, output_fn=output.append)
    assert load_config(destination) == config
    assert config.extraction.model == "explicit-extraction"
    assert config.embedding.model == "explicit-embedding"
    assert config.port == 8787 and config.graph_port == 16379
    assert config.pi_profiles is True and config.mcp_profiles is False
    assert not config.data_dir.exists()
    assert not config.path("state.json").exists()
    assert not config.path("corporate.token").exists()
    assert all(
        "capture" not in prompt.lower() and "publish" not in prompt.lower() for prompt in prompts
    )
    assert any("No daemon was initialized or started" in line for line in output)


def test_hidden_provider_key_is_private_and_not_in_configuration_or_output(tmp_path):
    destination = tmp_path / "config" / "setup.toml"
    values = local_answers(tmp_path)
    values[11:12] = ["new", "yes"]
    respond, _ = answers(values)
    output, hidden = [], []

    def secret(prompt):
        hidden.append(prompt)
        return "synthetic-provider-key"

    config = setup.wizard(destination, input_fn=respond, secret_fn=secret, output_fn=output.append)
    key = config.extraction.api_key_file
    assert hidden == ["Extraction API key (hidden): "]
    assert key.read_text() == "synthetic-provider-key\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert "synthetic-provider-key" not in destination.read_text() + "\n".join(output)
    assert load_config(destination) == config


def test_provider_key_storage_requires_explicit_selection(tmp_path):
    values = local_answers(tmp_path)
    values[11:] = ["new", "no"]
    respond, _ = answers(values)

    def secret(_prompt):
        raise AssertionError("A key must not be collected without storage selection")

    with pytest.raises(MemoryError, match="storage was not selected"):
        setup.wizard(
            tmp_path / "setup.toml", input_fn=respond, secret_fn=secret, output_fn=lambda _: None
        )
    assert not (tmp_path / "state").exists()


def test_existing_output_is_rejected_before_questions(tmp_path):
    destination = private(tmp_path / "setup.toml", "unowned existing configuration")

    def question(_prompt):
        raise AssertionError("No question should precede existing-path refusal")

    with pytest.raises(MemoryError, match="already exists"):
        setup.wizard(destination, input_fn=question)
    assert destination.read_text() == "unowned existing configuration"


def test_wizard_cancellation_does_not_save_configuration_or_pending_secret(tmp_path):
    values = local_answers(tmp_path)
    values[11:] = ["new", "yes"]
    iterator = iter(values)

    def respond(_prompt):
        try:
            return next(iterator)
        except StopIteration:
            raise KeyboardInterrupt

    with pytest.raises(MemoryError, match="cancelled"):
        setup.wizard(
            tmp_path / "setup.toml",
            input_fn=respond,
            secret_fn=lambda _: "synthetic-provider-key",
            output_fn=lambda _: None,
        )
    assert not (tmp_path / "state" / "extraction.key").exists()
    assert not (tmp_path / "setup.toml").exists()


@pytest.mark.parametrize("selection, expected", [("1", ["team"]), ("none", [])])
def test_hybrid_wizard_selects_shared_catalog_before_saving_binding(
    corporate, tmp_path, selection, expected
):
    config = corporate["config"]
    values = local_answers(tmp_path)
    values.pop()
    values[0] = "hybrid"
    values[1] = "test"
    values += [SERVICE, "file", str(corporate["source"]), selection, "yes"]
    respond, _ = answers(values)
    output = []
    destination = tmp_path / "setup.toml"
    result = setup.wizard(destination, input_fn=respond, output_fn=output.append)
    assert result.corporate.world_ids == expected
    assert json.loads(result.path("corporate.json").read_bytes())["world_ids"] == expected
    assert load_config(destination) == result
    assert not any("Synthetic private" in line for line in output)
    assert not config.data_dir.exists()
    assert FIRST_TOKEN not in "\n".join(output) + destination.read_text()


def test_hybrid_wizard_does_not_retain_staged_token_when_selection_is_invalid(corporate, tmp_path):
    values = local_answers(tmp_path)
    values.pop()
    values[0] = "hybrid"
    values += [SERVICE, "file", str(corporate["source"]), "3"]
    respond, _ = answers(values)
    with pytest.raises(MemoryError, match="displayed corporate"):
        setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=lambda _: None)
    assert not (tmp_path / "state" / "corporate.token").exists()
    assert not (tmp_path / "setup.toml").exists()


def test_custom_ports_and_optional_profile_choices_roundtrip(tmp_path):
    values = local_answers(tmp_path)
    values[6:8] = ["18787", "26379"]
    values[-3:-1] = ["no", "yes"]
    respond, _ = answers(values)
    config = setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=lambda _: None)
    assert (config.url, config.graph_port) == ("http://127.0.0.1:18787", 26379)
    assert config.pi_profiles is False and config.mcp_profiles is True
    assert load_config(tmp_path / "setup.toml") == config


@pytest.mark.parametrize("ports", [("8787", "8787"), ("not-a-number", "16379"), ("80", "16379")])
def test_invalid_ports_fail_before_provider_configuration(tmp_path, ports):
    values = local_answers(tmp_path)[:8]
    values[6:8] = list(ports)
    respond, _ = answers(values)
    with pytest.raises(MemoryError, match="ports"):
        setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=lambda _: None)
    assert not (tmp_path / "setup.toml").exists()


def test_existing_local_data_has_explicit_adoption_guidance(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    marker = data / "control.sqlite3"
    marker.write_bytes(b"existing authority sentinel; never opened as a database")
    values = local_answers(tmp_path)
    administrator = "mw_" + "a" * 32 + "." + "b" * 43
    admin_file = private(tmp_path / "local-admin.token", administrator + "\n")
    values[4:5] = ["adopt", str(admin_file)]
    respond, _ = answers(values)
    output = []
    setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=output.append)
    assert any("Existing local data detected" in line for line in output)
    assert any(
        "Existing administrator file:" in line and str(admin_file) in line for line in output
    )
    assert load_config(tmp_path / "setup.toml").adopt_admin_file == admin_file
    assert administrator not in (tmp_path / "setup.toml").read_text() + "\n".join(output)
    assert marker.read_bytes() == b"existing authority sentinel; never opened as a database"
    assert list(data.iterdir()) == [marker]


def test_save_confirmation_defaults_to_no_and_keeps_provider_secret_unsaved(tmp_path):
    values = local_answers(tmp_path)
    values[11:12] = ["new", "yes"]
    values[-1] = ""
    respond, _ = answers(values)
    output = []
    with pytest.raises(MemoryError, match="cancelled before saving"):
        setup.wizard(
            tmp_path / "setup.toml",
            input_fn=respond,
            secret_fn=lambda _: "synthetic-provider-key",
            output_fn=output.append,
        )
    assert any("Review setup configuration" in line for line in output)
    assert not (tmp_path / "setup.toml").exists()
    assert not (tmp_path / "state" / "extraction.key").exists()
    assert "synthetic-provider-key" not in "\n".join(output)


def test_hybrid_declined_review_does_not_commit_corporate_token(corporate, tmp_path):
    values = local_answers(tmp_path)
    values.pop()
    values[0] = "hybrid"
    values += [SERVICE, "file", str(corporate["source"]), "1", "no"]
    respond, _ = answers(values)
    output = []
    with pytest.raises(MemoryError, match="cancelled before saving"):
        setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=output.append)
    assert any(AUTHORITY in line for line in output)
    assert any("Corporate mounted world IDs: team" in line for line in output)
    assert not (tmp_path / "state" / "corporate.token").exists()
    assert not (tmp_path / "state" / "corporate.json").exists()
    assert not (tmp_path / "setup.toml").exists()


def test_hidden_key_entry_refuses_echo_fallback(tmp_path):
    values = local_answers(tmp_path)
    values[11:] = ["new", "yes"]
    respond, _ = answers(values)

    def unsafe_getpass(_prompt):
        warnings.warn("Can not control echo on the terminal.", GetPassWarning)
        raise AssertionError("The echoing fallback must not collect a secret")

    with pytest.raises(MemoryError, match="Hidden key entry is unavailable"):
        setup.wizard(
            tmp_path / "setup.toml",
            input_fn=respond,
            secret_fn=unsafe_getpass,
            output_fn=lambda _: None,
        )
    assert not (tmp_path / "setup.toml").exists()


def test_corporate_url_change_is_rejected_before_network_request(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    previous = connection_bytes(config)
    requests = len(corporate["requests"])
    config.corporate.url = "https://different.example.test"
    with pytest.raises(MemoryError, match="URL changed"):
        setup.connect_setup(config)
    assert len(corporate["requests"]) == requests
    assert connection_bytes(config) == previous


def test_wizard_refuses_to_reconfigure_existing_corporate_binding_before_questions_or_http(
    corporate, tmp_path
):
    config = corporate["config"]
    setup.connect_setup(config)
    original = connection_bytes(config)
    requests = len(corporate["requests"])
    respond, _ = answers(local_answers(tmp_path)[:6])
    with pytest.raises(MemoryError, match="already has a corporate connection"):
        setup.wizard(tmp_path / "new-config.toml", input_fn=respond, output_fn=lambda _: None)
    assert len(corporate["requests"]) == requests
    assert connection_bytes(config) == original
    assert not (tmp_path / "new-config.toml").exists()
    assert not (tmp_path / "state" / "extraction.key").exists()


@pytest.mark.parametrize("rotation", [False, True])
@pytest.mark.parametrize("boundary", ["prepared", "token", "binding", "complete"])
def test_actual_process_exit_recovers_initial_and_rotated_connection(
    corporate, tmp_path, monkeypatch, rotation, boundary
):
    config = corporate["config"]
    if rotation:
        setup.connect_setup(config)
        private(corporate["source"], NEXT_TOKEN + "\n")
        corporate["identity"]["credential_id"] = "rotated-session"
    configuration = tmp_path / "config" / "setup.toml"
    save_config(config, configuration)
    metadata = private(
        tmp_path / "response.json",
        json.dumps({"identity": corporate["identity"], "worlds": corporate["worlds"]}),
    )
    script = """
import json, os, sys
from pathlib import Path
from memory_worlds import setup_interactive as setup
from memory_worlds.models import World
from memory_worlds.setup_config import load_config
config = load_config(Path(sys.argv[1]))
metadata = json.loads(Path(sys.argv[2]).read_bytes())
setup._discover = lambda url, token: (
    metadata['identity'], [World.model_validate(item) for item in metadata['worlds']]
)
original = setup.write_private
def exit_after_publication(path, data, **kwargs):
    result = original(path, data, **kwargs)
    boundary = sys.argv[3]
    matched = (
        boundary == 'token' and path.name == 'corporate.token'
        or boundary == 'binding' and path.name == 'corporate.json'
        or boundary in ('prepared', 'complete') and path.name == 'connection.json'
        and json.loads(data)['phase'] == boundary
    )
    if matched:
        os._exit(73)
    return result
setup.write_private = exit_after_publication
setup.connect_setup(config)
raise AssertionError('The intended process-exit boundary was not reached')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(configuration), str(metadata), boundary],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 73, result.stderr.decode()
    assert not result.stdout
    journal_path = config.path("connection.json")
    journal = json.loads(journal_path.read_bytes())
    assert journal["phase"] == ("complete" if boundary == "complete" else "prepared")
    assert (
        FIRST_TOKEN not in journal_path.read_text() and NEXT_TOKEN not in journal_path.read_text()
    )
    original = setup._discover
    recovered = []

    def discover_after_recovery(url, token):
        state = json.loads(journal_path.read_bytes())
        assert state["phase"] in {"complete", "abandoned"}
        if config.path("corporate.token").exists():
            managed, binding = connection_bytes(config)
            assert json.loads(binding)["token_sha256"] == hashlib.sha256(managed).hexdigest()
        else:
            assert not config.path("corporate.json").exists()
        recovered.append(state["phase"])
        return original(url, token)

    monkeypatch.setattr(setup, "_discover", discover_after_recovery)
    receipt = setup.connect_setup(config)
    assert len(recovered) == 1
    expected_token = NEXT_TOKEN if rotation else FIRST_TOKEN
    assert config.path("corporate.token").read_text() == expected_token + "\n"
    assert receipt["world_ids"] == ["team"]
    assert receipt["authority_id"] == AUTHORITY and receipt["user_id"] == "alice"
    assert json.loads(journal_path.read_bytes())["phase"] == "complete"


def test_completed_binding_without_owned_journal_is_not_adopted(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    previous = connection_bytes(config)
    requests = len(corporate["requests"])
    config.path("connection.json").unlink()
    with pytest.raises(MemoryError, match="journal is missing"):
        setup.connect_setup(config)
    assert connection_bytes(config) == previous
    assert len(corporate["requests"]) == requests


def test_pending_connection_rejects_unexpected_file_changes(corporate, monkeypatch):
    config = corporate["config"]
    setup.connect_setup(config)
    previous = json.loads(config.path("connection.json").read_bytes())
    previous["phase"] = "prepared"
    path = config.path("connection.json")
    setup.write_private(
        path, setup._encode(previous), expected_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    private(config.path("corporate.token"), "unexpected-private-file-content\n")
    requests = len(corporate["requests"])
    with pytest.raises(MemoryError, match="changed outside"):
        setup.connect_setup(config)
    assert config.path("corporate.token").read_text() == "unexpected-private-file-content\n"
    assert len(corporate["requests"]) == requests


@pytest.mark.parametrize("rotation", [False, True])
def test_lost_binding_write_ack_can_restore_metadata_after_token_rollback(
    corporate, monkeypatch, rotation
):
    config = corporate["config"]
    previous = None
    if rotation:
        setup.connect_setup(config)
        previous = connection_bytes(config)
        private(corporate["source"], NEXT_TOKEN + "\n")
        corporate["identity"]["credential_id"] = "rotated-session"
    original = setup.write_private

    def lost_ack(path, data, **kwargs):
        result = original(path, data, **kwargs)
        if path.name == "corporate.json":
            raise OSError("synthetic lost publication acknowledgement")
        return result

    monkeypatch.setattr(setup, "write_private", lost_ack)
    with pytest.raises(OSError, match="lost publication"):
        setup.connect_setup(config)
    assert json.loads(config.path("connection.json").read_bytes())["phase"] == "prepared"
    if previous is None:
        assert not config.path("corporate.token").exists()
    else:
        assert config.path("corporate.token").read_bytes() == previous[0]
    assert config.path("corporate.json").exists()
    monkeypatch.setattr(setup, "write_private", original)
    original_discover = setup._discover

    def verify_old_pair_before_new_authentication(url, token):
        assert json.loads(config.path("connection.json").read_bytes())["phase"] == "abandoned"
        if previous is None:
            assert not config.path("corporate.token").exists()
            assert not config.path("corporate.json").exists()
        else:
            assert connection_bytes(config) == previous
        return original_discover(url, token)

    monkeypatch.setattr(setup, "_discover", verify_old_pair_before_new_authentication)
    receipt = setup.connect_setup(config)
    assert receipt["world_ids"] == ["team"]
    assert json.loads(config.path("connection.json").read_bytes())["phase"] == "complete"


def test_malformed_connection_phase_fails_cleanly_without_network(corporate):
    config = corporate["config"]
    setup.connect_setup(config)
    path = config.path("connection.json")
    before = connection_bytes(config)
    data = path.read_bytes()
    intent = json.loads(data)
    intent["phase"] = []
    setup.write_private(path, setup._encode(intent), expected_hash=hashlib.sha256(data).hexdigest())
    count = len(corporate["requests"])
    with pytest.raises(MemoryError, match="journal is invalid"):
        setup.connect_setup(config)
    assert len(corporate["requests"]) == count
    assert connection_bytes(config) == before


def test_new_authority_choice_refuses_nonempty_data_before_further_questions(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sentinel = data / "keep.txt"
    sentinel.write_text("unrelated local data")
    respond, _ = answers(local_answers(tmp_path)[:5])
    with pytest.raises(MemoryError, match="requires an empty data directory"):
        setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=lambda _: None)
    assert sentinel.read_text() == "unrelated local data"
    assert not (tmp_path / "setup.toml").exists()
    assert not (tmp_path / "state").exists()


def test_adoption_choice_refuses_empty_directory_before_collecting_administrator(tmp_path):
    values = local_answers(tmp_path)[:5]
    values[4] = "adopt"
    respond, _ = answers(values)
    with pytest.raises(MemoryError, match="requires an existing local authority"):
        setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=lambda _: None)
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "setup.toml").exists()


def test_adoption_reference_requires_private_bounded_administrator_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "control.sqlite3").write_text("not opened by the wizard")
    administrator = private(tmp_path / "administrator.token", "synthetic-invalid-credential\n")
    administrator.chmod(0o644)
    values = local_answers(tmp_path)[:4] + ["adopt", str(administrator)]
    respond, _ = answers(values)
    with pytest.raises(MemoryError, match="0600"):
        setup.wizard(tmp_path / "setup.toml", input_fn=respond, output_fn=lambda _: None)
    assert not (tmp_path / "setup.toml").exists()
    assert not (tmp_path / "state").exists()


def test_interactive_adoption_saves_only_reference_and_applies_existing_authority(tmp_path):
    from memory_worlds.setup_provision import apply_setup
    from memory_worlds.store import MemoryStore

    store = MemoryStore(tmp_path / "data")
    original, secret = store.bootstrap("original-owner")
    administrator = private(tmp_path / "administrator.token", secret + "\n")
    before = store.control_path.read_bytes()
    values = local_answers(tmp_path)
    # Existing data defaults to adoption; final save still requires explicit yes.
    values[4:5] = ["", str(administrator)]
    respond, prompts = answers(values)
    output = []
    destination = tmp_path / "setup.toml"
    config = setup.wizard(destination, input_fn=respond, output_fn=output.append)
    assert any("Local authority" in prompt and "[adopt]" in prompt for prompt in prompts)
    assert config.adopt_admin_file == administrator
    assert load_config(destination) == config
    assert store.control_path.read_bytes() == before, (
        "wizard must not authenticate or modify SQLite"
    )
    assert secret not in destination.read_text() + "\n".join(output)
    assert not config.path("operator.token").exists()
    receipt = apply_setup(load_config(destination))
    assert receipt["authority_id"] == store.authority_id
    assert store.get_world(store.authenticate(secret), original.id).id == original.id
    assert not config.path("operator.token").exists(), "existing administrator token is referenced"
