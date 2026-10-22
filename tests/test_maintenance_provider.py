import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from memory_worlds.errors import MemoryError
from memory_worlds.maintenance_provider import (
    OpenAICompatibleWorker,
    ProviderError,
    WorkerProfile,
    WorkerProfiles,
    checked_output,
)


def profile(tmp_path, **changes):
    key = tmp_path / "worker.key"
    key.write_text("provider-secret-canary")
    key.chmod(0o600)
    return WorkerProfile(
        **{
            "endpoint": "https://models.example.test/v1",
            "model": "configured-luna-route",
            "api_key_file": key,
            **changes,
        }
    )


def output(bundle_ids, **changes):
    return {
        "kind": "summary",
        "content": "Decision remains tentative pending measurement.",
        "source_bundle_ids": [str(item) for item in bundle_ids],
        "unresolved_questions": ["Which measurement supports the decision?"],
        "contradictions": [],
        "flags": [],
        **changes,
    }


async def test_explicit_profile_and_bounded_proposal_only_wire(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret-must-not-leak")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    selected = profile(tmp_path)
    bundle_ids = [uuid4()]
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(output(bundle_ids)),
                        },
                    }
                ]
            },
        )

    worker = OpenAICompatibleWorker(transport=httpx.MockTransport(respond))
    assert await worker.propose(
        selected, kind="summarize", text="Private evidence", bundle_ids=bundle_ids
    ) == output(bundle_ids)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://models.example.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer provider-secret-canary"
    wire = json.loads(request.content)
    assert wire["model"] == selected.model
    assert wire["tools"] == [] and wire["tool_choice"] == "none"
    assert wire["max_tokens"] == selected.max_output_tokens
    assert "ambient-secret" not in request.content.decode()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://models.example.test/v1",
        "https://user:secret@models.example.test/v1",
        "https://models.example.test/v1?key=secret",
        "https://models.example.test/v1#secret",
        "file:///tmp/model",
        "https://models.example.test/\nsecret",
        "https://models.example.test:bad/v1",
    ],
)
def test_forbidden_provider_endpoints(tmp_path, endpoint):
    with pytest.raises(ValidationError):
        profile(tmp_path, endpoint=endpoint)


async def test_redirect_is_not_followed_or_secret_forwarded(tmp_path):
    calls = []

    async def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example.test/v1"})

    worker = OpenAICompatibleWorker(transport=httpx.MockTransport(redirect))
    with pytest.raises(ProviderError, match="provider_rejected_request"):
        await worker.propose(
            profile(tmp_path), kind="summarize", text="Evidence", bundle_ids=[uuid4()]
        )
    assert calls == ["https://models.example.test/v1/chat/completions"]


@pytest.mark.parametrize(
    "malformed",
    [
        {"choices": []},
        {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": [{}]}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "provider-secret-error"}}]},
    ],
)
async def test_invalid_provider_output_is_sanitized(tmp_path, malformed):
    worker = OpenAICompatibleWorker(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=malformed))
    )
    with pytest.raises(ProviderError, match="invalid_provider_output") as error:
        await worker.propose(
            profile(tmp_path), kind="summarize", text="Evidence", bundle_ids=[uuid4()]
        )
    assert "provider-secret" not in str(error.value)


async def test_transport_error_reports_uncertain_usage_without_provider_text(tmp_path):
    async def fail(request):
        raise httpx.ReadTimeout("private-provider-response-and-credential")

    worker = OpenAICompatibleWorker(transport=httpx.MockTransport(fail))
    with pytest.raises(ProviderError) as error:
        await worker.propose(
            profile(tmp_path), kind="summarize", text="Evidence", bundle_ids=[uuid4()]
        )
    assert error.value.usage_uncertain and error.value.retryable
    assert str(error.value) == "provider_unavailable"


async def test_oversized_input_never_dispatches_and_unsafe_key_never_dispatches(tmp_path):
    calls = []
    worker = OpenAICompatibleWorker(
        transport=httpx.MockTransport(lambda request: calls.append(request))
    )
    selected = profile(tmp_path)
    with pytest.raises(ProviderError, match="input_budget_exceeded"):
        await worker.propose(selected, kind="summarize", text="🙂" * 4000, bundle_ids=[uuid4()])
    selected.api_key_file.chmod(0o644)
    with pytest.raises(ProviderError, match="provider_credentials_unavailable"):
        await worker.propose(selected, kind="summarize", text="Evidence", bundle_ids=[uuid4()])
    assert not calls


def test_strict_source_coverage_and_no_model_chosen_actions():
    selected = [uuid4()]
    for invalid in [
        output([uuid4()]),
        output(selected, action="erase"),
        output(selected, destination="company"),
        output(selected, content="  "),
        output(
            selected,
            flags=[
                {"reason": "needs-validation", "bundle_id": str(selected[0]), "question": "Check"}
            ],
        ),
    ]:
        with pytest.raises(ProviderError, match="invalid_provider_output"):
            checked_output(invalid, kind="summarize", bundle_ids=selected)


def test_profiles_have_no_ambient_defaults_and_explicit_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    assert WorkerProfiles.from_env({}).profiles == {}
    config = tmp_path / "maintenance.toml"
    config.write_text(
        '[maintenance]\ndefault_profile="lightweight"\n'
        '[maintenance.profiles.lightweight]\nendpoint="https://models.example.test/v1"\n'
        'model="my-configured-luna"\napi_key_file="' + str(tmp_path / "key") + '"\n'
    )
    config.chmod(0o600)
    loaded = WorkerProfiles.from_env({"MEMORY_WORLDS_MAINTENANCE_CONFIG": str(config)})
    assert loaded.profiles["lightweight"].model == "my-configured-luna"
    assert "ambient-secret" not in loaded.model_dump_json()
    config.chmod(0o644)
    with pytest.raises(MemoryError):
        WorkerProfiles.from_env({"MEMORY_WORLDS_MAINTENANCE_CONFIG": str(config)})


async def test_duplicate_keys_and_compressed_provider_responses_are_refused(tmp_path):
    responses = [
        httpx.Response(200, content=b'{"choices": [], "choices": []}'),
        httpx.Response(200, headers={"Content-Encoding": "unknown"}, content=b"not-decoded"),
    ]
    for response in responses:
        worker = OpenAICompatibleWorker(transport=httpx.MockTransport(lambda request: response))
        with pytest.raises(ProviderError, match="invalid_provider_output"):
            await worker.propose(
                profile(tmp_path), kind="summarize", text="Evidence", bundle_ids=[uuid4()]
            )


def test_unknown_provider_errors_do_not_become_public_diagnostics():
    assert str(ProviderError("secret provider response")) == "provider_failed"
