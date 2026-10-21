# Memory Worlds

Persistent memory for agents, with private work and shared knowledge in separate
scopes. Run one local service for your agents, connect remote agents through SSH,
or connect your local service to team and company worlds.

Agents use authenticated HTTP, a Python client, the CLI, optional MCP, or the
native Pi extension. SQLite stores local source journals; PostgreSQL supports
shared deployments. Graphiti extracts and retrieves knowledge through FalkorDB.
Source receipts record durable storage, with graph extraction tracked separately.

## Run locally

Install [mise](https://mise.jdx.dev/installing-mise.html), a host Python 3, and
Docker with Compose. Linux and macOS are supported. From a reviewed checkout:

```sh
mise trust
python3 scripts/bootstrap.py
```

The wizard asks for local or hybrid setup, extraction and embedding providers,
private paths and client profiles. It saves one TOML configuration and can apply
it after review. Start the configured installation with:

```sh
mise run setup -- apply
mise run setup -- run
```

The API binds to loopback. Agents use their own generated credentials; the local
operator credential stays with you. Ctrl-C stops the API and retains the graph.
Model processing uses the providers you configure.

See [guided setup](docs/setup.md) for custom config paths, scripted setup and
diagnostics. For direct control of initialization and credentials, use
[manual setup](docs/manual-setup.md).

## Choose what an agent can remember

A **world** organizes personal, private-work, team, company or ad-hoc knowledge.
Within each world, a **scope** defines the intended audience:

| Scope | Use |
| --- | --- |
| World-shared | Knowledge for the world's authorized readers |
| Run-shared | Context shared within one agent run |
| Agent-persistent | One agent's memory across runs and chats |
| Invocation-private | Scratch memory for one invocation or delegated subagent |

Credentials and grants enforce access. Names, aliases and UUIDs identify objects;
they grant no permissions. Read selections and the write destination are separate,
so reading team knowledge does not move private capture into a shared world.

Publication is explicit. A user can also enable a per-chat review or automatic
sharing rule for selected information, with exclusions. See [agent scopes](docs/scopes.md),
[local and corporate memory](docs/hybrid.md), and [chat sharing](docs/chat-sharing.md).

## Use and deploy

| Task | Guide |
| --- | --- |
| Add native Pi tools, hooks and persistent agent contexts | [Pi extension](packages/pi/README.md) |
| Connect Python, CLI or MCP clients | [Agent clients](docs/agents.md) |
| Discover accessible worlds, scopes and mounts with an ordinary token | [Discovery](docs/discovery.md) |
| Connect agents on another machine | [SSH access](docs/remote-access.md) |
| Import extracted ChatGPT/Claude chats or exported documents | [Directory import](docs/chat-import.md) |
| Export memory or sync a completed backup to cloud storage | [Portable archives](docs/portable-archives.md) and [authority recovery](docs/recovery.md) |
| Deploy a shared service with Helm, kind or PostgreSQL/RDS | [Deployments](docs/deployments.md) |
| Configure Okta and team group mappings | [OIDC and Okta](docs/oidc-okta.md) |

The [documentation home](docs/README.md) links the architecture, configuration
references, operating procedures and contributor guides. The HTTP API is available
at `/docs` and `/openapi.json` on a running service; a checked-in
[OpenAPI artifact](docs/openapi.json) is also available.

## Develop

```sh
mise install --locked
mise run deps
mise run check
```

See [development](docs/development.md) for the locked toolchain and test tasks,
[builds and artifacts](docs/releases.md) for Python/Pi/Helm packages and container
images, and [validation](docs/validation.md) for recorded checks and their limits.

Licensed under [Apache-2.0](LICENSE).
