# Memory Worlds for OpenCode

Native OpenCode tools for discovering accessible worlds, recalling evidence and
storing selected text in a private agent scope. The plugin calls the Memory Worlds
HTTP API directly. It uses a scoped user or agent credential and fixed profile
selections.

Start the service with the [setup guide](https://github.com/josephjohncox/memory-worlds/blob/main/docs/setup.md).
Create a native client profile with the Memory Worlds CLI:

```sh
mise run cli -- clients profile --config /private/setup.toml \
  --profile personal --output /private/memory-worlds/opencode.json
```

Set `MEMORY_WORLDS_CONFIG` to that file's absolute path before starting OpenCode.
A profile contains only:

```json
{
  "url": "http://127.0.0.1:8787",
  "tokenFile": "/private/memory-worlds/opencode.token",
  "readScopes": [
    {
      "world_id": "YOUR_WORLD_ID",
      "scope_id": "dd820226-39e7-47cd-afbc-565c4109dfc0"
    }
  ],
  "writeScope": {
    "world_id": "YOUR_WORLD_ID",
    "scope_id": "dd820226-39e7-47cd-afbc-565c4109dfc0"
  }
}
```

Replace both world/scope selections with IDs from your service. Omit `writeScope`
for a reader. The token must be a private regular file with mode `0600`;
administrator tokens are rejected. Remote services require HTTPS. Each tool reads
the current credential file and keeps that credential fixed for its operation.

Build a checkout package through the repository's mise tasks:

```sh
mise run opencode:verify
mise run opencode:pack -- --pack-destination /private/build-output
```

Install the resulting tarball in the OpenCode configuration directory's
`package.json`, then load one local plugin file:

```ts
// ~/.config/opencode/plugins/memory-worlds.ts
export { MemoryWorldsPlugin } from "memory-worlds-opencode";
```

OpenCode installs configuration-directory dependencies at startup. See the
[official plugin installation instructions](https://opencode.ai/docs/plugins/).
The package entry exports one plugin; use `memory-worlds-opencode/factory` only
when embedding a configured factory in your own integration.

| Tool | Behavior |
| --- | --- |
| `memory_discover` | Lists authorized worlds and mounts; optionally lists visible scopes |
| `memory_search` | Searches only the profile's `readScopes`, using graph or source retrieval |
| `memory_capture_private` | Captures explicit text into the fixed `writeScope`, after checking its private scope kind |
| `memory_status` | Reports the current identity and fixed scope selections |

Private capture requires an `agent-persistent` or `invocation-private` scope. It
records the OpenCode session, message and agent identifiers as attribution.
Supply stable `text`, `sourceId` and `occurredAt` arguments. Before the first
capture POST, the plugin durably saves the full request, including its original
session/message/agent attribution. Retrying those arguments from a later message,
new session or restarted plugin reuses that request. Changed text or timestamp
under the same intent key fails locally. The server's receipt confirms storage
and identifies the episode without echoing its text.

## Private capture journal

Explicit captures create records below
`$XDG_STATE_HOME/memory-worlds/opencode/captures`, or
`~/.local/state/memory-worlds/opencode/captures` when `XDG_STATE_HOME` is unset.
The directory is private (`0700`) and records are created with mode `0600`.
Records contain selected capture text and origin metadata, never the token.
Embedding integrations can pass an absolute `intentDirectory` to the factory;
the connection profile still accepts only the four fields shown above.

Each record binds the service URL, live credential ID, write world/scope and
source ID. Every attempt rereads the credential, checks the current identity
and private target, and sends the request through normal server authorization.
A different credential, URL or destination has a different intent key; it does
not reuse the earlier credential's request.

Records remain after success to preserve replay. Each is limited to 1 MiB, and
new intents are rejected when the directory already has 4,096 entries. Existing
intents can still be retried. There is no background upload or automatic retry
worker. Removing a record neither deletes
the server episode nor preserves its frozen attribution for future retries;
retain it while replay may still be needed.

The plugin performs explicit tool calls. It has no transcript capture,
publication, automatic sharing, automatic context injection or subagent delegation hooks.
OpenCode session IDs provide attribution; server credentials and grants authorize
access. For broader workflows, use the [scope guide](https://github.com/josephjohncox/memory-worlds/blob/main/docs/scopes.md)
and [client integrations](https://github.com/josephjohncox/memory-worlds/blob/main/docs/clients.md).

Development checks compile against `@opencode-ai/plugin` 1.19.3 and exercise the
actual plugin factory and tools with a controlled HTTP transport. A full OpenCode
host session is a separate qualification step.
