"""Explicit LangSmith spans for memory operations, without ambient state tracing.

This module imports LangSmith lazily. Install ``memory-worlds[langgraph]`` to use
the graph invocation helpers or the LangSmith sink.
"""

import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

TraceMode = Literal["off", "ids-only", "selected-content"]
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
_OPERATION = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_UUID_TEXT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PARENT_TRACE = re.compile(
    rf"^\d{{8}}T\d{{9,12}}Z{_UUID_TEXT}(?:\.\d{{8}}T\d{{9,12}}Z{_UUID_TEXT}){{0,31}}$"
)
_UUID_FIELDS = {
    "execution_id",
    "run_id",
    "agent_id",
    "context_id",
    "invocation_id",
    "parent_invocation_id",
    "scope_id",
    "source_scope_id",
    "destination_scope_id",
}
_IDENTIFIER_FIELDS = {
    "authority_id",
    "world_id",
    "source_world_id",
    "destination_world_id",
    "episode_id",
}


def _make_client(endpoint: str, api_key: str, workspace_id: str):
    """Own the transport and SDK policy rather than mutate an application client."""
    import requests
    from langsmith import Client

    class TraceSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    session = TraceSession()
    session.trust_env = False
    return Client(
        api_urls={endpoint: api_key},
        api_key=api_key,
        workspace_id=workspace_id,
        session=session,
        auto_batch_tracing=False,
        hide_inputs=False,
        hide_outputs=False,
        hide_metadata=False,
        omit_traced_runtime_info=True,
        tracing_mode="langsmith",
        tracing_sampling_rate=1,
        disable_prompt_cache=True,
    )


def _parent(trace, client, project_name):
    from langsmith.run_trees import RunTree

    # Upstream parsing sets a context variable. Keep that side effect inside a
    # copied context so unrelated application traces retain their own parent.
    return copy_context().run(
        RunTree.from_headers,
        {"langsmith-trace": trace},
        project_name=project_name,
        ls_client=client,
        replicas=[],
    )


@dataclass(frozen=True)
class TracePolicy:
    """Content consent for the explicit memory spans; default is no telemetry."""

    mode: TraceMode = "off"

    def __post_init__(self):
        if self.mode not in {"off", "ids-only", "selected-content"}:
            raise ValueError("Trace mode must be off, ids-only, or selected-content")


def _correlation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {}
    for key in _UUID_FIELDS:
        if value.get(key) is not None:
            try:
                result[key] = str(UUID(str(value[key])))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("Trace correlation contains an invalid UUID") from None
    for key in _IDENTIFIER_FIELDS:
        if value.get(key) is not None:
            item = str(value[key])
            if not _IDENTIFIER.fullmatch(item):
                raise ValueError("Trace correlation contains an invalid identifier")
            result[key] = item
    if value.get("digest") is not None:
        digest = value["digest"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Trace correlation contains an invalid digest")
        result["digest"] = digest
    for key in ("read_scopes", "source_scope_ids"):
        if value.get(key) is not None:
            items = value[key]
            if not isinstance(items, list) or len(items) > 50:
                raise ValueError("Trace correlation contains invalid source scopes")
            scopes = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {"world_id", "scope_id"}:
                    raise ValueError("Trace correlation contains invalid source scopes")
                selected = _correlation(item)
                if set(selected) != {"world_id", "scope_id"}:
                    raise ValueError("Trace correlation contains invalid source scopes")
                scopes.append(selected)
            result[key] = scopes
    return result


class LangSmithTraceSink:
    """Send selected memory spans using an owned client and explicit destination.

    Distributed parent propagation accepts ``langsmith-trace`` only. Baggage,
    authorization, ambient tags, metadata, replicas and callback inputs are never
    copied into these spans.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        project_name: str,
        workspace_id: str = "",
        policy: TracePolicy = TracePolicy(),
        parent_headers: Mapping[str, str] | None = None,
    ):
        if not isinstance(project_name, str) or not project_name.strip():
            raise ValueError("A LangSmith project name is required")
        if not isinstance(endpoint, str) or endpoint != endpoint.strip():
            raise ValueError("LangSmith requires an explicit HTTPS or loopback endpoint")
        parsed = urlsplit(endpoint)
        try:
            local = parsed.hostname == "localhost" or ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme == "http"
            and not local
        ):
            raise ValueError("LangSmith requires an explicit HTTPS or loopback endpoint")
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096:
            raise ValueError("An explicit LangSmith API key is required")
        if not isinstance(workspace_id, str) or (
            workspace_id and not _IDENTIFIER.fullmatch(workspace_id)
        ):
            raise ValueError("Invalid LangSmith workspace identifier")
        self._client = _make_client(endpoint.rstrip("/"), api_key, workspace_id)
        self.project_name = project_name
        self.policy = policy
        self._parent_trace = None
        if parent_headers is not None:
            value = parent_headers.get("langsmith-trace")
            if value is not None:
                if (
                    not isinstance(value, str)
                    or not 1 <= len(value) <= 8192
                    or not _PARENT_TRACE.fullmatch(value)
                ):
                    self.close()
                    raise ValueError("Invalid distributed trace parent")
                # Validate through the supported upstream parser, with no baggage.
                try:
                    parent = _parent(value, self._client, project_name)
                except (ValueError, TypeError, IndexError):
                    self.close()
                    raise ValueError("Invalid distributed trace parent") from None
                if parent is None:
                    self.close()
                    raise ValueError("Invalid distributed trace parent")
                self._parent_trace = value

    def close(self):
        """Release the owned client after all synchronous span writes complete."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def record(
        self,
        operation: str,
        correlation: dict,
        *,
        selected_content: dict | None = None,
        error: bool = False,
    ) -> None:
        if self.policy.mode == "off":
            return
        if not isinstance(operation, str) or not _OPERATION.fullmatch(operation):
            raise ValueError("Invalid memory trace operation")
        metadata = _correlation(correlation)
        inputs = {}
        if self.policy.mode == "selected-content" and selected_content is not None:
            if (
                len(selected_content) > 20
                or any(
                    not isinstance(key, str)
                    or not _OPERATION.fullmatch(key)
                    or not isinstance(value, str)
                    or len(value) > 200_000
                    for key, value in selected_content.items()
                )
                or sum(len(value) for value in selected_content.values()) > 1_000_000
            ):
                raise ValueError("Invalid selected trace content")
            inputs = selected_content.copy()
        from langsmith.run_trees import RunTree

        parent = (
            _parent(self._parent_trace, self._client, self.project_name)
            if self._parent_trace
            else None
        )
        if parent is None:
            run = RunTree(
                name=f"memory-worlds.{operation}",
                run_type="tool",
                project_name=self.project_name,
                ls_client=self._client,
                inputs=inputs,
                extra={"metadata": metadata},
                tags=[],
                replicas=[],
            )
        else:
            run = parent.create_child(
                f"memory-worlds.{operation}",
                run_type="tool",
                inputs=inputs,
                extra={"metadata": metadata},
                tags=[],
            )
        run.post()
        run.end(outputs={}, error="Memory operation failed" if error else None)
        run.patch(exclude_inputs=True)


@contextmanager
def without_langsmith_tracing():
    """Disable ambient LangSmith graph/node tracing for the complete invocation.

    This does not suppress arbitrary application callbacks or checkpoint writes.
    Explicit ``LangSmithTraceSink`` records remain controlled by their own policy.
    """
    from langsmith import tracing_context

    with tracing_context(enabled=False, parent=False, tags=[], metadata={}, replicas=[]):
        yield


def invoke_workflow(graph, inputs, *, context, config=None, **kwargs):
    """Invoke a graph while preventing ambient LangSmith state/body tracing."""
    with without_langsmith_tracing():
        return graph.invoke(inputs, config, context=context, **kwargs)


async def ainvoke_workflow(graph, inputs, *, context, config=None, **kwargs):
    """Async form of :func:`invoke_workflow`; the boundary covers the awaited run."""
    with without_langsmith_tracing():
        return await graph.ainvoke(inputs, config, context=context, **kwargs)
