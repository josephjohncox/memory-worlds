# LangGraph workflows and controlled tracing

[Documentation](README.md) · [Agent clients](agents.md) · [Scopes](scopes.md)

The Python adapter gives LangGraph nodes durable, scoped memory without storing
clients or bearer credentials in graph state. LangGraph keeps its own
checkpointer for execution and resume. Memory Worlds keeps authorized evidence,
Graphiti extraction and knowledge publication. LangSmith receives only the
telemetry explicitly enabled by the application.

## Install and bind an execution

From a checkout, use the pinned toolchain:

```sh
mise install --locked
mise run python:deps
```

For another application, add `memory-worlds[langgraph]` to its dependency file and
lock it with that application's tooling. The optional extra includes the tested
LangGraph runtime, SQLite checkpointer and LangSmith Python SDK.

Use an ordinary or delegated credential. [Discover](discovery.md) accessible
worlds and scopes, register an agent with the client or `identity-create`, and
explicitly provision its write scope. The adapter does not choose a shared world,
grant access or issue worker credentials.

```python
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from langgraph.runtime import Runtime
from memory_worlds.langgraph import MemoryWorldsAdapter, WorkflowMemory
from memory_worlds.models import ScopeSelection

# client is an existing authenticated memory_worlds.client.Client.
adapter = MemoryWorldsAdapter(client, Path(private_state_directory) / "memory")
memory = adapter.execution(
    "research-workflow-42",
    agent_id=UUID(registered_agent_id),
    read_scopes=[ScopeSelection(world_id=world_id, scope_id=read_scope_id)],
    write_scope=ScopeSelection(world_id=world_id, scope_id=private_scope_id),
)


@dataclass(frozen=True)
class Context:
    memory: WorkflowMemory = field(repr=False)


def research_node(state, runtime: Runtime[Context]):
    results = runtime.context.memory.search("fixture procedure")
    # Consume results locally. Store only intentionally selected values in state.
    return {"matches": len(results["results"])}
```

Build the graph with `StateGraph(State, context_schema=Context)` and supply
`context=Context(memory)` on every invocation and resume. Keep the same execution
key and private journal when resuming. A key identifies the workflow's persisted
binding; it grants no authority. If no run UUID is supplied, the adapter registers
one and records the relationship to the agent and server context.

| Knowledge audience or lifetime | Scope |
| --- | --- |
| Company, team or ad-hoc knowledge | World-shared |
| Workers collaborating on one execution | Run-shared |
| A recurring specialist across workflows or harnesses | Agent-persistent |
| One isolated worker invocation | Invocation-private |

The same registered agent UUID can identify a Pi specialist and a LangGraph
worker. Each still needs live grants. A context, thread ID, checkpoint namespace
or shared UUID does not narrow a broad credential or grant a restricted child
access. Use explicit [delegation](reference/scopes.md#create-and-delegate) for the
worker's permission ceiling, and pass the token through a private runtime channel.

A delegated worker attaches its already issued context instead of trying to
register a new run or invent an invocation:

```python
from memory_worlds.client import Client
from memory_worlds.models import DelegateCredential, ScopeSelection

delegated = client.delegate(
    DelegateCredential(
        private_world_id=world_id,
        parent_context_id=memory.context.id,
        agent_id=UUID(worker_agent_id),
        run_id=memory.run_id,
        grants=selected_worker_grants,
    )
)
try:
    with Client(service_url, delegated.token) as worker_client:
        worker = MemoryWorldsAdapter(worker_client, private_worker_journal).execution(
            "worker-1",
            agent_id=delegated.context.agent_id,
            run_id=memory.run_id,
            context_id=delegated.context.id,
            read_scopes=delegated.context.read_scopes,
            write_scope=ScopeSelection(
                world_id=delegated.private_scope.world_id,
                scope_id=delegated.private_scope.id,
            ),
        )
        handoff = memory.run_worker("worker-1", worker, execute_worker)
finally:
    client.request("DELETE", f"/v1/credentials/{delegated.credential_id}")
```

Here `execute_worker(worker)` is a trusted callback returning bounded selected
text. `run_worker` records the child's declared private audience before it starts
and merges its verified exposure again before findings return, including on
failure. Keep `delegated.token`, the clients and the live worker handle out of
messages, graph state, interrupts and traces. A resumed worker must use its
original private journal and credential lifecycle; a new credential requires a
new journal binding.

## Capture and retry

```python
receipt = memory.capture(
    "conclusion-1",
    "The selected fixture requires a ceramic bearing.",
    metadata={"evidence": "bench-test-42"},
)
```

An operation key identifies one immutable write intent within the execution.
The adapter durably freezes the complete request before sending it: timestamp,
scope, context, text, metadata and source ID. If an acknowledgement is lost, retry
the same operation with the same arguments. It resends the original request or
returns the acknowledged receipt after live authorization checks. Changing the
text, destination, metadata or explicit timestamp under the same key fails.

Use `capture` for a private context destination. Use
`memory.publish("finding-1", selected_text)` when the execution was explicitly
bound to a shared write destination. `publish` retains the same fixed target and
strict exposure checks; it cannot declassify private worker findings. Broader
publication uses the separately approved `publish_selected` action below.

Search records the selected source scopes in a durable, monotonic exposure
ledger. Automatic capture checks the current audience flow from every recorded
scope into the fixed write destination. Unknown provenance, foreign authorities
or a denied flow block automatic capture. An empty search still records the
selected scopes conservatively. A classifier or a model cannot reset exposure or
authorize broader publication.

The ledger covers adapter reads and explicit trusted worker handoffs. It cannot
infer the source of arbitrary tool results, graph inputs or pre-existing
checkpoints. Call `memory.mark_unknown_exposure()` before consuming content whose
audience cannot be verified. That marker is sticky and blocks automatic writes;
there is no reset method. Keep the original exposure journal with its checkpoints,
or start a fresh invocation instead of claiming that old state has no exposure.

Keep each adapter journal private and outside graph state. It contains frozen
source text and receipts, bound to the service URL, authority and credential ID.
It is separate from both the authority's source journal and the LangGraph
checkpointer. Back up or delete these copies according to their own retention
requirements. Each journal record is limited to 1 MiB; a write whose complete
intent exceeds that bound is rejected before submission to the service.

## Run the interrupt/resume example

[The runnable example](../examples/langgraph/scoped_workflow.py) recalls source
evidence, captures a private draft and pauses before publishing selected text.
It runs without a model provider. Supply an existing registered agent, readable
scope, private write scope and authorized publication destination:

```sh
mise run langgraph:example -- \
  --url http://127.0.0.1:8787 \
  --token-file /absolute/private/agent.token \
  --state-dir /absolute/private/research-workflow \
  --execution-key research-workflow-42 \
  --agent-id AGENT_UUID \
  --world-id PRIVATE_WORLD_ID \
  --read-scope-id READ_SCOPE_UUID \
  --write-scope-id PRIVATE_SCOPE_UUID \
  --publish-world-id TEAM_WORLD_ID \
  --publish-scope-id TEAM_SCOPE_UUID \
  --content-file /absolute/private/selected-finding.txt
```

Replace the uppercase placeholders with discovered canonical IDs. The token and
content files must be private to the current user. Add `--no-project` for a
source-only exercise. The example prints IDs, counts and approval status; it does
not print private evidence.

Review the selected file and destination yourself. Rerun the exact command with
`--decision approve` to publish, or `--decision reject` to decline. The SQLite
checkpointer and adapter journal survive process exit. A digest prevents a changed
file or destination from being approved under the original pending proposal.
Use a new execution key for a different proposal.

Existing checkpoint files and SQLite journal/WAL/shared-memory sidecars must be
owned private regular files with one hard link. The example rejects symlinks,
shared modes, FIFOs and files larger than 64 MiB before opening the saver.

The application calls `publish_selected(..., approved=True)` only after that
specific approval. Exact `True` is required; a truthy string or model-produced
approval is insufficient. This assertion is an application consent boundary.
The HTTP service separately checks current publication permission. Do not expose
the approval flag as an unrestricted model tool.

The example's normal state contains IDs, a proposal digest and counts. Its
interrupt payload deliberately includes the selected text and destination so an
approver can inspect the proposal. LangGraph persists that payload. Arbitrary
retrieved text copied into state is also checkpointed; Memory Worlds does not
apply retention or access policy to those independent copies.

## Trace IDs without uploading memory bodies

Use the invocation helper for the complete graph run, including resumed runs:

```python
from memory_worlds.langgraph_tracing import invoke_workflow

result = invoke_workflow(
    graph,
    inputs,
    context=Context(memory),
    config={"configurable": {"thread_id": "research-workflow-42"}},
)
```

`invoke_workflow` and `ainvoke_workflow` disable ambient LangSmith graph/node
tracing around the complete invocation. This prevents a globally enabled tracer
from uploading the graph's state independently of the memory tracing policy.
The helper does not suppress application callbacks, logging or checkpoint writes.

The adapter has no tracing sink by default. Enable a separate, controlled sink:

```python
from memory_worlds.langgraph_tracing import LangSmithTraceSink, TracePolicy

sink = LangSmithTraceSink(
    endpoint="https://api.smith.langchain.com",
    api_key=private_langsmith_api_key,
    project_name="memory-workflows",
    policy=TracePolicy("ids-only"),
)
adapter = MemoryWorldsAdapter(client, private_journal_path, trace_sink=sink)
```

| Policy | Exported content |
| --- | --- |
| `off` (default) | No memory spans |
| `ids-only` | Allowed execution, agent, run, context, invocation, authority, scope and receipt correlation fields |
| `selected-content` | The same IDs plus separately supplied selected text |

The sink drops arbitrary metadata, queries, credentials, operation keys and
exception bodies in IDs-only mode. Errors use a fixed message. It does not copy
ambient tags, metadata or trace replicas. For distributed nesting, supply an
explicit `parent_headers` mapping; only the validated `langsmith-trace` parent is
accepted. Baggage and authorization headers are discarded, and the explicitly
configured client/project remains the destination.

The sink owns a dedicated SDK client. It disables runtime/environment metadata,
ambient tracing transports, proxy inheritance and redirects; it does not mutate
another application's client. Close the sink after use or use it as a context
manager. LangSmith credentials stay in this runtime object, outside graph state.

Selected-content mode is a separate disclosure choice. It does not authorize
publication into a Memory Worlds scope. Conversely, a scope-flow check or a chat
sharing classifier does not authorize trace uploads.

The adapter supplies IDs to its sink. To trace a separately selected excerpt,
explicitly call a sink configured with `TracePolicy("selected-content")`:

```python
sink.record(
    "approved_excerpt",
    {"execution_id": str(memory.execution_id)},
    selected_content={"content": user_selected_excerpt},
)
```

Enabling that policy does not automatically upload queries, search results or
captured bodies.

## Pi and worker composition

Pi sessions can use the same Memory Worlds service and authorized scopes as the
workflow. The official [LangSmith Pi extension](https://github.com/langchain-ai/langsmith-pi-extension)
traces Pi; it does not execute LangGraph. Isolated Pi specialists exclude ambient
extensions, so their tracing must be explicitly supplied by the trusted harness.

When a trusted controller returns worker findings to a parent, it must merge the
worker's exposure before releasing the content. The adapter's worker handoff
accepts a live `WorkflowMemory` handle and verifies its parent/run relationship;
serialized model output is not proof of provenance. The Pi bridge similarly
requires an explicit trusted harness handoff rather than guessing provenance from
ordinary tool results. Worker invocation-private data remains private until an
explicit authorized action selects it for broader publication.

## Validate

```sh
mise run langgraph:test
```

The integration tests use the actual LangGraph runtime and SQLite checkpointer
with an isolated HTTP authority. They cover durable lifecycle, lost acknowledgments,
interrupt/resume, exposure and controlled LangSmith serialization. They do not
use or modify your running daemon, Pi installation or real conversations.

The separation follows upstream [runtime context](https://docs.langchain.com/oss/python/concepts/context),
[persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
[interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) and
[tracing context](https://reference.langchain.com/python/langsmith/run_helpers/tracing_context).
