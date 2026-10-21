# Memory Worlds

A shared memory service for agents. Run one local daemon per user, give each agent its
own credential, and share durable memory across sessions and machines through
HTTP or SSH forwarding. Mount private and shared worlds independently from the
world that receives new evidence.

The service includes per-world SQLite source storage, authenticated HTTP,
a Python client, CLI, optional stdio MCP, native Pi tools and hooks, remote
shared-space routing, an OIDC shared service, ChatGPT/Pi import, portable export
and restore, and an opt-in Graphiti/FalkorDB knowledge graph. Source search works
without a graph or model provider. Graph extraction has separate delivery state;
a capture receipt establishes durable source storage.

Run the same service locally, through SSH, or as a single-replica Kubernetes
StatefulSet with retained volumes. The [router and identity guide](docs/sharing.md),
[native Pi package](packages/pi/README.md), and [deployment guide](docs/deployments.md)
cover those configurations. The optional Neptune/OpenSearch adapter is experimental;
the local and Kubernetes graph backend is FalkorDB.

## Run locally

Linux and macOS are the initial targets. The daemon uses Unix file locking.
Use current uv; `.python-version` selects stable Python 3.15. The lockfile records
exact dependencies, including Graphiti 0.31.2 and MCP 2.3.0.

```sh
uv sync --locked --extra graph --extra mcp
umask 077
mkdir -p ~/.config/memory-worlds
.venv/bin/memory-worlds init --user alice > ~/.config/memory-worlds/bootstrap.json
python3 - <<'PY'
import json
from pathlib import Path
folder = Path.home() / ".config/memory-worlds"
bootstrap = json.loads((folder / "bootstrap.json").read_text())
(folder / "admin.token").write_text(bootstrap["token"] + "\n")
print("Personal world:", bootstrap["world"]["id"])
PY
.venv/bin/memory-worlds serve
```

Keep the bootstrap output private: it contains the initial administrative
credential. Runtime data defaults to `~/.local/share/memory-worlds`, outside the
repository. `--data-dir PATH` selects another local directory. `init` refuses to
overwrite an initialized authority. The daemon binds `127.0.0.1:8787`; a second
daemon cannot take the same directory. Starting the daemon alone does not create
an owner credential.

In a second terminal, replace `WORLD_ID` with the printed ID:

```sh
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token worlds
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token \
  token-create --user alice --agent editor --grant WORLD_ID:writer
```

Save the returned token in a private file for that agent. The response includes
its credential ID for `token-revoke`. Agent roles are `reader`, `writer`, and
`manager`; all can read, while writer/manager can contribute. Administration is
a separate credential capability. Use routine agent credentials for agents.

```sh
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/editor.token \
  capture WORLD_ID --source-id session-42-entry-7 \
  --at 2026-10-20T12:00:00Z --text 'The fixture spindle uses a ceramic bearing.'
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/editor.token \
  search 'ceramic bearing' --world WORLD_ID
```

Repeat the same source ID, timestamp, content, and metadata on retries. The
service returns the original receipt. Reusing an ID with changed input returns
HTTP 409. Capture defaults to source-only; `--project` queues graph extraction.
Source search performs case-insensitive matching of every query word against
source text. `search --graph` explicitly requests graph retrieval.

The HTTP contract is at `/docs` and `/openapi.json`, with an inspectable
[OpenAPI artifact](docs/openapi.json) in the repository. The [Python client
example](docs/agents.md) and MCP adapter use that same service. `/healthz` checks
the HTTP process; `/readyz` also checks that required background workers are running.

## Worlds and private data

An administrator creates worlds with `world-create NAME --kind KIND [--owner USER]`.
Kinds are `personal`, `private-work`, `team`, `company`, and `ad-hoc`. Labels do
not confer permissions. Each credential has explicit grants. Private-world
grants must name the owning user; every source lookup and search enforces them.
Authority administrators control the local files and can access all worlds.
OIDC sessions use live user membership. Local agent credentials retain only
their explicit grants; a user membership never widens an agent's credential.

Ordinary capture goes to a selected private world. Shared worlds require the
separate `publish` command with a destination and selected text. The publication
request cannot dereference a private source or attach private transcript links.
Reading a shared world never changes an agent's capture destination.

For remote shared spaces, `mounts` lists operator-configured authorities,
`route-search QUERY --mount MOUNT` reads them, and `route-publish MOUNT WORLD
--source-id ID --at TIMESTAMP --text TEXT` durably queues selected text.
`deliveries` distinguishes pending, delivered, and blocked publications. Reads
never enqueue a publication. Mounts exclude remote private worlds; agents reach
their own private memory directly through the authenticated API or SSH.

All client requests select their worlds explicitly. One agent changing its
selection does not change another agent's context. Mixed personal/work context
can still be copied by an agent into text it later saves; storage permissions
alone do not enforce information flow inside a model context.

## Remote agents over SSH

From a machine running an agent, forward a local port to the memory host:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8788:127.0.0.1:8787 user@memory-host
```

Configure the remote agent with `MEMORY_WORLDS_URL=http://127.0.0.1:8788` and its
own token, or use CLI `--url`. Application authentication remains required
through the tunnel. Raw graph ports are never needed by agent clients.

If the daemon runs on your laptop and the agent runs on a remote compute node,
run this on the laptop instead:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -R 127.0.0.1:8788:127.0.0.1:8787 user@compute-node
```

The compute-node agent then uses `http://127.0.0.1:8788`. Forwarding requires the
SSH host's forwarding policy to permit it. For a public/network endpoint,
configure authenticated TLS ingress and explicitly enable non-loopback binding;
the local setup does not require opening a public listener.

## Import and portable recovery

Import is a local administrative operation using `--data-dir` and an admin
token. It defaults to source-only, even if models are configured.

```sh
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token \
  import-chatgpt WORLD_ID /private/path/conversations.json
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token \
  import-pi WORLD_ID /private/path/session.jsonl
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token \
  export WORLD_ID /private/backups/world.mworld
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token \
  restore NEW_WORLD_ID /private/backups/world.mworld
```

The ChatGPT importer accepts a conversation object or list of conversation
objects, preserves original roles/times/IDs/parents, and selects the active branch
by default. Pi JSONL uses the current leaf; `--all-branches` imports alternatives
without splicing them into a conversation. Extending an active-branch import to
all branches is idempotent. Unsupported parts are retained/reported as source
metadata or references; referenced attachment bytes are not imported.

An archive contains a versioned manifest and checksummed source/audit records,
with sanitized model configuration and no service credentials or memberships.
Restore preserves source identity and origin attribution while binding new
records to the importing authority and user. Conflicts roll back the entire
batch. Restoring an identical artifact again replays its imported records.
Restore into a new world for an independent copy; an existing source with
different provenance or projection intent is a conflict.

Archives recover evidence. Re-extraction into Graphiti may produce different
facts and entities; `.mworld` is not an exact graph-engine snapshot. Limits per
import/archive are 128 MiB input, 256 MiB uncompressed archive data, 2 MiB per
record, and 100,000 episodes. Split larger histories into bounded inputs.

For OneDrive or another file-sync tool, sync completed `.mworld` artifacts.
Exports stage a `.pending` file on the destination filesystem, then atomically
publish the final name without overwriting a previous artifact. A cloud client
may see staging files: exclude `*.pending` in the sync tool where possible, and
consume only finalized `.mworld` files. These archives are plaintext. Keep live
SQLite/WAL/FalkorDB directories outside synced folders. Export is a snapshot,
not bidirectional multi-writer database synchronization.

`backup WORLD_ID /private/backups/source.sqlite3` creates a consistent native
SQLite source backup while capture continues. It is an administrator recovery
artifact; the `restore` command accepts portable `.mworld` archives. See
[storage and recovery](docs/storage.md) for backend boundaries.

## Enable the graph

Start a local persistent FalkorDB 6.0 instance:

```sh
export MEMORY_WORLDS_FALKORDB_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose -f deploy/compose.graph.yaml up -d
```

The graph-only Compose file exposes its database port on loopback `16379`. The
host daemon uses `MEMORY_WORLDS_FALKORDB_PORT=16379`. It is not an agent endpoint.
Keep that generated password in your private configuration and supply it to
both the backend and daemon. Compose refuses to start without it. Loopback
binding alone would allow other users on the host to access an unprotected
graph port. The deployment requires 64 hex characters and writes its private
config into tmpfs so the password is absent from process arguments. Configure
extraction and embeddings before starting the daemon:

```sh
export MEMORY_WORLDS_GRAPH_BACKEND=falkordb
export MEMORY_WORLDS_FALKORDB_PORT=16379
export MEMORY_WORLDS_LLM_PROVIDER=openai-compatible
export MEMORY_WORLDS_LLM_MODEL=your-extraction-model
export MEMORY_WORLDS_LLM_BASE_URL=http://127.0.0.1:9000/v1
export MEMORY_WORLDS_EMBEDDING_PROVIDER=openai-compatible
export MEMORY_WORLDS_EMBEDDING_MODEL=your-embedding-model
export MEMORY_WORLDS_EMBEDDING_BASE_URL=http://127.0.0.1:9001/v1
export MEMORY_WORLDS_EMBEDDING_DIMENSIONS=1536
.venv/bin/memory-worlds serve
```

Those model names/endpoints are placeholders for servers you operate. Providers
must support the Graphiti structured-output/extraction and embedding contracts.
Alternatively select `openai` for each provider, choose models, and supply
`MEMORY_WORLDS_LLM_API_KEY` and `MEMORY_WORLDS_EMBEDDING_API_KEY` or `OPENAI_API_KEY`.
Model processing occurs at those configured providers. Graphiti telemetry is
disabled by default. See [.env.example](.env.example) for all options.

Capture with `--project` to enqueue extraction. `status WORLD_ID` distinguishes
disabled, pending, processing, projected, and unknown records. Graph mutations
serialize within a world; independent worlds can progress concurrently.
An interrupted or failed write may have changed the backend: the world becomes
degraded and graph reads stop while source capture/search continue. Stop the
daemon and repair explicitly:

```sh
.venv/bin/memory-worlds --token-file ~/.config/memory-worlds/admin.token \
  graph-rebuild WORLD_ID
```

Add `--include-unprojected` to explicitly send source-only imports to the
configured model providers. A rebuild creates an isolated graph generation and
activates it only after success. Changed model/embedding configuration requires
a rebuild. Previous generations remain stored; automatic garbage collection
and source erasure are not implemented in this release.

## Container and checks

The [Helm chart](deploy/helm/memory-worlds/README.md) deploys the API and optional
FalkorDB with separate retained volumes, existing Secrets, readiness checks,
release-scoped networking, and optional TLS ingress. See the
[installation and upgrade guide](docs/helm.md) for source-only, graph, shared
OIDC/router, and experimental Neptune profiles.

The private [GitHub repository](https://github.com/josephjohncox/memory-worlds)
builds the Python wheel/source archive, Pi package, and Helm archive together,
with a manifest and SHA-256 checksums. After CI passes, it builds a private
multi-platform container identified by the tested commit and registry digest.
See [builds and artifact access](docs/releases.md) for local builds and downloads.

The API can run in a container with persistent local storage:

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps api init --user alice
docker compose -f deploy/compose.yaml up -d
```

Save the bootstrap credential securely. The image uses Python 3.15, current
pinned uv, and the locked application dependencies. Its API port is published
only on host loopback. The graph-only file above is a separate deployment for
a host daemon; it does not automatically configure a containerized API.

```sh
uv run --frozen --all-extras pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
MEMORY_WORLDS_RUN_FALKORDB_TEST=1 MEMORY_WORLDS_TEST_FALKORDB_PORT=16379 \
  uv run --frozen --all-extras pytest tests/test_graph.py -q
```

The live graph test uses real Graphiti and FalkorDB with deterministic extraction
and embedding test clients. It proves backend wiring and scoping, not real-model
recall quality. See [validation](docs/validation.md) for executed checks.

Private conversation exports, runtime data, credentials, and local instructions
are excluded from package and container inputs. Public fixtures are synthetic.
The Python library and native Pi package are licensed under [Apache-2.0](LICENSE).
Packages have not been published to public registries.
