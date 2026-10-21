# Graphiti configuration and operations

[Documentation](README.md)

Graphiti extracts entities and relationships and performs graph retrieval.
FalkorDB stores the graph; SQLite or PostgreSQL stores the durable source evidence
and work state. The [guided setup](setup.md) configures these components for a
local installation. This guide covers direct runtime configuration and repair.

## Configure models and FalkorDB

Configure the private `.env` before starting the daemon. The backend-only Compose
service publishes FalkorDB on loopback port `16379` for a host daemon. Use one
random 64-hex-character database password in that file. Mise loads it with
redaction; keep the file mode `0600` and outside version control.

```dotenv
MEMORY_WORLDS_GRAPH_BACKEND=falkordb
MEMORY_WORLDS_FALKORDB_PORT=16379
MEMORY_WORLDS_LLM_PROVIDER=openai-compatible
MEMORY_WORLDS_LLM_MODEL=your-extraction-model
MEMORY_WORLDS_LLM_BASE_URL=http://127.0.0.1:9000/v1
MEMORY_WORLDS_EMBEDDING_PROVIDER=openai-compatible
MEMORY_WORLDS_EMBEDDING_MODEL=your-embedding-model
MEMORY_WORLDS_EMBEDDING_BASE_URL=http://127.0.0.1:9001/v1
MEMORY_WORLDS_EMBEDDING_DIMENSIONS=1536
```

```sh
mise run graph:backend-up
mise run cli -- serve
```

Those model names/endpoints are placeholders for servers you operate. Providers
must support the Graphiti structured-output/extraction and embedding contracts.
Alternatively select `openai` for each provider, choose models, and supply
`MEMORY_WORLDS_LLM_API_KEY` and `MEMORY_WORLDS_EMBEDDING_API_KEY` or `OPENAI_API_KEY`.
Model processing occurs at those configured providers. Graphiti telemetry is
disabled by default. See [.env.example](../.env.example) for all options.

## Check extraction and rebuild

Capture, publication, import, and restore enqueue extraction by default.
`status WORLD_ID` distinguishes disabled, pending, processing, projected, and
unknown records. Use `--scope SCOPE_UUID` to inspect a specific private or shared
scope. Extraction and retrieval use isolated scope namespaces; work ownership
and generation activation are coordinated through the durable journal.
An interrupted or failed write may have changed the backend: the scope becomes
degraded and graph reads stop while source capture/search continue. Stop the
daemon and repair explicitly:

```sh
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  graph-rebuild WORLD_ID --scope SCOPE_UUID
```

Rebuild normally includes every source. Use `--projected-only` to restrict a
rebuild to sources already selected for extraction. A rebuild creates an isolated
graph generation and activates it only after success. Changed model/embedding configuration requires
a rebuild. Explicit bounded retention retires old generations and terminal
delivery payloads. Lifecycle actions retract or erase source evidence, retain
replay tombstones, and queue tracked namespace cleanup. Final graph purge
requires drained writers. See [lifecycle and retention](lifecycle.md).

For graph actions available to agents, see [Pi Graphiti coverage](pi-graphiti-features.md).
For shared storage, see [storage](storage.md) and [Helm installation](helm.md).
