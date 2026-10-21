# Agent clients

Each agent receives its own token and an explicit set of world grants. All
clients use the running HTTP daemon, including clients in remote shells.

```python
import os
from datetime import UTC, datetime

from memory_worlds.client import Client
from memory_worlds.models import EpisodeInput, SearchRequest

with Client(os.environ["MEMORY_WORLDS_URL"], os.environ["MEMORY_WORLDS_TOKEN"]) as client:
    receipt = client.capture(
        "WORLD_ID",
        EpisodeInput(
            source_id="agent-session-42:entry-7",
            content="The approved fixture uses a ceramic bearing.",
            occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
            session_id="agent-session-42",
            source_entry_id="entry-7",
            role="user",
        ),
    )
    results = client.search(SearchRequest(world_ids=["WORLD_ID"], query="ceramic"))
```

Preserve the original timestamp and source identity on retries. A successful
capture queues Graphiti extraction and returns the stored episode and `replayed`; its projection state is
independent from durable receipt. Each read request names its worlds explicitly.

For MCP clients, install the MCP extra and configure a stdio server command:

```json
{
  "command": "/absolute/path/to/.venv/bin/memory-worlds",
  "args": ["--url", "http://127.0.0.1:8787", "mcp"],
  "env": {"MEMORY_WORLDS_TOKEN": "agent-specific-token"}
}
```

Use your client's secret mechanism for the token, or pass `--token-file` before
`mcp`. The tools include `memory_worlds`, `memory_search`, `memory_capture`,
`memory_publish`, `memory_mounts`, `memory_remote_search`, and `memory_deliveries`,
plus `memory_scopes`, `memory_scope_status`, `memory_episodes`,
`memory_bind_context`, and `memory_context` for scoped agent contexts.
Scope selections and bound context IDs are explicit arguments. MCP exposes no
credential issuance or grant administration tool. See [agent scopes](scopes.md)
for typed child delegation, UUID identities, and publication rules.
The MCP process is an
HTTP client, so several MCP clients can share one daemon. It never takes local
storage ownership or grants extra permissions.

The [native Pi extension](../packages/pi/README.md) uses direct typed HTTP calls,
commands, session hooks, provenance renderers, and visible world selection.
Its client is also exported as `memory-worlds-pi/client` for other native Pi code.
It does not need MCP or a Pi fork. Automatic capture is off by default;
publication is an explicit user slash command. See [sharing](sharing.md) for
remote mount configuration, OIDC membership, and outbox receipts.

Use [discovery](../README.md#discover-worlds-scopes-and-mounts) to find canonical IDs
and aliases. For native user login and team mappings, see [OIDC and Okta](oidc-okta.md).
