# Memory Worlds for Pi

Give Pi agents durable private memory and access to selected team knowledge.
The native extension provides tools, slash commands, session capture, evidence
rendering and scoped subagents through the Memory Worlds HTTP service. It uses
a typed client directly; MCP and a Pi fork are unnecessary.

Graphiti extracts and searches each scope's graph. Durable source records remain
available separately. Automatic capture, pre-turn retrieval and chat sharing
start disabled.

Requires Node **22.19.0 or later**, Pi **1.3.0** and a running Memory Worlds
service. Licensed under Apache-2.0.

## Install

From the repository root:

```sh
mise install --locked
mise run pi:build
mise exec -- node packages/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js \
  install "$(pwd)/packages/pi"
```

For one invocation, load the built extension with
`pi --extension ./packages/pi/dist/extension.js`. Run `mise run pi:pack` to build
an installable archive. Start the service with the
[guided setup](https://github.com/josephjohncox/memory-worlds/blob/main/docs/setup.md)
if you do not already have one.

## Configure

If guided setup generated a personal profile, select it when launching Pi.
For the default local instance named `personal`:

```sh
env -u MEMORY_WORLDS_URL -u MEMORY_WORLDS_TOKEN_FILE \
  MEMORY_WORLDS_CONFIG="$HOME/.local/state/memory-worlds/personal/pi-personal.json" pi
```

For another instance, use its configured state directory and profile path.
The generated profile already selects its private notes scope. Hybrid setup also
generates separate local work-research and direct corporate profiles; see
[local and corporate memory](https://github.com/josephjohncox/memory-worlds/blob/main/docs/hybrid.md).

For manual configuration, save the following as
`~/.pi/agent/memory-worlds.json`, replacing the token-file path with your agent
credential. Store that credential in a private file with mode `0600`.

```json
{
  "url": "http://127.0.0.1:8787",
  "tokenFile": "~/.config/memory-worlds/pi.token"
}
```

This connection selects no memory and enables no capture. Use an ordinary user
or agent token; discovery does not require administrator privileges. Loopback
HTTP supports a local daemon or SSH tunnel; other hosts require HTTPS.

## First use

In Pi, inspect what your credential can access. For a manual installation, first
[create a private scope](https://github.com/josephjohncox/memory-worlds/blob/main/docs/chat-import.md#create-a-private-archive-scope)
if discovery shows only a world-shared scope; a writer role on that world is sufficient.
Then select reads and the existing private write destination:

```text
/memory discover
/memory read WORLD_ID READ_SCOPE_UUID
/memory target PERSONAL_WORLD_ID PRIVATE_SCOPE_UUID
/memory search approved bearing material
/memory capture The approved fixture uses a ceramic bearing.
/memory status
```

Use IDs returned by discovery. An existing `@alias` can replace a world or scope
ID. The private target must be an `agent-persistent` or `invocation-private`
scope that your credential can write. Reading a scope never selects a write
destination. `/memory capture on` enables future user/assistant capture after
the service checks the audience; `/memory capture off` stops it.

`/memory` opens the interactive selector. The bottom status bar shows connectivity,
capture policy, exposure warnings and sharing mode. Use `/memory status` for
scopes, the write target and pending deliveries. Evidence messages retain
authority, scope and source references. `/memory source QUERY` searches durable
records explicitly.

## Go deeper

- [Startup audit](https://github.com/josephjohncox/memory-worlds/blob/main/docs/reference/pi-startup.md):
  measure isolated Pi startup and memory-service latency with synthetic fixtures.
- [Pi reference](https://github.com/josephjohncox/memory-worlds/blob/main/docs/reference/pi.md):
  configuration, `/memory` and `/graph` commands, offline spool and native SDK.
- [Agent and subagent scopes](https://github.com/josephjohncox/memory-worlds/blob/main/docs/scopes.md):
  world-shared, run-shared, agent-persistent and invocation-private audiences.
- [Chat sharing](https://github.com/josephjohncox/memory-worlds/blob/main/docs/chat-sharing.md):
  user-enabled review or automatic publication of selected excerpts.
- [Chat import](https://github.com/josephjohncox/memory-worlds/blob/main/docs/chat-import.md):
  preview and import extracted ChatGPT, Claude or document directories.
- [Pi Graphiti features](https://github.com/josephjohncox/memory-worlds/blob/main/docs/pi-graphiti-features.md):
  graph actions, curation, coverage and limitations.

Rich Pi integrations can import `memory-worlds-pi/client`, `/orchestrator`,
`/imports`, `/graph`, `/curation`, `/sharing` and `/spool`. The orchestrator issues
delegated child credentials and starts fresh Pi sessions with an explicit brief
and selected evidence. Parents and siblings receive no automatic access to a
child's invocation-private notes. See the
[SDK example](https://github.com/josephjohncox/memory-worlds/blob/main/docs/reference/pi.md#native-subagents-and-review).
