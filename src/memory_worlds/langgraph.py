"""Runtime memory for checkpointed workflows; identifiers never grant access.

Inject these objects through LangGraph runtime context, never graph state. The
HTTP authority remains the permission boundary. Local records preserve exact
write intents and conservative exposure across application restarts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .client import Client
from .errors import Conflict, Forbidden, MemoryError
from .identity_names import CreateIdentity
from .langgraph_journal import Journal
from .models import (
    AgentContext,
    BindContext,
    EpisodeInput,
    Publication,
    Receipt,
    ScopeFlow,
    ScopeSelection,
    SearchRequest,
)


class WorkflowError(MemoryError):
    """A sanitized workflow-boundary failure, without upstream response bodies."""


class TraceSink(Protocol):
    def record(
        self, operation: str, correlation: dict, *, selected_content=None, error: bool = False
    ) -> None: ...


def _boundary(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except MemoryError:
            if args and isinstance(args[0], WorkflowMemory):
                args[0]._trace(function.__name__, error=True)
            raise
        except Exception:
            if args and isinstance(args[0], WorkflowMemory):
                args[0]._trace(function.__name__, error=True)
            raise WorkflowError(
                "Memory workflow operation failed; check its live authorization"
            ) from None

    return checked


def _key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 500
        or any(not character.isprintable() for character in value)
    ):
        raise WorkflowError("Workflow operation keys must be bounded printable strings")
    return value


def _json(value) -> dict:
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _selections(values) -> list[ScopeSelection]:
    parsed = [ScopeSelection.model_validate(item) for item in values]
    if len(parsed) > 50 or len({(item.world_id, item.scope_id) for item in parsed}) != len(parsed):
        raise WorkflowError("Select at most 50 distinct readable scopes")
    return sorted(parsed, key=lambda item: (item.world_id, str(item.scope_id)))


def _selection(value) -> ScopeSelection:
    return ScopeSelection.model_validate(value)


class MemoryWorldsAdapter:
    """One live ordinary credential and one private, identity-bound journal."""

    @_boundary
    def __init__(self, client: Client, journal_dir: Path, *, trace_sink: TraceSink | None = None):
        self._client = client
        self._trace_sink = trace_sink
        self._url = str(client.http.base_url).rstrip("/")
        parsed = urlsplit(self._url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or client.http.follow_redirects
        ):
            raise WorkflowError("Use HTTPS or an authenticated loopback service")
        if parsed.scheme == "http":
            try:
                local = (
                    parsed.hostname == "localhost"
                    or ipaddress.ip_address(parsed.hostname or "").is_loopback
                )
            except ValueError:
                local = False
            if not local:
                raise WorkflowError("Remote memory services require HTTPS")
        identity = client.me()
        if identity.get("admin") is not False:
            raise Forbidden("Workflow runtimes require an ordinary user or delegated credential")
        self._identity = {
            key: identity[key] for key in ("authority_id", "credential_id", "user_id", "restricted")
        }
        self._journal = Journal(
            journal_dir,
            service_url=self._url,
            authority_id=identity["authority_id"],
            credential_id=identity["credential_id"],
        )

    def __repr__(self):
        return "<MemoryWorldsAdapter runtime dependency>"

    def __getstate__(self):
        raise TypeError("Memory clients belong in runtime context, outside checkpoints")

    def _live(self):
        if str(self._client.http.base_url).rstrip("/") != self._url:
            raise Conflict("Workflow service binding changed")
        identity = self._client.me()
        if identity.get("admin") is not False or any(
            identity.get(key) != value for key, value in self._identity.items()
        ):
            raise Conflict("Workflow credential or authority binding changed")
        return identity

    @_boundary
    def execution(
        self,
        execution_key: str,
        *,
        agent_id: UUID,
        read_scopes: list[ScopeSelection],
        write_scope: ScopeSelection,
        run_id: UUID | None = None,
        context_id: UUID | None = None,
    ) -> WorkflowMemory:
        """Resume one fixed context; keys and checkpoint IDs cannot broaden it."""
        execution_key = _key(execution_key)
        self._live()
        agent_id = UUID(str(agent_id))
        reads, write = _selections(read_scopes), _selection(write_scope)
        if context_id is None:
            self._client.identity("agent", agent_id)
        parameters = {
            "agent_id": str(agent_id),
            "run_id": str(UUID(str(run_id))) if run_id else None,
            "read_scopes": [item.model_dump(mode="json") for item in reads],
            "write_scope": write.model_dump(mode="json"),
            "context_id": str(UUID(str(context_id))) if context_id else None,
        }

        def create():
            if context_id is not None:
                context = self._client.get_context(context_id)
                if (
                    context.agent_id != agent_id
                    or context.run_id is None
                    or run_id is not None
                    and context.run_id != UUID(str(run_id))
                    or _selections(context.read_scopes) != reads
                    or context.write_scope != write
                ):
                    raise Conflict("Delegated context differs from the requested execution")
                # An attached context can have been used before this adapter.
                # Conservatively include its declared readable/write audiences.
                authority = self._identity["authority_id"]
                self._journal.record_exposure(
                    str(context.id),
                    scopes=[
                        {"authority_id": authority, **item.model_dump(mode="json")}
                        for item in [*reads, write]
                    ],
                )
                return {"id": str(uuid4()), "context": context.model_dump(mode="json")}
            planned = self._journal.freeze(
                "run",
                execution_key,
                {"requested": parameters["run_id"]},
                lambda: {"id": parameters["run_id"] or str(uuid4())},
            )
            selected_run = UUID(planned["id"])
            if run_id is None:
                # The registry accepts an explicit UUID idempotently. Lost replies
                # do not replace the planned run with a fresh identity.
                self._client.create_identity(
                    "run", CreateIdentity(id=selected_run, name="LangGraph workflow")
                )
            else:
                self._client.identity("run", selected_run)
            context = self._client.bind_context(
                BindContext(
                    agent_id=agent_id, run_id=selected_run, read_scopes=reads, write_scope=write
                )
            )
            self._journal.record_exposure(str(context.id))
            return {"id": str(uuid4()), "context": context.model_dump(mode="json")}

        record = self._journal.freeze("execution", execution_key, parameters, create)
        result = WorkflowMemory(
            self, UUID(record["id"]), AgentContext.model_validate(record["context"])
        )
        result._live()
        return result


class WorkflowMemory:
    """A live workflow invocation. Use IDs or receipts, rather than this object, in state."""

    def __init__(self, adapter: MemoryWorldsAdapter, execution_id: UUID, context: AgentContext):
        self._adapter = adapter
        self._context = context.model_copy(deep=True)
        self.execution_id = execution_id

    def __repr__(self):
        return "<WorkflowMemory runtime dependency>"

    def __getstate__(self):
        raise TypeError("Workflow memory belongs in runtime context, outside checkpoints")

    @property
    def context(self) -> AgentContext:
        return self._context.model_copy(deep=True)

    @property
    def run_id(self) -> UUID:
        return self._context.run_id

    @property
    def _journal(self) -> Journal:
        return self._adapter._journal

    @property
    def _client(self) -> Client:
        return self._adapter._client

    def _live(self):
        self._adapter._live()
        current = self._client.get_context(self._context.id)
        if current != self._context:
            raise Conflict("Workflow context changed")
        if self._journal.get("exposure", str(self._context.id)) is None:
            raise Conflict("Workflow exposure record is missing; restore its private journal")

    def _record(self, scopes=(), *, unknown=False):
        authority = self._adapter._identity["authority_id"]
        return self._journal.record_exposure(
            str(self._context.id),
            scopes=[{"authority_id": authority, **item.model_dump(mode="json")} for item in scopes],
            unknown=unknown,
        )

    def _ledger(self):
        if self._journal.get("exposure", str(self._context.id)) is None:
            raise Conflict("Workflow exposure record is missing")
        return self._journal.exposure(str(self._context.id))

    def _flow(self, destination: ScopeSelection):
        ledger = self._ledger()
        authority = self._adapter._identity["authority_id"]
        if ledger["unknown"] or any(item != authority for item in ledger["authorities"]):
            raise Forbidden("Automatic writes require known exposure within this authority")
        sources = _selections(
            [
                {"world_id": item["world_id"], "scope_id": item["scope_id"]}
                for item in ledger["scopes"]
            ]
        )
        if not self._client.can_flow(ScopeFlow(read_scopes=sources, write_scope=destination)):
            raise Forbidden("Workflow exposure cannot flow automatically to that destination")

    def _trace(self, operation: str, *, destination=None, receipt=None, error=False):
        sink = self._adapter._trace_sink
        if sink is None:
            return
        fields = {
            "authority_id": self._adapter._identity["authority_id"],
            "execution_id": str(self.execution_id),
            "agent_id": str(self._context.agent_id),
            "run_id": str(self.run_id),
            "context_id": str(self._context.id),
            "invocation_id": str(self._context.invocation_id),
        }
        if destination:
            fields.update(destination.model_dump(mode="json"))
        if operation == "search":
            fields["read_scopes"] = [
                item.model_dump(mode="json") for item in self._context.read_scopes
            ]
        if receipt:
            fields.update(episode_id=receipt.episode.id, digest=receipt.episode.digest)
        try:
            sink.record(operation, fields, error=error)
        except Exception:
            # Optional telemetry cannot turn an acknowledged write into a retry.
            pass

    @_boundary
    def exposure(self) -> dict:
        """Export provenance for a trusted harness, never as an authorization proof."""
        self._live()
        ledger = self._ledger()
        authority = self._adapter._identity["authority_id"]
        return {
            "authority_id": authority,
            "read_scopes": [
                {"world_id": item["world_id"], "scope_id": item["scope_id"]}
                for item in ledger["scopes"]
                if item["authority_id"] == authority
            ],
            "complete": not ledger["unknown"]
            and all(item == authority for item in ledger["authorities"]),
        }

    @_boundary
    def mark_unknown_exposure(self) -> dict:
        """Block automatic writes after untracked tool, checkpoint or external input.

        No method clears this marker. Start a fresh invocation after restoring
        the intended audience boundary; explicit selected publication is separate.
        """
        self._live()
        self._record(unknown=True)
        return self.exposure()

    @_boundary
    def search(self, query: str, *, mode: str = "graph", limit: int = 10) -> dict:
        self._live()
        request = SearchRequest(
            scopes=self._context.read_scopes, query=query, mode=mode, limit=limit
        )
        # Record all selected sources before returning even the first byte. A
        # failed request cannot erase previously read evidence on a resumed node.
        self._record(self._context.read_scopes)
        result = self._client.search(request)
        selected = {(item.world_id, str(item.scope_id)) for item in self._context.read_scopes}
        try:
            authority = self._adapter._identity["authority_id"]
            if not isinstance(result, dict) or not isinstance(result["results"], list):
                raise ValueError
            if result.get("mode") != mode or any(
                item.get("authority_id") != authority for item in result["worlds"]
            ):
                raise ValueError
            for item in result["results"]:
                if (item["world_id"], str(UUID(str(item["scope_id"])))) not in selected:
                    raise ValueError
                if item.get("authority_id", authority) != authority:
                    raise ValueError
        except Exception:
            self._record(unknown=True)
            raise WorkflowError("Memory search returned unverified provenance") from None
        self._trace("search")
        return result

    def _operation(self, operation_key: str) -> str:
        return str(self.execution_id) + ":" + _key(operation_key)

    def _deliver(self, operation_key: str, intent: dict) -> Receipt:
        # Exposure writers take the same process-shared guard. A new private
        # read cannot escape between the flow decision and an automatic POST.
        with self._journal.invocation_guard(str(self._context.id)):
            return self._deliver_guarded(operation_key, intent)

    def _deliver_guarded(self, operation_key: str, intent: dict) -> Receipt:
        self._live()
        destination = _selection(intent["destination"])
        if intent["kind"] in {"capture", "automatic-publication"}:
            if destination != self._context.write_scope:
                raise Conflict("Capture destination differs from the persisted context")
            self._flow(destination)
            self._record([destination])
        else:
            context = self._client.get_context(intent["request"]["context_id"])
            if context.write_scope != destination:
                raise Conflict("Publication context differs from its frozen destination")
        saved = self._journal.get("receipt", operation_key)
        # Replaying the original POST rechecks the actual write/publish operation,
        # including when a credential retains read access to an old receipt.
        if intent["kind"] == "capture":
            receipt = self._client.capture(
                destination.world_id, EpisodeInput.model_validate(intent["request"])
            )
        elif intent["kind"] in {"publication", "automatic-publication"}:
            receipt = self._client.publish(
                destination.world_id,
                Publication.model_validate(intent["request"]),
                scope_id=destination.scope_id,
            )
        else:
            raise WorkflowError("Invalid durable memory intent")
        if (
            receipt.episode.world_id != destination.world_id
            or receipt.episode.scope_id != destination.scope_id
            or receipt.episode.source_id != intent["request"]["source_id"]
            or receipt.episode.content != intent["request"]["content"]
        ):
            raise WorkflowError("Memory receipt does not match its durable intent")
        expected = (
            (EpisodeInput if intent["kind"] == "capture" else Publication)
            .model_validate(intent["request"])
            .model_dump(mode="json")
        )
        actual = receipt.episode.model_dump(mode="json", include=set(expected))
        if actual != expected:
            raise WorkflowError("Memory receipt changed the original request fields")
        if saved is not None:
            original = Receipt.model_validate(saved)
            if (
                original.episode.id != receipt.episode.id
                or original.episode.digest != receipt.episode.digest
            ):
                raise Conflict("Acknowledged memory differs from its original receipt")
        result = self._journal.freeze(
            "receipt",
            operation_key,
            {"intent_digest": _digest(intent)},
            lambda: receipt.model_dump(mode="json"),
        )
        receipt = Receipt.model_validate(result)
        self._trace(intent["kind"], destination=destination, receipt=receipt)
        return receipt

    @_boundary
    def capture(
        self,
        operation_key: str,
        content: str,
        *,
        occurred_at: datetime | None = None,
        metadata: dict | None = None,
        project: bool = True,
    ) -> Receipt:
        """Freeze before POST; changed retries fail, omitted timestamps stay frozen."""
        self._live()
        key = self._operation(operation_key)
        parameters = {
            "kind": "capture",
            "content": content,
            "occurred_at": occurred_at.isoformat() if occurred_at else None,
            "metadata": _json(metadata or {}),
            "project": project,
        }

        def prepare():
            request = EpisodeInput(
                scope_id=self._context.write_scope.scope_id,
                context_id=self._context.id,
                source_id="langgraph:" + _digest(key),
                content=content,
                occurred_at=occurred_at or datetime.now(UTC),
                metadata=parameters["metadata"],
                session_id=str(self.execution_id),
                source_entry_id=operation_key,
                project=project,
            )
            return {
                "kind": "capture",
                "destination": self._context.write_scope.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            }

        intent = self._journal.freeze(
            "intent", key, {"parameters_digest": _digest(parameters)}, prepare
        )
        return self._deliver(key, intent)

    @_boundary
    def publish(
        self,
        operation_key: str,
        content: str,
        *,
        occurred_at: datetime | None = None,
        project: bool = True,
    ) -> Receipt:
        """Publish to the configured shared target only when current exposure can flow there."""
        self._live()
        key = self._operation(operation_key)
        parameters = {
            "kind": "automatic-publication",
            "content": content,
            "occurred_at": occurred_at.isoformat() if occurred_at else None,
            "project": project,
        }

        def prepare():
            request = Publication(
                context_id=self._context.id,
                source_id="langgraph:" + _digest(key),
                content=content,
                occurred_at=occurred_at or datetime.now(UTC),
                project=project,
            )
            return {
                "kind": "automatic-publication",
                "destination": self._context.write_scope.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            }

        intent = self._journal.freeze(
            "intent", key, {"parameters_digest": _digest(parameters)}, prepare
        )
        return self._deliver(key, intent)

    @_boundary
    def publish_selected(
        self,
        operation_key: str,
        content: str,
        *,
        destination: ScopeSelection,
        approved: bool = False,
        occurred_at: datetime | None = None,
        project: bool = True,
    ) -> Receipt:
        """Publish only exact user-approved selected text. Never expose approval as an LLM tool."""
        if approved is not True:
            raise Forbidden(
                "Selected publication requires explicit approval of text and destination"
            )
        self._live()
        key = self._operation(operation_key)
        destination = _selection(destination)
        parameters = {
            "kind": "publication",
            "content": content,
            "destination": destination.model_dump(mode="json"),
            "occurred_at": occurred_at.isoformat() if occurred_at else None,
            "project": project,
        }

        def prepare():
            context = self._client.bind_context(
                BindContext(
                    agent_id=self._context.agent_id,
                    # Delegated tokens cannot look up the owner's run registry.
                    # Their execution journal retains the original run mapping.
                    run_id=None if self._adapter._identity["restricted"] else self.run_id,
                    write_scope=destination,
                )
            )
            request = Publication(
                context_id=context.id,
                source_id="langgraph:" + _digest(key),
                content=content,
                occurred_at=occurred_at or datetime.now(UTC),
                project=project,
            )
            return {
                "kind": "publication",
                "destination": parameters["destination"],
                "request": request.model_dump(mode="json"),
            }

        intent = self._journal.freeze(
            "intent", key, {"parameters_digest": _digest(parameters)}, prepare
        )
        return self._deliver(key, intent)

    @_boundary
    def retry(self, operation_key: str) -> Receipt:
        """Deliver the original frozen intent, rechecking current exposure and ACLs."""
        key = self._operation(operation_key)
        intent = self._journal.get("intent", key)
        if intent is None:
            raise WorkflowError("No durable write exists for that operation")
        return self._deliver(key, intent)

    @_boundary
    def absorb_worker(self, worker_key: str, worker: WorkflowMemory) -> dict:
        """Union a trusted live child ledger; serialized tool results cannot prove exposure."""
        self._live()
        if not isinstance(worker, WorkflowMemory):
            self._record(unknown=True)
            raise WorkflowError("Worker handoff requires a trusted live memory runtime")
        try:
            worker._live()
            if (
                worker._adapter._identity["authority_id"] != self._adapter._identity["authority_id"]
                or worker._context.parent_invocation_id != self._context.invocation_id
                or worker.run_id != self.run_id
            ):
                raise WorkflowError("Worker context is not a child of this workflow invocation")
            self._journal.freeze(
                "worker",
                self._operation(worker_key),
                {"context": worker.context.model_dump(mode="json")},
                lambda: {
                    "execution_id": str(worker.execution_id),
                    "context": worker.context.model_dump(mode="json"),
                },
            )
            worker._record([worker._context.write_scope])
            ledger = worker._ledger()
            self._journal.record_exposure(
                str(self._context.id),
                scopes=ledger["scopes"],
                authorities=ledger["authorities"],
                unknown=ledger["unknown"],
            )
        except Exception:
            self._record(unknown=True)
            raise WorkflowError("Worker provenance could not be verified") from None
        return self.exposure()

    @_boundary
    def run_worker(
        self, worker_key: str, worker: WorkflowMemory, execute: Callable[[WorkflowMemory], str]
    ) -> dict:
        """Return selected findings only after a verified, durable provenance handoff."""
        self.absorb_worker(worker_key, worker)
        try:
            result = execute(worker)
            if not isinstance(result, str) or not result.strip() or len(result) > 64_000:
                raise WorkflowError("Workers must return bounded selected text")
        finally:
            self.absorb_worker(worker_key, worker)
        return {
            "result": result,
            "exposure": self.exposure(),
            "context": worker.context.model_dump(mode="json"),
        }
