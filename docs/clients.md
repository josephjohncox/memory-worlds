# Use Memory Worlds from coding agents

[Documentation](README.md) · [Python and MCP tools](agents.md)

Pi, Codex, Claude Code and OpenCode can use the same local daemon or corporate
API. Each connects with an ordinary user or agent credential; no client needs
the operator token. Native integrations add harness-specific tools and behavior
on top of the same authorized service.

| Harness | Integration | Behavior |
| --- | --- | --- |
| [Pi](../packages/pi/README.md) | Native extension | Tools, slash commands, evidence rendering, scoped subagents and optional session capture/sharing |
| Codex | Exported MCP configuration | Discovery, selected recall, explicit capture/publication tool calls |
| Claude Code | Exported MCP configuration or [plugin bundle](../plugins/claude/README.md) | MCP tools; the bundle also supplies a scoped-memory skill |
| [OpenCode](../packages/opencode/README.md) | Native plugin or exported MCP configuration | Native discovery, scoped search, private capture with durable replay and status; MCP exposes the wider facade |

## Set up the service once

From a reviewed checkout with Docker running:

```sh
mise trust
mise install --locked
mise run setup
```

Use [guided setup](setup.md) to choose explicit extraction/embedding models,
apply the configuration and start the daemon or install its background service.
Keep the chosen `setup.toml` path; client exports read its applied receipt and
reuse its private credential files. They do not provision another authority.

Choose the intended profile:

| Profile | Connection |
| --- | --- |
| `personal` | Local personal memory; default export selection |
| `work` | Hybrid installation's separate local work credential; no default write target |
| `corporate` | Hybrid installation's direct corporate user connection; no default scope selections |

Hybrid profiles require an applied hybrid setup. See [local and corporate memory](hybrid.md)
for their audiences. Exporting a profile neither broadens grants nor enables
automatic capture, retrieval or sharing.

Exports reuse the selected profile's existing credential. They do not issue a
new identity for each harness. Use separate [delegated credentials](scopes.md)
when clients need independent attribution, permissions or revocation.

## Export an MCP connection

Choose new output filenames in an existing private directory. These commands
read the applied setup state without contacting a model, signing in or using an
administrator credential:

```sh
config="$HOME/.config/memory-worlds/default/setup.toml"
mise run cli -- clients export --client codex --config "$config" \
  --profile personal --output "$HOME/.config/memory-worlds/codex.toml"
mise run cli -- clients export --client claude --config "$config" \
  --profile personal --output "$HOME/.config/memory-worlds/claude.json"
mise run cli -- clients export --client opencode --config "$config" \
  --profile personal --output "$HOME/.config/memory-worlds/opencode.json"
```

The exporter creates mode-0600 files and refuses to overwrite an existing file.
It includes the service URL and credential-file path, never the token value.
It changes no global harness configuration. Keep the installed Python runtime
and private token files at the exported paths. Exports verify the applied
credential and corporate-connection bindings; an unexpected replacement token
fails instead of silently selecting a different identity.

For Codex, review and merge the `[mcp_servers.memory-worlds]` entry into your
private `~/.codex/config.toml`, preserving existing settings. The format follows
[Codex's official MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

Claude Code can load the exported `mcpServers` configuration for one invocation:

```sh
claude --mcp-config "$HOME/.config/memory-worlds/claude.json"
```

For OpenCode, select the exported configuration:

```sh
OPENCODE_CONFIG="$HOME/.config/memory-worlds/opencode.json" opencode
```

These use [Claude Code's MCP support](https://code.claude.com/docs/en/mcp) and
[OpenCode's custom configuration path](https://opencode.ai/docs/config/).
Hosts still apply their own configuration precedence and tool-permission rules.

For a manually managed daemon or a direct corporate connection without guided
setup, configure the same stdio command using [the MCP reference](agents.md#mcp)
and a normal user/delegated credential.

## Native integrations

Pi's [installation guide](../packages/pi/README.md#install) covers installing the
built extension and selecting its generated profile. It provides the full
session integration, including disabled-by-default automatic capture,
retrieval, offline spool and [per-chat sharing](chat-sharing.md).

Export a minimal connection for native clients when needed:

```sh
mise run cli -- clients profile --config "$config" --profile personal \
  --output "$HOME/.config/memory-worlds/native-personal.json"
```

The JSON contains `url`, `tokenFile`, selected `readScopes` and any private
`writeScope`. Pi and the native OpenCode plugin accept that connection shape.
The [OpenCode package guide](../packages/opencode/README.md) covers installation,
profile selection and its explicit tools. After installing it, select the native
profile when launching:

```sh
MEMORY_WORLDS_CONFIG="$HOME/.config/memory-worlds/native-personal.json" opencode
```

It adds no automatic session capture or sharing hooks. Use one OpenCode
integration at a time to avoid duplicate memory tools. The corporate native
profile starts without scopes; add authorized selections from discovery before
using its fixed search/write configuration.

Native OpenCode capture durably records the original request before sending it.
Retry the same `text`, `sourceId` and `occurredAt`; the plugin preserves original
attribution across new sessions and restarts, while rechecking the current
credential and target. Its [private local journal](../packages/opencode/README.md#private-capture-journal)
retains selected text after success and never uploads in the background.

The [Claude Code plugin bundle](../plugins/claude/README.md) adds a skill around
the MCP tools. From the repository root, launch it with:

```sh
claude --plugin-dir ./plugins/claude
```

When prompted, set `runtime_python` to the installed Python interpreter shown in
an exported MCP configuration, `url` to the service and `token_file` to the
private agent credential file. No setting takes a bearer value. Choose either
this bundle or the plain MCP export for a session. `/memory-worlds:memory-worlds`
invokes the scoped-memory skill. The bundle uses
[Claude Code's plugin manifest and MCP support](https://code.claude.com/docs/en/plugins-reference);
it adds no automatic transcript capture.

## Discover, select and write

First ask the harness to call `memory_discover`, or use the native client's
discovery tool. Inspect the returned world IDs, scope UUIDs and authorized
mounts. Read selection and write destination remain separate. A private capture
target must be an authorized agent-persistent or invocation-private scope.

MCP exposes [the service's explicit tools](agents.md#mcp). It does not inherit
Pi's session hooks, exposure ledger, offline spool or per-chat classifier. A Pi
session's exposure record does not follow copied text into another harness.
`memory_scope_flow` checks live audience containment for explicit source scopes
and a destination; it neither tracks exposure nor grants publication consent.
Use user-authorized selected publication for MCP integrations.

Use [delegation](scopes.md) when a subagent needs narrower access. A context ID,
agent UUID or shared profile name is attribution, not an access grant.

## Change or remove a connection

After changing an applied setup, export to a new filename and review the host's
updated configuration. After credential rotation, select the intended token
file and restart the client process. Removing an MCP entry or disabling a plugin
disconnects that harness; it retains the daemon and memory. Revoke the corresponding
credential when the credential itself should stop working.
