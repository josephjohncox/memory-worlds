# Memory Worlds documentation

Memory Worlds runs as a local service for one user's agents or as a shared
service with private work contexts and team/company worlds. Start with the
installation path that matches your use, then add clients and shared access.

## Start here

- [Guided local or hybrid setup](setup.md): mise bootstrap, one TOML config, wizard, background service and diagnostics.
- [Manual local setup](manual-setup.md): initialize the daemon and issue agent credentials yourself.
- [Local and corporate memory](hybrid.md): keep personal memory local and read selected shared worlds.
- [Pi extension](../packages/pi/README.md): installation, configuration and first use.
- [Coding-agent integrations](clients.md): Pi, Codex, Claude Code and OpenCode connections from one applied setup.
- [Agent clients](agents.md): Python, CLI and MCP access.
- [Discovery](discovery.md): find authorized worlds, scopes, aliases and mounts.

## Work with memory

| Topic | Guide |
| --- | --- |
| Worlds, scopes, credentials and the source/graph boundary | [Architecture](architecture.md) |
| Agent identity, delegation and subagent scratch memory | [Scopes](scopes.md) |
| Remote agents and laptop-to-compute tunnels | [SSH access](remote-access.md) |
| ChatGPT/Claude exports and document snapshots | [Directory import](chat-import.md) |
| Per-chat private, review and automatic sharing | [Chat sharing](chat-sharing.md) |
| Shared authorities, mounts and publication delivery | [Routing and sharing](sharing.md) |
| Graph providers, extraction state and rebuilds | [Graphiti operations](graphiti.md) |
| Portable source snapshots and file-sync folders | [Portable archives](portable-archives.md) |
| Retraction, erasure, graph cleanup and retention | [Lifecycle](lifecycle.md) |

## Run a shared service

Choose a [deployment topology](deployments.md), then follow the [Helm guide](helm.md)
or the [PostgreSQL/RDS guide](rds-postgresql.md). Declare spaces, role profiles and
group bindings in [company/team access configuration](corporate-access.md), then
configure [Okta](oidc-okta.md) or your OIDC provider. The [storage guide](storage.md) explains what lives
in each backend; [authority recovery](recovery.md) covers encrypted backups,
restore and completed-artifact transport.

## Reference and development

Configuration and detailed behavior live in the [reference index](reference/README.md).
For contributor work, use [development tasks](development.md) and the
[source map](implementation.md). [Local qualification](qualification.md) describes
repeatable infrastructure checks; [validation](validation.md) records executed
results. [Builds and artifacts](releases.md) covers distributions and container
receipts.

Design documents and implementation plans are retained under
[specifications](superpowers/specs/) and [plans](superpowers/plans/). They record
decisions and proposals; the task guides describe the current implementation.
