# Source map

[Documentation](README.md) · [Development](development.md) · [Architecture](architecture.md)

The Python package contains the service, clients and local setup controller.
The native Pi package calls the same HTTP API. Start with the boundary you are
changing, then follow its current-contract tests.

| Boundary | Source | Tests and guidance |
| --- | --- | --- |
| HTTP contract and validation | [api.py](../src/memory_worlds/api.py), [models.py](../src/memory_worlds/models.py), [OpenAPI](openapi.json) | `tests/test_api.py`, [agent clients](agents.md) |
| Durable sources and credentials | [store.py](../src/memory_worlds/store.py), [persistence.py](../src/memory_worlds/persistence.py), [postgres.py](../src/memory_worlds/postgres.py) | [Storage](storage.md) |
| Scope authorization and delegated contexts | [scopes.py](../src/memory_worlds/scopes.py), [identity_names.py](../src/memory_worlds/identity_names.py) | `tests/test_scopes.py`, [scopes](scopes.md) |
| OIDC and native login | [auth.py](../src/memory_worlds/auth.py), [login.py](../src/memory_worlds/login.py) | [OIDC and Okta](oidc-okta.md), [discovery](discovery.md) |
| Graph extraction and snapshots | [graph.py](../src/memory_worlds/graph.py), [graph_snapshots.py](../src/memory_worlds/graph_snapshots.py) | `tests/test_graph.py`, [Graphiti operations](graphiti.md) |
| Remote mounts and delivery | [mounts.py](../src/memory_worlds/mounts.py), [router.py](../src/memory_worlds/router.py), [outbox.py](../src/memory_worlds/outbox.py) | [Routing and sharing](sharing.md) |
| Import and portable archives | [directory_import.py](../src/memory_worlds/directory_import.py), [chat_formats.py](../src/memory_worlds/chat_formats.py), [portability.py](../src/memory_worlds/portability.py) | [Directory import](chat-import.md), [portable archives](portable-archives.md) |
| Authority recovery and lifecycle | [recovery.py](../src/memory_worlds/recovery.py), [recovery_archive.py](../src/memory_worlds/recovery_archive.py), [lifecycle.py](../src/memory_worlds/lifecycle.py) | [Recovery](recovery.md), [lifecycle](lifecycle.md) |
| Local configuration, provisioning and runtime | `src/memory_worlds/setup_*.py`, [bootstrap.py](../scripts/bootstrap.py) | `tests/test_setup_*.py`, [guided setup](setup.md) |
| Python, CLI and MCP clients | [client.py](../src/memory_worlds/client.py), [cli.py](../src/memory_worlds/cli.py), [mcp.py](../src/memory_worlds/mcp.py) | [Agent clients](agents.md) |
| Native Pi | [packages/pi/src](../packages/pi/src/) | `packages/pi/test`, [Pi package](../packages/pi/README.md) |
| Containers and Kubernetes | [deploy](../deploy/), [tasks](../tasks/) | [Deployments](deployments.md), [qualification](qualification.md) |

[AGENTS.md](../AGENTS.md) defines the development contract: one current schema and
API, explicit audiences, synthetic public fixtures, and private runtime data kept
out of artifacts. Design records live under [specifications](superpowers/specs/)
and [plans](superpowers/plans/); see [validation](validation.md) for executed checks.
