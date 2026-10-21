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

The model receives `memory_search`, `memory_capture` and `memory_status` tools.
Publication is a user command: no model tool or automatic hook publishes data.
Reading worlds and mounts never changes the private write target. An explicit
private note remains available after mixed-authority retrieval.
Repeating the same publication text to the same destination in one session reuses
its durable request identity, including after a lost acknowledgment. Session
metadata retains its content hash and request identity, without copying the text.

## Capture and session boundaries

Capture starts **off** unless both `automaticCapture: true` and an explicit
private `writeWorldId` are configured. The service verifies the target is
`personal` or `private-work` before capture. Source capture never implicitly
requests model extraction (`project: false`).

Automatic capture records only future user/assistant text on the active branch.
Source identity includes the original Pi session and entry IDs; timestamps,
roles and parent entry IDs are stable on retries. Successful receipts are saved
as extension entries. Offline failures retry on the next completed turn; a lost
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
Evidence enters a labeled custom message, without replacing the system prompt.
Source and graph modes remain distinct; unavailable services produce explicit
partial results, without pretending source matches are semantic results.
Requests time out after five seconds and cancel on session shutdown/reload.

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
