"""Verify real LangSmith span serialization and LangGraph tracing suppression."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypedDict
from unittest.mock import patch
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langsmith import Client, tracing_context
from langsmith.run_trees import RunTree

from memory_worlds.langgraph_tracing import (
    LangSmithTraceSink,
    TracePolicy,
    ainvoke_workflow,
    invoke_workflow,
)


class RecordingClient(Client):
    def __init__(self):
        super().__init__(
            api_url="http://127.0.0.1:1",
            api_key="test-only-not-delivered",
            auto_batch_tracing=False,
            info={},
        )
        self.sent = []

    def create_run(self, *args, **kwargs):
        self.sent.append(("create", args, kwargs))

    def update_run(self, *args, **kwargs):
        self.sent.append(("update", args, kwargs))


def _sink(client, **kwargs):
    with patch("memory_worlds.langgraph_tracing._make_client", return_value=client):
        return LangSmithTraceSink(
            endpoint="http://127.0.0.1:1", api_key="test-only-not-delivered", **kwargs
        )


def _serialize(value):
    return json.dumps(value, default=str)


def test_default_trace_policy_sends_nothing():
    client = RecordingClient()
    sink = _sink(client, project_name="memory-tests")
    sink.record("search", {"query": "private query"}, selected_content={"content": "secret"})
    assert client.sent == []


def test_ids_only_ignores_content_unknown_metadata_and_ambient_destinations():
    client = RecordingClient()
    execution_id, context_id, scope_id = uuid4(), uuid4(), uuid4()
    parent = RunTree(
        name="upstream",
        project_name="wrong-project",
        extra={"metadata": {"ambient-secret": "do not copy"}},
        tags=["private-tag"],
        ls_client=client,
    )
    headers = parent.to_headers()
    headers["Authorization"] = "private bearer canary"
    sink = _sink(
        client,
        project_name="memory-tests",
        policy=TracePolicy("ids-only"),
        parent_headers=headers,
    )
    with tracing_context(
        enabled=True,
        parent=parent,
        client=client,
        metadata={"private-metadata": "do not copy"},
        tags=["private-tag"],
    ):
        sink.record(
            "search",
            {
                "execution_id": execution_id,
                "context_id": context_id,
                "authority_id": "authority123",
                "read_scopes": [{"world_id": "personal", "scope_id": str(scope_id)}],
                "token": "private bearer canary",
                "query": "private query canary",
                "operation_key": "private operation key",
            },
            selected_content={"content": "private content canary"},
            error=True,
        )
    payload = _serialize(client.sent)
    assert str(execution_id) in payload
    assert str(context_id) in payload
    assert str(scope_id) in payload
    for excluded in (
        "private bearer canary",
        "private query canary",
        "private content canary",
        "private operation key",
        "private-metadata",
        "ambient-secret",
        "private-tag",
        "wrong-project",
    ):
        assert excluded not in payload
    created = client.sent[0][2]
    assert created["parent_run_id"] == parent.id
    assert created["trace_id"] == parent.trace_id
    assert created["session_name"] == "memory-tests"
    assert created["inputs"] == {}
    assert client.sent[1][2]["error"] == "Memory operation failed"


def test_selected_content_mode_only_sends_explicit_content():
    client = RecordingClient()
    sink = _sink(client, project_name="memory-tests", policy=TracePolicy("selected-content"))
    sink.record(
        "capture",
        {"episode_id": "episode123", "hidden": "not allowed"},
        selected_content={"content": "user-selected finding"},
    )
    assert client.sent[0][2]["inputs"] == {"content": "user-selected finding"}
    assert "not allowed" not in _serialize(client.sent)


@pytest.mark.parametrize("mode", ["auto", "full", "", True])
def test_trace_policy_rejects_unknown_modes(mode):
    with pytest.raises(ValueError, match="Trace mode"):
        TracePolicy(mode)


@pytest.mark.parametrize(
    "correlation",
    [
        {"execution_id": "secret instead of UUID"},
        {"world_id": "contains spaces"},
        {"digest": "not-a-digest"},
        {"read_scopes": [{"world_id": None, "scope_id": None}]},
        {"read_scopes": [{"world_id": "personal", "scope_id": str(uuid4()), "token": "x"}]},
    ],
)
def test_invalid_correlation_is_rejected_before_delivery(correlation):
    client = RecordingClient()
    sink = _sink(client, project_name="memory-tests", policy=TracePolicy("ids-only"))
    with pytest.raises(ValueError, match="Trace correlation"):
        sink.record("search", correlation)
    assert client.sent == []


def test_invalid_parent_rejected_without_delivery():
    client = RecordingClient()
    with pytest.raises(ValueError, match="Invalid distributed trace parent"):
        _sink(
            client,
            project_name="memory-tests",
            policy=TracePolicy("ids-only"),
            parent_headers={"langsmith-trace": "secret instead of trace"},
        )
    assert client.sent == []


def _graph():
    class State(TypedDict):
        body: str

    builder = StateGraph(State)
    builder.add_node("copy", lambda state: {"body": state["body"]})
    builder.add_edge(START, "copy")
    builder.add_edge("copy", END)
    return builder.compile()


def test_invocation_wrapper_suppresses_actual_ambient_langsmith_state_tracing():
    graph = _graph()
    client = RecordingClient()
    with tracing_context(enabled=True, client=client, project_name="memory-tests"):
        graph.invoke({"body": "private graph state canary"})
        assert "private graph state canary" in _serialize(client.sent)
        client.sent.clear()
        result = invoke_workflow(graph, {"body": "private graph state canary"}, context=None)
    assert result == {"body": "private graph state canary"}
    assert client.sent == []


async def test_async_invocation_wrapper_suppresses_ambient_langsmith_state_tracing():
    client = RecordingClient()
    with tracing_context(enabled=True, client=client, project_name="memory-tests"):
        result = await ainvoke_workflow(_graph(), {"body": "private state"}, context=None)
    assert result == {"body": "private state"}
    assert client.sent == []


@pytest.mark.parametrize("ambient_destination", ["endpoint", "replicas"])
def test_owned_real_sdk_transport_excludes_environment_metadata_and_destinations(
    monkeypatch, ambient_destination
):
    from langsmith.env._runtime_env import get_langchain_env_var_metadata

    received = []
    api_key = "explicit-synthetic-local-key"
    private = "synthetic-private-environment-canary"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.collect()

        def do_PATCH(self):
            self.collect()

        def collect(self):
            size = int(self.headers.get("Content-Length", "0"))
            received.append(
                {
                    "path": self.path,
                    "body": json.loads(self.rfile.read(size)),
                    "api_key": self.headers.get("x-api-key"),
                    "workspace": self.headers.get("x-tenant-id"),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        with monkeypatch.context() as environment:
            for name in ("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT", "LANGSMITH_RUNS_ENDPOINTS"):
                environment.delenv(name, raising=False)
            environment.setenv("LANGSMITH_PRIVATE_CONTEXT_CANARY", private)
            environment.setenv("LANGSMITH_TRACING_MODE", "hybrid")
            environment.setenv("LANGSMITH_API_KEY", "ambient-api-key-canary")
            environment.setenv("LANGSMITH_WORKSPACE_ID", "ambient-workspace-canary")
            if ambient_destination == "endpoint":
                environment.setenv("LANGSMITH_ENDPOINT", f"{endpoint}/unexpected-ambient")
            else:
                environment.setenv(
                    "LANGSMITH_RUNS_ENDPOINTS",
                    json.dumps({f"{endpoint}/unexpected-replica": "ambient-replica-key"}),
                )
            get_langchain_env_var_metadata.cache_clear()
            with LangSmithTraceSink(
                endpoint=endpoint,
                api_key=api_key,
                project_name="memory-tests",
                policy=TracePolicy("ids-only"),
            ) as sink:
                sink.record(
                    "search",
                    {"context_id": str(uuid4()), "private_metadata": private},
                    selected_content={"query": private, "content": private},
                    error=True,
                )
        assert len(received) == 2
        assert received[0]["path"] == "/runs"
        assert received[1]["path"].startswith("/runs/")
        assert all(item["api_key"] == api_key for item in received)
        assert all(item["workspace"] is None for item in received)
        payload = _serialize([item["body"] for item in received])
        for excluded in (
            private,
            api_key,
            "ambient-api-key",
            "ambient-workspace",
            "ambient-replica",
        ):
            assert excluded not in payload
        assert received[0]["body"]["inputs"] == {}
        assert received[1]["body"]["error"] == "Memory operation failed"
    finally:
        get_langchain_env_var_metadata.cache_clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
