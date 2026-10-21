# Memory Worlds for Pi

Native Pi tools, commands, evidence rendering and session hooks over the Memory
Worlds HTTP service. The extension uses a direct typed client; it does not need
MCP or access to the graph database. Pi's extension API supplies the integration
points without a fork.

Requires Node **22.19.0 or later**, and Pi **1.3.0** from the current
`@earendil-works/pi-coding-agent` package. The former `@mariozechner` package is
deprecated. Dependencies are pinned in `package-lock.json`; TypeScript 7.0.2
and TypeBox 1.4.2 were verified against the public npm registry on 2026-10-20.

## Install from this checkout

```sh
cd packages/pi
npm ci
npm run build
pi install "$(pwd)"
```

For one invocation, use `pi --extension ./dist/extension.js`. To package the
extension, run `npm pack`; publication remains an owner operation. The npm
archive includes only compiled extension/client files and this README.
The package is licensed under Apache-2.0; the archive includes `LICENSE`.

## Configure

Create `~/.pi/agent/memory-worlds.json`:

```json
{
  "url": "http://127.0.0.1:8787",
  "tokenFile": "~/.config/memory-worlds/pi.token",
  "readWorldIds": ["PERSONAL_WORLD_ID"],
  "readMountIds": [],
  "writeWorldId": "PERSONAL_WORLD_ID",
  "automaticCapture": false,
  "retrieval": {
    "enabled": false,
    "mode": "source",
    "limit": 5,
    "maxChars": 8000
  }
}
```

Use an agent credential issued by the daemon, stored in a private regular file
(`chmod 600`). Tokens are read from the file for every request, so credential
rotation takes effect without saving credentials in session entries. The config
rejects plaintext token fields. Environment overrides are `MEMORY_WORLDS_URL`,
`MEMORY_WORLDS_TOKEN_FILE`, and `MEMORY_WORLDS_CONFIG` (config file path).

Loopback HTTP supports the same SSH forwards as other clients. Use HTTPS for
other hosts. URL credentials, queries, fragments and redirects are rejected.
The extension does not read project-local configuration or give models access
to the token file through a memory tool.

## Commands and native tools

`/memory` opens a selector in interactive Pi. In RPC or print mode it shows help.
The persistent footer and widget show connectivity, capture policy, selected
worlds/mounts and pending delivery count. Expand retrieved evidence messages or
tool results to inspect authority/world/source attribution.

| Command | Behavior |
| --- | --- |
| `/memory status` | Authorized worlds, current selections and delivery count |
| `/memory worlds` | List local worlds |
| `/memory read WORLD_A,WORLD_B` | Set local read selection; `none` clears it |
| `/memory mounts` | List configured remote mounts |
| `/memory mount TEAM,COMPANY` | Set remote read selection; `none` clears it |
| `/memory target WORLD` | Select an authorized private target; `off` clears it |
| `/memory capture on` | Capture future user/assistant text in the private target |
| `/memory capture off` | Stop automatic capture |
| `/memory capture SELECTED TEXT` | Explicit private note; preserves line breaks |
| `/memory search QUERY` | Search durable source evidence |
| `/memory graph QUERY` | Request graph retrieval explicitly |
| `/memory publish MOUNT WORLD SELECTED TEXT` | Publish only selected text via the durable router outbox |
| `/memory deliveries` | Inspect this caller's publication delivery state |

The model receives `memory_search`, `memory_capture`, `memory_status` and the
pi-graphiti-compatible `graph` tool.
Publication is a user command: no model tool or automatic hook publishes data.
Reading worlds and mounts never changes the private write target. An explicit
private note remains available after mixed-authority retrieval.
Repeating the same publication text to the same destination in one session reuses
its durable request identity, including after a lost acknowledgment. Session
metadata retains its content hash and request identity, without copying the text.

## Capture and session boundaries

Capture starts **off** unless `automaticCapture: true` and an explicit private
`writeWorldId` or `writeScope` are configured. Legacy world targets must be
`personal` or `private-work`; scoped targets must be `agent-persistent` or
`invocation-private` within an authorized world. Source capture requests graph extraction only when `projectCaptured: true` is
configured. The explicit `graph` add operation requests extraction (`project: true`);
the daemon must have a graph backend and model providers configured.

Automatic capture records only future user/assistant text on the active branch.
Source identity includes the original Pi session and entry IDs; timestamps,
roles and parent entry IDs are stable on retries. Successful receipts are saved
as extension entries. Offline failures retry on the next completed turn or before compaction; a lost
acknowledgment replays the same service receipt. Each turn drains at most 100
backlogged entries. Messages larger than the service limit require an explicit
excerpt; images, thinking blocks and tool output are not captured automatically.

Read selection follows the active branch. Authority exposure is the union over
the complete session tree and parent sessions, so branching, compaction,
unmounting and reload cannot clear exposure. Automatic capture is disabled once
another authority has supplied evidence, or when parent exposure cannot be
verified. A clean session is required to enable it again. The rule protects a
workflow boundary; it cannot prove that selected text contains no private facts.
Forks without a persisted parent file keep known exposure and mark the missing
history as unknown. That state survives reload and keeps automatic capture off.

Pre-turn retrieval is separately opt-in with `retrieval.enabled`. It requests at
most 20 records and renders at most 32,000 characters (defaults: 5 and 8,000).
Evidence enters a labeled custom message. The extension appends a short scope and
publication policy to the existing system prompt.
Source and graph modes remain distinct; unavailable services produce explicit
partial results, without pretending source matches are semantic results.
Requests time out after five seconds. Capture flushes use a five-second total
budget. Configured filesystem spooling makes shutdown local and bounded; retrieval
requests cancel on shutdown/reload.

## Verify

```sh
npm test
npm run smoke
npm run smoke:service
npm pack --dry-run
```

Tests cover authenticated HTTP, cancellation, response bounds, config validation,
stable capture retries, provenance, branch/parent exposure, explicit publication,
and UI cleanup. The smoke launches the actual Pi 1.3.0 RPC executable against an
ephemeral authenticated fixture service; it exercises package loading, commands,
widgets and native capture without provider credentials. It requires permission
to bind an ephemeral loopback port. It does not measure live-model retrieval.
`smoke:service` launches a real Python daemon as well as Pi and verifies capture,
source retrieval, `/v1/me` and provenance through the actual API and SQLite store.
It defaults to the checkout's `.venv/bin/python`; set
`MEMORY_WORLDS_SMOKE_PYTHON=/absolute/path/to/python` for another installed Python
environment containing Memory Worlds and uvicorn.

The reusable direct SDK is exported as `memory-worlds-pi/client`:

```ts
import { MemoryWorldsClient, readTokenFile } from "memory-worlds-pi/client";

const client = new MemoryWorldsClient(
  "http://127.0.0.1:8787",
  () => readTokenFile("/absolute/private/path/pi.token"),
);
const worlds = await client.worlds();
```

Upstream contracts: [native extensions](https://pi.dev/docs/latest/extensions),
[Pi packages](https://pi.dev/docs/latest/packages),
[Pi source](https://github.com/earendil-works/pi),
[current Pi registry package](https://registry.npmjs.org/@earendil-works/pi-coding-agent/latest).


## Scopes and invocation UUIDs

Version **0.2.0** adds four independently authorized scopes within each world:
`world-shared`, `run-shared`, `agent-persistent`, and `invocation-private`.
A scope has a UUID; runs, stable agent identities, and invocations have separate
UUIDs. A Pi fork creates a fresh context while retaining the stable agent identity.
Ordinary credentials receive a fresh invocation UUID; restricted child credentials
retain the invocation UUID fixed by delegation. A new subagent receives its own
restricted credential and private scope.
World membership never grants implicit access to another invocation's private notes.

Explicit scope configuration can replace the legacy world selections:

```json
{
  "url": "http://127.0.0.1:8787",
  "tokenFile": "~/.config/memory-worlds/pi.token",
  "readScopes": [
    {"world_id": "TEAM_WORLD_ID", "scope_id": "00000000-0000-4000-8000-000000000001"}
  ],
  "writeScope": {
    "world_id": "TEAM_WORLD_ID",
    "scope_id": "00000000-0000-4000-8000-000000000002"
  },
  "automaticCapture": false,
  "projectCaptured": true,
  "spoolDirectory": "~/.config/memory-worlds/pi-spool",
  "graphAliases": {
    "project": {"world_id": "TEAM_WORLD_ID", "scope_id": "00000000-0000-4000-8000-000000000002"},
    "global": {"world_id": "PERSONAL_WORLD_ID", "scope_id": "00000000-0000-4000-8000-000000000003"}
  }
}
```

Replace every example ID with an authorized world/scope. `project` and `global`
are explicit aliases. The extension never derives authority from a directory,
repository path, machine name, or arbitrary Graphiti group ID.

| Command | Behavior |
| --- | --- |
| `/memory scopes WORLD` | List scopes this credential can access |
| `/memory scope-read WORLD SCOPE` | Add an explicit readable scope; `none` clears scoped reads |
| `/memory target WORLD SCOPE` | Set a private agent/invocation capture target |
| `/memory alias project WORLD SCOPE` | Bind the project alias; `global` binds the other alias |
| `/memory share WORLD SCOPE TEXT` | Explicit selected publication into another scope |
| `/memory spool status` | Inspect the configured local queue |
| `/memory spool drain` | Dispatch envelopes pinned to the current credential and authority |
| `/graph project`, `/graph global`, `/graph both` | Select alias reads independently of the private write target |
| `/graph search QUERY` | Graphiti entity and fact search with scope provenance |
| `/graph episodes` | List recent selected source episodes |
| `/graph add TEXT` | Selected private source plus scope-isolated graph extraction |
| `/graph ingest PATH` | Explicit UTF-8 file ingestion, one episode, maximum 200 KB |
| `/graph curate` | Show correction, preference and decision proposals; saves nothing |
| `/graph curate NUMBER` | Save the selected proposal into the private project capture scope |

`graph` accepts pi-graphiti 0.6's `action: add|search|episodes`, `content`, `name`,
`source: text|message|json`, `query`, `limit`, and `scope: project|global|both`.
Limits are floored and clamped to 1..20. Search defaults to both aliases; add and
episodes default to project, unless `/graph` selection overrides them. An unbound
alias fails explicitly. JSON sources must parse as JSON. Responses retain entities,
facts, episode IDs and richer world/scope provenance rather than raw database group IDs.

Model-issued adds are permitted only when the alias matches the explicitly
configured private capture target. Run/world-shared writes require a selected user
publication through `/memory share`; the extension never turns `graph add` into
an implicit publication. Administrative clear/teardown and unrestricted upstream
graph-database imports are deliberately excluded.

Exposure includes same-authority worlds and scopes and survives forks, branches,
compaction, and reload. Reading evidence whose audience is narrower than the
capture target stops automatic capture. Before automatic capture, the daemon checks that every current reader of the
write target can read every source scope exposed in this session. This permits
run-shared context to feed invocation-private notes when the actual ACLs establish
that narrower audience. A failed, unknown, or foreign-authority relationship stops
automatic capture. The filesystem spool preserves the flow guard and rechecks it
before dispatch. Explicit selected notes remain available. The native child runner
below writes only into its own restricted invocation-private scope.

Source IDs, context IDs, projection choices, and receipt identities are immutable
across capture retries. Read-selection changes create new context bindings for
future source entries. Rotating credentials or switching authority disables
inherited automatic capture; enable future capture explicitly at the current leaf.

The optional filesystem spool stores immutable source envelopes in a private
0700 directory with 0600 files, without bearer tokens. It binds each envelope to
the service URL, authority UUID, credential ID, world, scope, original source ID,
and context. Atomic file publication and receipt validation precede deletion.
A drain snapshots its bearer credential, so a token-file rotation cannot redirect
queued content mid-drain. Queue capacity is 10,000 envelopes and 64 MiB; matching
retries remain allowed when full. Keep the spool on a local filesystem. A new
credential never automatically adopts another credential's pending sources.

With spooling configured, shutdown enqueues locally and performs no HTTP drain.
Later completed turns, precompaction, or `/memory spool drain` dispatch the queue.
Without it, Pi session entries provide retry durability for persisted sessions;
`--no-session` does not provide durable retries across process exit.

## Native subagent and review APIs

The `memory-worlds-pi/orchestrator` export provides `withSubagent`,
`runPiSubagent`, and `createIsolatedPiSession`:

```ts
import { runPiSubagent, createIsolatedPiSession } from "memory-worlds-pi/orchestrator";

const result = await runPiSubagent(client, {
  task: "Check the bearing constraints. Return selected findings with references.",
  privateWorldId: teamWorldId,
  parentContextId: parentContext.id,
  runId,
  agentId: specialistAgentUuid, // omit to create a fresh stable agent identity
  grants: [
    {world_id: teamWorldId, scope_id: runScopeId, operations: ["read"]}
  ],
  evidence: selectedEvidence,
}, runtime => createIsolatedPiSession(runtime, {modelRuntime, model}));

console.log(result.context.invocation_id, result.result);
```

The parent must have delegable authority for every grant. The service caps expiry
to the parent lifetime; omitted expiry uses the server default. `allowDelegation`
defaults false. `coordinatorRead: true` explicitly permits the parent to inspect
child scratch; the default returns only the selected result.

The runner passes only the explicit task brief and selected evidence from readable
delegated scopes. It rejects inherited Pi messages. The supplied model runtime is
an explicit operator dependency. Its fresh temporary cwd and agent directory use
in-memory sessions/settings, no ambient context files, extensions, skills, prompt
templates, MCP, shell or filesystem tools. Normal children receive only scoped
memory search and invocation-private capture tools. A child bearer token remains
in a closure and is cleared and revoked on success, failure, or cancellation.
The parent credential is snapshotted for the complete delegation and cleanup lease.
This is a native SDK boundary, not an operating-system process sandbox.

`memory-worlds-pi/curation` exports conservative `proposeMemories`, provider-based
`reviewMemories`, and `reviewWithPiSubagent`. The latter runs a short-lived isolated
Pi reviewer with **no tools**, validates a bounded JSON proposal array and requires
references to selected source entries. It returns proposals without saving them.

Rich extensions can supply `registerMemoryWorlds(pi, {review})` and enable
`reviewCorrections: true` to trigger their provider callback on likely user
corrections, at most once per three user turns. The callback receives only selected
candidate messages and an abort signal. Proposals are validated and shown for
review; no correction hook automatically writes or publishes memory.
`correctionHints` defaults true and can be disabled independently.
