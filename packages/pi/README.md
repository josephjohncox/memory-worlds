# Memory Worlds for Pi

Native Pi tools, commands, evidence rendering, scoped agent contexts and session
hooks over the Memory Worlds HTTP service. The extension uses a direct typed
client. Graph extraction and semantic retrieval use the service's Graphiti backend;
source search reads durable episodes.

Requires Node **22.19.0 or later** and Pi **1.3.0** from
`@earendil-works/pi-coding-agent`. Dependencies are pinned in `package-lock.json`.
The package is licensed under Apache-2.0.

## Install

```sh
mise install --locked
mise run pi:build
mise exec -- node packages/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js \
  install "$(pwd)/packages/pi"
```

Run these commands from the repository root. For one invocation, use the installed
`pi --extension ./packages/pi/dist/extension.js`. `mise run pi:pack` builds an
archive containing compiled code, type declarations, README and LICENSE. The build
cleans its owned `dist` directory before compilation.

## Configure

For a personal daemon alongside a corporate service, see the
[local and corporate memory guide](../../docs/hybrid.md).

Create `~/.pi/agent/memory-worlds.json` with explicit world/scope UUIDs or `@alias` references:

```json
{
  "url": "http://127.0.0.1:8787",
  "tokenFile": "~/.config/memory-worlds/pi.token",
  "readScopes": [
    {"world_id": "TEAM_WORLD_ID", "scope_id": "00000000-0000-4000-8000-000000000001"}
  ],
  "readMountIds": [],
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
  },
  "retrieval": {"enabled": false, "mode": "graph", "limit": 5, "maxChars": 8000}
}
```

Replace each example with an authorized identifier. References such as
`{"world_id":"@personal","scope_id":"@notes"}` resolve against live visible
service objects when a session starts. The session stores canonical IDs; renaming
an alias cannot silently retarget existing chat state. Missing or ambiguous aliases
fail explicitly. `project` and `global` remain explicit graph bindings; the
extension never derives authority from a directory or repository name.

Store the agent credential in a private regular file (`chmod 600`). Tokens are read
from that file for each ordinary operation. Multi-request delegation and spool
leases snapshot their credential so rotation cannot redirect private content.
Credentials never enter model results or session entries. Plaintext token fields
are rejected in configuration.

Environment overrides are `MEMORY_WORLDS_URL`, `MEMORY_WORLDS_TOKEN_FILE`, and
`MEMORY_WORLDS_CONFIG`. Loopback HTTP supports SSH forwarding; other hosts require
HTTPS. URL credentials, queries, fragments and redirects are rejected. The extension
does not load project-local configuration.

## Worlds, scopes and UUIDs

Each world contains independently authorized scopes:

| Scope | Purpose |
| --- | --- |
| `world-shared` | Published knowledge for authorized world participants |
| `run-shared` | Coordination and shared findings for one task or run |
| `agent-persistent` | Durable notes associated with a stable agent identity |
| `invocation-private` | Working notes belonging to one agent invocation |

Runs, scopes, stable agent identities and invocations have separate UUIDs. A Pi fork
creates a fresh context while retaining the stable agent identity. An ordinary
credential receives a fresh invocation UUID; a restricted child retains the
invocation UUID fixed by its delegation.

Name a stable agent and select it through the live identity registry:

```text
/memory identity agent create architect Design architect
/memory agent @architect
/memory identities agent
/memory identity agent name @architect Primary architect
/memory identity run create investigation Bearing investigation
/memory run @investigation
/memory identity invocation current first-pass Initial inspection
```

To reuse that agent across independent chats, add `"agent":"@architect"` to the
configuration. Each chat still binds a fresh invocation. A UUID can replace the
alias and must also identify a registered visible agent. Without this setting,
new chats receive independent agent UUIDs; forks retain their agent identity.
`/memory agent` and `/memory run` select attribution within the current chat and
disable capture and sharing before rebinding its context. They preserve reads and
the private write destination. `run off` clears run attribution. A delegated child
retains its credential's fixed agent/invocation identities.

Registry entries have an owner, UUID, display name and optional per-kind alias.
They label agents, runs and invocations; they do not grant scope access. Use
`/memory identity KIND alias REF ALIAS|none` to change an alias. `current` names an
already bound UUID, while `create` creates a named identity. The typed client
provides `identities`, `resolveIdentity`, `createIdentity`, and `updateIdentity`.

Read scopes and the private capture target are independent. Capture requires an
`agent-persistent` or `invocation-private` target. World membership never implicitly
grants access to another invocation's private notes. Shared writes use explicit
selected publications or a user-enabled per-chat sharing rule.

Before automatic capture, the daemon verifies that every current reader of the
write target can read every source scope exposed in the session. Run-shared context
can therefore feed narrower invocation-private working notes. A failed, unknown or
foreign-authority relationship disables automatic capture. Exposure survives forks,
branches, compaction and reload; changing selections cannot erase it.

## Commands and tools

`/memory` opens a selector in interactive Pi. The footer and widget show connectivity,
read scopes, capture policy and pending deliveries. Expand evidence messages to
inspect authority/world/scope/source attribution.

| Command | Behavior |
| --- | --- |
| `/memory status` | Current context, selections, capture policy and deliveries |
| `/memory discover [WORLD]` | List visible worlds, readable scopes and authorized mounts together |
| `/memory worlds` | Discover authorized worlds |
| `/memory scopes WORLD` | Discover authorized scopes in a world |
| `/memory world-alias WORLD NAME\|none` | Set or clear an authorized world alias |
| `/memory scope-alias WORLD SCOPE NAME\|none` | Set or clear an authorized scope alias |
| `/memory identities KIND` | List visible agent, run or invocation names and UUIDs |
| `/memory agent REF`, `/memory run REF` | Select registered attribution without selecting memory scopes |
| `/memory import DIRECTORY --preview` | Validate local extracted files and show a content-free report |
| `/memory import DIRECTORY` | Atomically import into the current private target |
| `/memory read WORLD SCOPE` | Add an explicit read scope; `none` clears local reads |
| `/memory read WORLD` | Resolve and add that world's actual shared scope |
| `/memory mounts` | Discover configured remote mounts |
| `/memory mount A,B` | Select remote mounts; `none` clears them |
| `/memory target WORLD SCOPE` | Select a private capture target; `off` clears it |
| `/memory capture on` | Capture future user/assistant text after authorization |
| `/memory capture off` | Stop automatic capture |
| `/memory capture TEXT` | Save an explicitly selected private note |
| `/memory search QUERY` | Retrieve graph evidence |
| `/memory source QUERY` | Retrieve durable source evidence explicitly |
| `/memory alias project WORLD SCOPE` | Bind the project alias; `global` binds the other alias |
| `/memory share WORLD SCOPE TEXT` | Publish selected text to another local scope |
| `/memory sharing review WORLD SCOPE` | Classify future messages into proposals for user approval |
| `/memory sharing auto WORLD SCOPE` | Enable selected automatic publication under the configured rule |
| `/memory sharing auto mount MOUNT WORLD` | Enable selected remote publication through an authorized mount |
| `/memory sharing pending` | Inspect private proposals and publication state |
| `/memory sharing approve ID` | Publish an individual review proposal |
| `/memory sharing private` | Stop new and unsubmitted automatic sharing |
| `/memory publish MOUNT WORLD TEXT` | Publish selected text through the router outbox |
| `/memory deliveries` | Inspect this caller's remote publication deliveries |
| `/memory spool status` | Inspect the configured local queue |
| `/memory spool drain` | Dispatch sources pinned to the current credential and authority |

The model receives `memory_discover`, `memory_search`, `memory_capture`,
`memory_status`, and `graph`.
Publication requires a user command or a user-enabled sharing rule. A model tool
cannot enable a rule or choose its destination. An explicit selected note remains
available when automatic capture is off. See [chat sharing modes](../../docs/chat-sharing.md)
for classifier configuration, category/topic exclusions, review, and retry behavior.

World and scope arguments accept canonical IDs or explicit `@aliases`, including
read/target selections, graph bindings, selected publication and local sharing
destinations. Remote mount worlds retain their remote IDs. The typed client offers
`resolveWorld`, `resolveScope`, `resolveSelection`, `setWorldAlias`, and
`setScopeAlias`; alias lookup never grants permission.

Discovery works with an ordinary user credential, including an OIDC access token;
an administrator token is unnecessary. `/memory discover` returns
`{worlds, scopes, mounts}`, where `scopes` maps each visible world ID to its readable
scopes. `/memory discover @personal` selects one live visible world by alias.
Discovery returns metadata and identifiers without source contents or credentials.
It does not select read scopes or mounts, enable capture, or change a write target.

The same operation is available to harnesses through the typed client:

```ts
const available = await client.discover();
const personal = await client.discover({world: "@personal"});
const worldsAndMounts = await client.discover({includeScopes: false});
```

The `memory_discover` model tool accepts optional `world` and `include_scopes`
arguments; scope discovery defaults to true. It lists only scopes the current
credential can read. A write or publish grant alone does not expose private scope
metadata. Mounts are authorized local router references; discovery does not query
the remote system or enumerate its private scopes. A live authorization failure
rejects the operation. Token-file, transport and parser diagnostics are redacted
before they can enter a model tool result. Results exceeding 2 MiB fail explicitly; select a world or
omit scopes to reduce the result.

## Import extracted directories

Install the matching Memory Worlds Python CLI on the machine running Pi. The
configuration's `cliPath` defaults to `memory-worlds`; for a source checkout, set
it to the absolute `CHECKOUT/.venv/bin/memory-worlds` after `mise run python:deps`.
The executable is trusted local configuration and is never selected by a model.

```text
/memory target @personal @history
/memory import "/private/chat exports" --preview --source-timezone America/Los_Angeles
/memory import "/private/chat exports" --source-timezone America/Los_Angeles
/memory import /private/documents --format documents --fallback-timestamp 2026-10-20T12:00:00Z
```

The target must be an `agent-persistent` or `invocation-private` scope in your own
personal or private-work world. Imports leave chat reads, capture mode, and graph
bindings unchanged. No model tool can select a directory or initiate this command.
Graphiti extraction is queued by default; `--source-only` explicitly skips it.
Other options are `--format auto|chatgpt|claude|richlow-md|documents` and
`--all-branches`. The interactive `/memory` menu offers preview and apply choices.

The native bridge runs `plan-directory --emit-records` with fixed argument arrays,
no shell and no credential environment. It bounds private subprocess output,
validates the resulting records, then sends one typed `/imports` request under a
pinned memory credential. Source records stay in private pipes and memory; only
the report and imported/replayed counts reach the user interface. Token rotation
before dispatch stops the operation. Preview performs no import request.

Native JSON retains source identities. Rendered Markdown remains an opaque
snapshot: long files become lossless consecutive chunks rather than invented
turns. Reimporting unchanged files replays; edited rendered bytes create a new
snapshot. Richlow local dates require an explicit timezone, and document mode
requires an aware fallback timestamp. See the [directory import guide](../../docs/chat-import.md)
for filesystem limits, format selection and fidelity boundaries.

```ts
import {MemoryWorldsClient, readTokenFile} from "memory-worlds-pi/client";
import {planDirectory, importDirectory} from "memory-worlds-pi/imports";

const client = new MemoryWorldsClient(url, () => readTokenFile(tokenFile));
const destination = await client.resolveSelection("@personal", "@history");
const options = {cliPath: "/absolute/path/memory-worlds", sourceTimezone: "Etc/UTC"};
const preview = await planDirectory(directory, options);
console.log(preview.report); // The plan's episodes contain private source text.
const receipt = await importDirectory(client, destination, directory, options);
console.log(receipt.report, receipt.imported, receipt.replayed);
```

`client.importEpisodes(destination, episodes)` is the lower-level atomic batch
operation for already parsed records. It fixes the explicit scope, bounds the
request, rejects context bindings/publications, and validates receipt counts.

## Graph actions

The native `graph` tool supports:

- `action: add|search|episodes`.
- `content`, `name`, `source: text|message|json`, `query`, and `limit`.
- `scope: project|global|both`, mapped to explicitly bound service scopes.

Limits are floored and clamped to 1..20. Search defaults to both aliases; add and
episodes default to project. `/graph project`, `/graph global`, or `/graph both`
select an override. An unbound alias fails explicitly.

Search returns entities and facts with scope provenance and record IDs. Episodes
returns recent source records. Adds require the alias to match the configured
private capture target and request scope-isolated graph extraction (`project: true`).
JSON sources must parse as JSON. The service needs a configured graph backend and
model providers for extraction and semantic search.

| Command | Behavior |
| --- | --- |
| `/graph status` | Inspect context and graph selections |
| `/graph project`, `/graph global`, `/graph both` | Select alias reads independently of the write target |
| `/graph search QUERY` | Search entities and facts |
| `/graph episodes` | List selected source episodes |
| `/graph add TEXT` | Store a private source and request graph extraction |
| `/graph ingest PATH` | Explicit UTF-8 file ingestion: one episode, at most 200 KB |
| `/graph curate` | Show correction, preference and decision proposals; saves nothing |
| `/graph curate NUMBER` | Save one selected proposal into the private project target |

Feature coverage is documented in [Pi graph features](../../docs/pi-graphiti-features.md).

## Capture and offline durability

Capture defaults off. Automatic capture requires an explicit private `writeScope`
and consent through configuration or `/memory capture on`. Once captured, memory
queues Graphiti extraction by default (`projectCaptured: true`). Set
`projectCaptured: false` only for explicit source maintenance or recovery.
`/memory search` and the memory search tool retrieve graph evidence by default;
`/memory source` requests source records explicitly.

Only future user/assistant text on the active branch is captured automatically.
Images, thinking blocks and tool output are excluded. Original session/entry IDs,
parentage, timestamps, context, projection choice and flow guard remain immutable
across retries. Successful receipts become session entries. A lost acknowledgment
replays the same source rather than creating another episode.

Read-selection changes bind new immutable contexts for future entries. Credential
or authority changes disable inherited automatic capture; explicitly re-enable it
at the current leaf after reviewing the new binding.

The optional filesystem spool stores immutable envelopes in a private 0700 directory
with 0600 files. Each envelope binds the service URL, authority, credential ID,
world/scope UUIDs, source, context and audience-flow guard. Bearer tokens are omitted.
Atomic file publication precedes dispatch; full source receipt validation precedes
deletion. Audience flow is checked again before upload.

Enqueue checks a 10,000-envelope and 64 MiB capacity. Matching immutable retries
remain permitted when full. Use a local filesystem. A new credential never adopts
another credential's pending sources automatically.

Completed turns and precompaction flush capture with a five-second total budget.
With a spool configured, shutdown only enqueues locally and performs no HTTP drain.
A later healthy turn or `/memory spool drain` dispatches pending envelopes. Without
it, persisted Pi session entries provide retry durability; `--no-session` provides
no durability across process exit.

Pre-turn retrieval is separately opt-in. It requests at most 20 records and renders
at most 32,000 characters. Evidence enters a labeled custom message; the extension
appends a short scope/publication policy to the existing system prompt. Source and
graph retrieval remain distinct. Remote failures are explicit partial results;
unauthorized local scope selections reject the request.

## Native subagents and review

Public SDK entry points are `memory-worlds-pi/client`, `/imports`, `/graph`,
`/orchestrator`, `/curation`, `/sharing`, and `/spool`.

```ts
import {MemoryWorldsClient, readTokenFile} from "memory-worlds-pi/client";
import {runPiSubagent, createIsolatedPiSession} from "memory-worlds-pi/orchestrator";

const client = new MemoryWorldsClient(
  "http://127.0.0.1:8787",
  () => readTokenFile("/absolute/private/path/pi.token"),
);
const result = await runPiSubagent(client, {
  task: "Check the bearing constraints. Return selected findings with references.",
  privateWorldId: teamWorldId,
  parentContextId: parentContext.id,
  runId: runScope.run_id,
  agentId: specialistAgentUuid,
  grants: [{world_id: teamWorldId, scope_id: runScope.id, operations: ["read"]}],
  evidence: selectedEvidence,
}, runtime => createIsolatedPiSession(runtime, {modelRuntime, model}));

console.log(result.context.invocation_id, result.result);
```

The parent must have delegable authority for every grant. Omitted expiry uses the
server default, capped by the parent's lifetime. `allowDelegation` defaults false.
`coordinatorRead: true` explicitly permits parent inspection of child scratch; the
default returns only the selected result.

The runner passes only the task brief and selected evidence from delegated readable
scopes and rejects inherited messages. Its fresh temporary cwd and agent directory
use in-memory sessions/settings and load no ambient context files, extensions,
skills, prompt templates, MCP, shell or filesystem tools. The model runtime is an
explicit operator dependency. Normal children receive `memory_discover`, scoped
memory search, and invocation-private capture tools. Discovery uses only the
child's delegated credential: it reveals readable delegated scopes and the child's
own private scope, without inheriting the parent's wider roster or remote mounts.
Their token stays in a closure and is cleared
and revoked on success, failure or cancellation. This SDK boundary is not an
operating-system process sandbox.

`proposeMemories` produces conservative candidates. `reviewMemories` validates a
provider's proposals. `reviewWithPiSubagent` runs an isolated short-lived Pi reviewer
with no tools, validates bounded JSON proposals and requires selected-source
references. It returns proposals without saving them.

Rich extensions can supply `registerMemoryWorlds(pi, {review})` and enable
`reviewCorrections: true` to trigger their callback on likely user corrections, at
most once per three user turns. The callback receives only selected candidates and
an abort signal. Proposals are shown for review; correction hooks never automatically
write or publish memory. `correctionHints` defaults true and is independently configurable.

## Verify

```sh
mise run pi:verify
mise run pi:smoke
mise run pi:pack -- --dry-run
```

Tests cover scope authorization, UUID bindings, audience flow, source/graph modes,
credential rotation, immutable retries, spool retention, branch exposure, selected
publication, isolated Pi sessions and validated review. RPC smokes load the actual
Pi package and exercise a real authenticated Python daemon with SQLite. Set
`MEMORY_WORLDS_SMOKE_PYTHON` to an installed Python interpreter containing Memory
Worlds and uvicorn. The sharing smoke also exercises the actual Pi model registry
with a synthetic provider. These smokes use synthetic data and no paid-model calls.
