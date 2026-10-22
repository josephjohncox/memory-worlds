# Pi Graphiti audit and feature evidence

[Documentation](../README.md) · [Guide](../pi-graphiti-features.md)

Memory Worlds uses the real Graphiti library for graph extraction and graph
retrieval. Its SQLite/PostgreSQL journals own durable source evidence, credentials, scope
grants, and delivery receipts. Source search remains available without a graph
backend. A source-only capture is not silently sent to an extraction model.

The local FalkorDB adapter and experimental Neptune adapter both use
`graphiti-core==0.31.2`. The native Pi extension calls the authenticated Memory
Worlds API; it does not require an MCP bridge or expose graph database ports.
The optional MCP adapter is another client of the same authorization boundary.
See [deployment configuration](../deployments.md) and [storage and recovery](../storage.md).

## Audited upstream

The reference package is [`pi-graphiti`](https://www.npmjs.com/package/pi-graphiti)
from [`p2c2e/pi-graphiti`](https://github.com/p2c2e/pi-graphiti).
This audit was performed on October 20, 2026 using
the published npm archive and its corresponding public GitHub source.

| Evidence | Value |
| --- | --- |
| Latest npm version observed | `0.6.0` |
| npm publication time | `2026-08-09T09:21:38.344Z` |
| npm `gitHead` | `fd82b29f7157bac9190546e3b4a6abda223f0f81` |
| GitHub `main` observed | `8fbfbe13c2f761af4bf29401a371e4080c0f18a9` |
| License | MIT, copyright 2026 Sudharsan Rangarajan |
| npm tarball SHA-256 | `679b2b6ffc54cf407ccb5acb7382e0e390071826991abc91f9a582bfff2f3c87` |

All 16 `src/*.ts` files in the published package match the observed GitHub
revision byte for byte. Source references below use that immutable revision.
The npm metadata is available from the [official registry](https://registry.npmjs.org/pi-graphiti/0.6.0).
No upstream code has been copied into Memory Worlds by this audit.

## Exact upstream tool surface

The upstream [tool definition](https://github.com/p2c2e/pi-graphiti/blob/8fbfbe13c2f761af4bf29401a371e4080c0f18a9/src/tool.ts)
registers one tool named `graph`:

| Argument | Allowed values or type | Behavior |
| --- | --- | --- |
| `action` | `add`, `search`, `episodes` | Required |
| `content` | String | Required by `add` |
| `name` | String | Optional episode label |
| `source` | `text`, `message`, `json` | Defaults to `text` |
| `query` | String | Required by `search` |
| `limit` | Number | Defaults to 5; floored and clamped to 1–20 |
| `scope` | `project`, `global`, `both` | `add`/`episodes` default to `project`; `search` defaults to `both` |

`search` queries both entity nodes and relationship facts. `episodes` returns
recent episode labels/content. IDs are retained in tool result details even
when omitted from compact model-facing text. `add` acknowledges an episode
before asynchronous extraction finishes.

Upstream maps `project` and `global` to client-selected Graphiti `group_id`
values. This is a partition convention, not a server-enforced permission model.
Memory Worlds maps tool aliases to explicitly authorized world
and scope IDs. A directory basename, username, alias, or subagent UUID cannot
grant access by itself.

## Feature comparison

This table describes the native Memory Worlds 0.3.0 graph features. They use
server authorization and explicit publication. Transport and administration
contracts are described separately.

| Capability in upstream 0.6.0 | Memory Worlds 0.3.0 | Verification |
| --- | --- | --- |
| `graph add` | Same action and content/name/source inputs; writes only to the configured agent-persistent, thread-private or invocation-private target, and explicitly requests Graphiti extraction | [Tool and alias tests](../../packages/pi/test/scoping.test.mjs), [actual-service Pi smoke](../../packages/pi/test/real-service-smoke.mjs) |
| `graph search` | Scoped entity and relationship fact retrieval with IDs, source evidence, and projection provenance | [Graph tool tests](../../packages/pi/test/scoping.test.mjs), [real graph tests](../../tests/test_graph.py) |
| `graph episodes` | Recent authorized source episodes, including evidence awaiting extraction | [Scope client tests](../../tests/test_scope_clients.py), [actual-service Pi smoke](../../packages/pi/test/real-service-smoke.mjs) |
| Project/global search | Explicit alias bindings; `both` reads their authorized union. Search defaults to `both`; add/episodes default to `project`, unless the user selected an override | [Alias tests](../../packages/pi/test/scoping.test.mjs) |
| Ambient recall | Opt-in source or graph recall before a user turn, with scope exposure recorded before model delivery | [Extension lifecycle tests](../../packages/pi/test/extension.test.mjs) |
| Automatic capture cadence | Opt-in capture after completed agent responses; stable source IDs and per-entry receipts rather than periodic raw snapshots | [Extension tests](../../packages/pi/test/extension.test.mjs) |
| Pre-compaction and shutdown capture | Pre-compaction/turn work has a five-second total budget. With a configured spool, shutdown queues locally without network calls, under a two-second local budget | [Lifecycle and queue tests](../../packages/pi/test/extension.test.mjs) |
| LLM curation | `reviewMemories` accepts a host-supplied provider; `reviewWithPiSubagent` runs an isolated, tool-less Pi reviewer and validates referenced proposals. `/graph curate [NUMBER]` previews and explicitly saves a selected private proposal | [Curation and isolated factory tests](../../packages/pi/test/scoping.test.mjs) |
| Correction-triggered review | Correction/preference hints are enabled by default. Optional `reviewCorrections` uses a supplied review callback, at most once per three user turns. It produces proposals without saving or publication | [Curation implementation](../../packages/pi/src/curation.ts), [extension hooks](../../packages/pi/src/extension.ts) |
| Offline local spool | Opt-in `spoolDirectory`; private immutable envelopes pinned to service, authority, credential, target, source, and exposure guard. Defaults: 10,000 envelopes and 64 MiB aggregate capacity; no automatic age-based deletion | [Queue tests](../../packages/pi/test/spool.test.mjs) |
| Document ingestion | `/graph ingest PATH` accepts one user-selected UTF-8 text file up to 200,000 bytes as one episode, with a stable retry identity. Paragraph/sentence chunking is not implemented | [Native command implementation](../../packages/pi/src/extension.ts) |
| Extracted chat directories | Native preview/import commands reuse the credential-free Python planner, preserve ChatGPT/Claude message provenance and split oversized document snapshots losslessly into private batches | [Planner bridge](../../packages/pi/src/imports.ts), [actual service smoke](../../packages/pi/test/real-service-smoke.mjs) |
| Export and restore | Scope-aware `.mworld` source/audit/dependency archives and explicit imports; the CLI rejects restoring a private archive into a broader scope kind | [Scoped projection/portability tests](../../tests/test_scope_graph.py), [adversarial review](../../tests/test_scope_adversarial_review.py) |
| Docker setup and teardown | Compose, kind, and Helm are service operator workflows; `/graph` does not provide `setup`, `uninstall`, or `teardown` | [Deployment guide](../deployments.md), [Helm guide](../helm.md) |
| Retraction and erasure | Scoped manager operations block recall and retain tombstones; operator-only drained namespace purge and bounded retention keep durable receipts | [Lifecycle](../lifecycle.md) |

Memory Worlds already adds multi-agent authenticated service access, SSH
tunnels, organizational worlds, explicit publications, OIDC memberships,
federated routing, conservative graph recovery, and Kubernetes persistence.
Ordinary OIDC users and delegated agents can discover their readable worlds,
scopes and authorized mounts through the native `memory_discover` tool,
`/memory discover`, typed clients and MCP. Discovery leaves read selections and
capture settings unchanged; see [user discovery](../discovery.md).
Its native [chat sharing modes](../chat-sharing.md) also support classifier-assisted
review and selected automatic publication under a user-enabled per-chat rule.
These do not by themselves establish complete upstream feature parity.

Memory Worlds is not a drop-in replacement for every upstream command,
configuration key, or MCP response shape. Upstream Markdown dump/load files and
raw group IDs are not an interchangeable archive format. The native graph feature contract uses explicit scope bindings and typed tool
actions; there is no separate emulation layer. Running both
extensions with the same `graph` tool name is unsupported; disable one tool
registration.

## Scope mapping

A world owns its membership and organizational boundary. Its scope IDs identify
audiences, with the following native scope kinds:

| Kind | Identity and intended audience |
| --- | --- |
| `world-shared` | Participants explicitly granted access to shared memory in that world |
| `run-shared` | Participants delegated into a particular run UUID |
| `agent-persistent` | A stable agent UUID and any explicitly granted readers |
| `thread-private` | One durable thread UUID and its explicitly granted readers |
| `invocation-private` | A particular invocation/subagent UUID and any explicitly granted readers |

UUIDs distinguish agents, runs, and invocations; possession of a UUID is not a
capability. A child credential has its own identity, a parent delegation link,
scope grants, expiry, and delegation limit. It cannot expand the parent's
delegable access. Revoking the parent invalidates its delegated descendants.

The upstream aliases `global` and `project` resolve to deliberately configured
scope IDs. A common read mapping uses world-shared for `global` and run-shared
for `project`; an agent-persistent alias can instead support private cross-project
notes. Neither alias implies access to every world or company. There is no
automatic fallback from an absent project mapping to a broader shared write
target. `both` is a read selector, not a write scope.
An alias pointing at `world-shared` or `run-shared` is not an automatic capture
destination: sharing requires the explicit publication operation. The graph tool cannot turn `graph add` into broader publication.

An invocation can read shared scopes while writing to its private scope.
Publishing selected conclusions creates a new attributed destination episode
and receipt. It does not relabel the private original. Parents receive returned
results; inspecting a child's entire scratch space requires a separate grant.

## Graph extraction boundary

Graph namespaces include authority, world, scope, and projection generation.
The derived namespace is generated by the service, not accepted from a tool
argument. Previous episodes supplied to Graphiti extraction come from the same
namespace. Entity resolution, summaries, relation extraction, graph retrieval,
and graph rebuilds remain within that boundary.

Filtering search results after extraction is insufficient: a private episode
could already have affected a shared summary or relationship. Cross-scope
retrieval searches authorized namespaces independently and combines attributed
results. Explicit publication is independently extracted at its destination.

The same boundary applies to direct episode lookup, recent episodes, source
search, exports, imports, router deliveries, retries, and synchronization. A
request containing an unauthorized scope fails rather than silently returning
an incomplete authorized subset.

## Curation and prompt exposure

The upstream [curation process](https://github.com/p2c2e/pi-graphiti/blob/8fbfbe13c2f761af4bf29401a371e4080c0f18a9/src/review.ts)
launches another Pi process. Memory Worlds must not treat process spawning as
credential isolation. A curator either receives a narrowly delegated credential
bound to its invocation scope, or returns structured proposals to its caller
without a write tool. It cannot inherit the user's broad token or choose a
wider audience from conversation text.

Storage grants cannot remove information already included in a child's prompt.
The caller constructs that prompt from an explicit task brief and selected
context. Scope exposure survives branches, forks, compaction, and resumption.
Reading private context must stop automatic capture into an audience that is
broader than the exposed data's audience.

The service's `POST /v1/scopes/flow` endpoint checks current reader inclusion.
Every live credential able to read the capture destination must also be able to
read each exposed source scope. This permits run-shared context to feed private
invocation notes without treating all shared/private kind labels as an access
hierarchy. The response contains only `allowed`; reader identities and counts
are not disclosed. Large candidate audiences conservatively fail the bounded
check. The decision concerns current grants, not future sharing decisions.

Pi checks this condition before automatic capture and the disk queue checks it
again before dispatch. Foreign-authority or unknown exposure fails closed.
Forking creates a fresh context invocation while retaining the stable agent
UUID and inherited exposure. Credential rotation stops inherited automatic
capture until the user explicitly enables future entries. Delegation and queue
draining snapshot credentials for one operation, preventing token-file rotation
from moving private data to a different credential mid-operation.

Retrieved text remains evidence rather than instructions. Curation, correction
nudges, and proactive saves use the same write boundary as explicit tool calls.
Completing a task never automatically publishes a private transcript.

## Python, CLI, and MCP scope access

The Python client exposes typed `scopes`, `get_scope`, `create_scope`,
`grant_scope`, `bind_context`, `get_context`, and `delegate` operations.
Capture, episode lookup/listing, status, and publication accept an explicit
`scope_id`. `SearchRequest.scopes` selects several world/scope pairs, while
world-level requests select the world-shared scope. A linked publication
records its source scope and source episode without changing the original.

The CLI supports:

```text
memory-worlds scopes WORLD
memory-worlds scope WORLD SCOPE_UUID
memory-worlds scope-create WORLD --input scope.json
memory-worlds scope-grant WORLD SCOPE_UUID --input grant.json
memory-worlds scope-flow --input flow.json
memory-worlds context-bind --input context.json
memory-worlds context CONTEXT_UUID
memory-worlds agent-delegate --input child.json --token-output child.token
memory-worlds search QUERY --select WORLD:SCOPE_UUID --select WORLD:SCOPE_UUID
memory-worlds search QUERY --world WORLD --scope SCOPE_UUID --entities
```

`capture`, `publish`, `status`, `graph-rebuild`, `export`, `restore`,
`import-chatgpt`, and `import-pi` also accept `--scope SCOPE_UUID`. Native database
backup is an encrypted administrator operation covering the whole authority,
including private scopes, credentials and jobs. See [recovery](../recovery.md).

`capture` and `publish` accept `--context CONTEXT_UUID` for a live, caller-owned
context whose write destination matches the selected target. Stored agent and
invocation UUIDs are assigned by the service; source input cannot forge them.
A context records selected reads and a write destination. Issuing an attenuated
credential through delegation establishes the permission ceiling; binding a new
context does not narrow an existing broad credential by itself.

Structured command input is a bounded JSON file validated against the matching
request model. Delegation reserves a new file with mode `0600` before issuing a
credential, refuses an existing destination, and writes the one-time token only
to that file. Standard output contains the credential ID, context, private scope,
expiry, and token-file path. It contains neither the parent nor child token.

The MCP facade exposes scope listing/status, recent scoped episodes, context
binding/inspection, and scoped capture/search/publication. It exposes no token
issuance, delegation, scope grants, or database administration tools. A host that
needs delegated subprocess credentials uses the typed client or CLI through a
private channel instead of placing bearer credentials in model-visible output.
