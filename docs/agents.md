# Connect agent clients

[Documentation](README.md) · [Scopes and subagents](scopes.md)

All clients connect to the running HTTP service, including agents in remote
shells. Give each agent an ordinary user credential or its own delegated
credential. Ordinary OIDC users can discover their accessible worlds without an
administrator token; delegated children see only their granted audience.

| Client | Choose it for |
| --- | --- |
| [Native Pi extension](../packages/pi/README.md) | Slash commands, session hooks, evidence rendering and rich Pi subagents |
| Python `Client` | Typed harnesses and orchestration, including child delegation |
| CLI | Discovery, explicit capture/import and operator workflows |
| MCP stdio facade | Hosts that integrate tools through MCP |

Start with [discovery](discovery.md) to find world IDs, scope UUIDs and mounts.
Read selections and write destinations are separate. A context records selection
and attribution; a delegated credential sets a child's actual permission ceiling.

## Python

This example writes and searches one explicitly selected private scope. Supply
`service_url`, `agent_token`, `world_id` and `scope_id` from your private client
configuration and discovery; keep tokens out of prompts and logs.

```python
from datetime import UTC, datetime

from memory_worlds.client import Client
from memory_worlds.models import EpisodeInput, ScopeSelection, SearchRequest

selection = ScopeSelection(world_id=world_id, scope_id=scope_id)
with Client(service_url, agent_token) as client:
    receipt = client.capture(
        world_id,
        EpisodeInput(
            scope_id=selection.scope_id,
            source_id="agent-session-42:entry-7",
            content="The approved fixture uses a ceramic bearing.",
            occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
            session_id="agent-session-42",
            source_entry_id="entry-7",
            role="user",
        ),
    )
    results = client.search(SearchRequest(scopes=[selection], query="ceramic"))
```

Reuse the original source ID and timestamp on retries. A capture receipt confirms
source storage and reports `replayed`; Graphiti extraction completes separately.
Search uses Graphiti by default; set `mode="source"` for durable source text.

For scoped children, use the [delegation example](reference/scopes.md#create-and-delegate).
The [scope reference](reference/scopes.md) also covers contexts, aliases,
publication and credential expiry.

## MCP

Install the MCP extra through the project's locked dependency task
(`mise run python:deps`), then configure your host to run the stdio client:

```json
{
  "command": "/absolute/path/to/.venv/bin/memory-worlds",
  "args": [
    "--url", "http://127.0.0.1:8787",
    "--token-file", "/absolute/private/path/agent.token",
    "mcp"
  ]
}
```

Use the installed executable's absolute path. Several MCP clients can share one
daemon; the stdio process opens no storage and grants no additional permissions.

| Tools | Purpose |
| --- | --- |
| `memory_discover`, `memory_worlds`, `memory_scopes`, `memory_mounts` | Discover the caller's visible metadata |
| `memory_search`, `memory_remote_search`, `memory_episodes` | Retrieve selected evidence |
| `memory_capture`, `memory_publish` | Save or explicitly publish selected text |
| `memory_bind_context`, `memory_context`, `memory_scope_status` | Bind context or inspect scoped state |
| `memory_deliveries` | Inspect the caller's remote publication receipts |

Scope selections and context IDs are explicit tool arguments. The facade exposes
no credential issuance, delegation or grant-administration tool. A harness that
needs to issue child credentials uses the typed client or CLI through a private
channel, keeping bearer tokens out of model-visible output.

## Native Pi

The [Pi quickstart](../packages/pi/README.md) covers install and first use. The
[Pi reference](reference/pi.md) documents commands, configuration, capture
retries, disk spool and SDK exports. Its direct typed client and native session
integration require neither MCP nor a Pi fork.

Automatic capture starts disabled. Publication requires a user command or a
user-enabled [per-chat sharing rule](chat-sharing.md). For corporate login,
mounts and team mappings, see [OIDC](oidc-okta.md) and [local plus corporate memory](hybrid.md).
