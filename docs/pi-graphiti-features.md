# Pi Graphiti feature coverage

[Documentation](README.md) · [Audit and evidence](reference/pi-graphiti-audit.md)

Memory Worlds provides native Pi graph tools over an authenticated multi-agent
service. Graphiti extracts entities and relationship facts in each scope's
isolated namespace. SQLite or PostgreSQL journals retain source evidence,
permissions and receipts; graph failures do not discard source.

The native extension connects directly to the service. It needs neither MCP nor
an exposed graph database port. Start with the [Pi quickstart](../packages/pi/README.md).

## Graph actions

| Action | Memory Worlds behavior |
| --- | --- |
| `graph add` | Stores a source and requests extraction into the configured private target |
| `graph search` | Finds authorized entities and facts with scope and source provenance |
| `graph episodes` | Lists recent source records, including those awaiting extraction |
| `project` / `global` / `both` | Explicit graph scope bindings; `both` combines authorized reads |
| `/graph ingest PATH` | Imports one selected UTF-8 file up to 200,000 bytes as one episode |
| `/graph curate [NUMBER]` | Previews proposals, then saves one explicitly selected private proposal |

Bind graph aliases deliberately. For example, `global` can read a world's shared
scope while `project` binds a private persistent scope. Neither alias grants
company-wide access. `graph add` requires its alias to match the private write
target; it cannot turn a shared read alias into a broader publication.

See the [graph command reference](reference/pi.md#graph-actions) for arguments
and defaults. Directory imports use the separate [chat import workflow](chat-import.md).

## Feature comparison

The reference package is [`pi-graphiti`](https://www.npmjs.com/package/pi-graphiti).
The comparison covers upstream **0.6.0**, audited on **October 20, 2026**, and
Memory Worlds **0.3.0**. It is a dated audit, not a claim about future upstream
releases.

| Capability | Memory Worlds |
| --- | --- |
| Add, search and list graph episodes | Native `graph` tool with the same action names and explicit authorized bindings |
| Pre-turn recall and automatic capture | Separate opt-in controls; capture starts disabled |
| Curation and correction review | Bounded proposals; saving requires explicit selection |
| Offline capture | Optional private disk spool, pinned to service, authority, credential and target |
| Multi-agent access | Delegated credentials, private invocation notes and selected shared scopes |
| Team/company memory | Organizational worlds, OIDC membership and explicit publication |
| Remote shared memory | Authorized mounts, attributed retrieval and durable publication receipts |
| Recovery and deployment | Scope archives plus operator-managed Compose, kind and Helm workflows |

See the [full comparison with source/test evidence](reference/pi-graphiti-audit.md#feature-comparison).
The [audit record](reference/pi-graphiti-audit.md#audited-upstream) retains versions,
immutable source revisions, archive hash and the observed upstream tool surface.

The packages do not have interchangeable configuration, raw graph group IDs or
archive formats. Running both extensions with the same `graph` tool name is
unsupported. Memory Worlds does not provide `/graph setup`, `uninstall` or
`teardown`, and its single-file ingestion does not implement paragraph/sentence
chunking. Use [guided setup](setup.md) and [deployment guides](deployments.md)
for service administration.

## Scope mapping

Read selection and write destination are independent. World-shared, run-shared,
agent-persistent and invocation-private scopes have explicit grants. UUIDs and
names provide attribution; credentials authorize access.

Parents receive selected child results. Reading the child's complete private
notes requires a separate grant. A fresh SDK session receives only its brief and
selected evidence; it inherits no parent transcript, ambient tools or broad
credential. This is an SDK boundary, not an operating-system process sandbox.

See [scopes and delegation](scopes.md) and the
[native subagent SDK](reference/pi.md#native-subagents-and-review).

## Graph extraction boundary

Extraction, entity resolution and rebuilds stay inside one authority/world/scope
namespace. Authorized retrieval combines results afterward, preserving their
provenance. Filtering after cross-scope extraction would already be too late to
protect private input from shared summaries.

See [the extraction contract](reference/pi-graphiti-audit.md#graph-extraction-boundary)
and [Graphiti operations](graphiti.md).

## Curation and prompt exposure

Automatic capture checks that the write target's current readers can read all
memory already exposed to the session. Exposure survives branches, forks and
compaction. Unknown or foreign-authority exposure stops capture. Curation and
correction hooks produce proposals rather than publishing transcripts.

[Chat sharing](chat-sharing.md) can publish selected excerpts only under a rule
enabled by the user for that chat. The classifier cannot choose its destination
or widen credentials. See [exposure details](reference/pi-graphiti-audit.md#curation-and-prompt-exposure).

## Python, CLI, and MCP scope access

The same service boundary applies to Python, CLI, native Pi and MCP. Ordinary
users discover their accessible worlds without administrator tokens. Restricted
children discover only their delegated audience. See [agent clients](agents.md),
[discovery](discovery.md) and the [complete scope surface](reference/pi-graphiti-audit.md#python-cli-and-mcp-scope-access).
