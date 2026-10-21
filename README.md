# Memory Worlds

A shared memory service for agents. Run one local daemon per user, give each agent its
own credential, and share durable memory across sessions and machines through
HTTP or SSH forwarding. Select private and shared read scopes independently from
the destination for new evidence.

The service includes SQLite local journals and PostgreSQL shared journals, authenticated HTTP,
a Python client, CLI, optional stdio MCP, native Pi tools and hooks, remote
shared-space routing, an OIDC shared service, ChatGPT/Claude directory import, Pi import, portable export
and restore, and Graphiti/FalkorDB extraction and recall by default. Explicit
source retrieval remains available for recovery and inspection. Graph extraction has separate delivery state;
a capture receipt establishes durable source storage.

Run the same service locally, through SSH, as a single-daemon Kubernetes
StatefulSet with SQLite volumes, or as PostgreSQL-backed API replicas. The [router and identity guide](docs/sharing.md),
[native Pi package](packages/pi/README.md), and [deployment guide](docs/deployments.md)
cover those configurations. The optional Neptune/OpenSearch adapter is experimental;
the local and Kubernetes graph backend is FalkorDB.

For a personal daemon alongside a corporate service, follow the
[local and corporate memory guide](docs/hybrid.md).

## Run locally

Use the [guided local/hybrid setup](docs/setup.md) for a terminal wizard or a
scripted TOML configuration. The manual steps below remain available.

Linux and macOS are the initial targets. The daemon uses Unix file locking.
Use [mise](https://mise.jdx.dev/) to install the pinned toolchain and run tasks.
The checked-in mise, uv, and npm lockfiles record the tool and library versions.
See [development tasks](docs/development.md). Configure explicit extraction and
embedding models in a private `.env` before starting the service.

```sh
mise trust
mise install
mise run deps
cp .env.example .env
chmod 600 .env
# Fill the model/provider settings and a random 64-hex FalkorDB password.
# The example uses host model endpoints; change them to your actual providers.
mise run graph:backend-up
umask 077
mkdir -p ~/.config/memory-worlds
mise run cli -- init --user alice > ~/.config/memory-worlds/bootstrap.json
mise exec -- python - <<'PY'
import json
from pathlib import Path
folder = Path.home() / ".config/memory-worlds"
bootstrap = json.loads((folder / "bootstrap.json").read_text())
(folder / "admin.token").write_text(bootstrap["token"] + "\n")
print("Personal world:", bootstrap["world"]["id"])
PY
mise run cli -- serve
```

Keep the bootstrap output private: it contains the initial administrative
credential. Runtime data defaults to `~/.local/share/memory-worlds`, outside the
repository. `--data-dir PATH` selects another local directory. `init` refuses to
overwrite an initialized authority. The daemon binds `127.0.0.1:8787`; a second
daemon cannot take the same directory. Starting the daemon alone does not create
an owner credential.

In a second terminal, replace `WORLD_ID` with the printed ID:

```sh
mise run cli -- --token-file ~/.config/memory-worlds/admin.token worlds
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  token-create --user alice --agent editor --grant WORLD_ID:writer
```

Save the returned token in a private file for that agent. The response includes
its credential ID for `token-revoke`. Agent roles are `reader`, `writer`, and
`manager`; all can read, while writer/manager can contribute. Administration is
a separate credential capability. Use routine agent credentials for agents.

```sh
mise run cli -- --token-file ~/.config/memory-worlds/editor.token \
  capture WORLD_ID --source-id session-42-entry-7 \
  --at 2026-10-20T12:00:00Z --text 'The fixture spindle uses a ceramic bearing.'
mise run cli -- --token-file ~/.config/memory-worlds/editor.token \
  search 'ceramic bearing' --world WORLD_ID
```

Repeat the same source ID, timestamp, content, and metadata on retries. The
service returns the original receipt. Reusing an ID with changed input returns
HTTP 409. Capture queues Graphiti extraction by default; recall defaults to graph
retrieval. `search --source` explicitly inspects the durable source journal, while
`capture --source-only` deliberately skips extraction for that record. A failed
graph query reports its error and never silently changes retrieval modes.

The HTTP contract is at `/docs` and `/openapi.json`, with an inspectable
[OpenAPI artifact](docs/openapi.json) in the repository. The [Python client
example](docs/agents.md) and MCP adapter use that same service. `/healthz` checks
the HTTP process; `/readyz` also checks that required background workers are running.

## Discover worlds, scopes, and mounts

Discovery uses an ordinary user or agent token. On a corporate service, sign in
with Okta/OIDC and use that API access token; no administrator token or preknown
world IDs are required. Results contain only the caller's accessible objects.
See [user and harness discovery](docs/discovery.md) for corporate login and Pi/MCP setup.

```sh
mise run cli -- --token-file ~/.config/memory-worlds/editor.token discover --scopes
mise run cli -- --token-file ~/.config/memory-worlds/editor.token worlds --table
mise run cli -- --token-file ~/.config/memory-worlds/editor.token mounts --table
mise run cli -- --token-file ~/.config/memory-worlds/editor.token discover --world WORLD_ID --json
```

Worlds and scopes have display names and optional lowercase aliases. Use
`@alias` in local CLI selections; IDs remain the canonical values in API requests,
contexts, graph namespaces, and authorization. Aliases resolve only among visible
objects. Multiple visible matches require an explicit ID. Scope aliases require a
manage grant; world aliases require the operator. `--select @personal:@notes`
selects one world/scope pair. Mounted worlds retain IDs from their remote authority.
An operator can assign a world alias with `world-alias WORLD_ID personal`;
ordinary users then resolve the visible alias with `resolve world @personal`.

## Worlds and private data

An administrator creates worlds with `world-create NAME --kind KIND [--owner USER]`.
Kinds are `personal`, `private-work`, `team`, `company`, and `ad-hoc`. Labels do
not confer permissions. Each credential has explicit grants. Private-world
grants must name the owning user; every source lookup and search enforces them.
Authority administrators control the local files and can access all worlds.
OIDC sessions use live user membership. Local agent credentials retain only
their explicit grants; a user membership never widens an agent's credential.

Capture goes to an explicitly selected private world or private scope. Shared
destinations require a separate publication operation with selected text.
Scoped publications can link authorized source evidence without copying its
transcript. Reading a shared world never changes an agent's capture destination.
Native Pi chats also support [private, review, and automatic sharing modes](docs/chat-sharing.md).
An explicitly enabled chat rule can publish classifier-selected excerpts to one
shared destination, with category and topic exclusions and private decision records.

Worlds contain four [memory scopes](docs/scopes.md): world-shared, run-shared,
agent-persistent, and invocation-private. UUID child identities use attenuated,
expiring credentials; parents and siblings cannot read child scratch by default.
Source journals and Graphiti extraction are isolated by scope. World-level
selection addresses the world-shared scope. The native Pi package provides
[graph tools and Pi Graphiti feature coverage](docs/pi-graphiti-features.md).

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

Import previously extracted chat directories into an explicit private scope.
Preview runs locally without a token or service connection; application uses the
authenticated HTTP service and writes the entire batch in one transaction:

```sh
mise run cli -- import-directory @personal /private/chat-exports \
  --scope @history --dry-run
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  import-directory @personal /private/chat-exports --scope @history
```

Use an `agent-persistent` or `invocation-private` scope in your own personal or
private-work world. The [directory import guide](docs/chat-import.md) shows scope
creation, native ChatGPT/Claude JSON, richlow Markdown, timezone selection,
document snapshots, and format selection to avoid duplicate representations.
Graphiti extraction is queued by default; `--source-only` deliberately skips it.

The single-file import and portable recovery commands below are local
administrative operations using `--data-dir` and an admin token:

```sh
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  import-chatgpt WORLD_ID /private/path/conversations.json
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  import-pi WORLD_ID /private/path/session.jsonl
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  export WORLD_ID /private/backups/world.mworld
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
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
facts and entities; `.mworld` is not an exact graph-engine snapshot. Single-file
imports and portable archives allow 128 MiB input, 256 MiB uncompressed archive
data, 2 MiB per record, and 100,000 episodes. Directory batches have the smaller
limits documented in the [directory import guide](docs/chat-import.md).

For OneDrive or another file-sync tool, sync completed `.mworld` artifacts.
Exports stage a `.pending` file on the destination filesystem, then atomically
publish the final name without overwriting a previous artifact. A cloud client
may see staging files: exclude `*.pending` in the sync tool where possible, and
consume only finalized `.mworld` files. These archives are plaintext. Keep live
SQLite/WAL/FalkorDB directories outside synced folders. Export is a snapshot,
not bidirectional multi-writer database synchronization.

`authority-backup /private/backups/authority.mwauthority` requests an encrypted
cut of all private/shared source journals, identities, grants, tombstones, jobs,
outbox and native FalkorDB namespaces. `authority-restore` installs it into an
isolated fresh authority. An explicit `--graph-mode rebuild` instead queues
fresh extraction after recovery. Optional `mise run backup:sync` copies a verified
encrypted artifact through a named OneDrive/S3 rclone remote and checks downloaded
bytes. See [authority recovery](docs/recovery.md).

## Graphiti runtime configuration

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
disabled by default. See [.env.example](.env.example) for all options.

Capture, publication, import, and restore enqueue extraction by default.
`status WORLD_ID` distinguishes
disabled, pending, processing, projected, and unknown records. Graph mutations
serialize within a world; independent worlds can progress concurrently.
An interrupted or failed write may have changed the backend: the world becomes
degraded and graph reads stop while source capture/search continue. Stop the
daemon and repair explicitly:

```sh
mise run cli -- --token-file ~/.config/memory-worlds/admin.token \
  graph-rebuild WORLD_ID
```

Rebuild normally includes every source. Use `--projected-only` to restrict a
rebuild to sources already selected for extraction. A rebuild creates an isolated
graph generation and
activates it only after success. Changed model/embedding configuration requires
a rebuild. Explicit bounded retention retires old generations and terminal
delivery payloads. Lifecycle actions retract or erase source evidence, retain
replay tombstones, and queue tracked namespace cleanup. Final graph purge
requires drained writers. See [lifecycle and retention](docs/lifecycle.md).

## Container and checks

The [Helm chart](deploy/helm/memory-worlds/README.md) deploys the API and
FalkorDB by default with separate retained volumes, existing Secrets, readiness checks,
release-scoped networking, and optional TLS ingress. See the
[installation and upgrade guide](docs/helm.md), [Okta/OIDC configuration](docs/oidc-okta.md),
and [RDS/PostgreSQL assessment](docs/rds-postgresql.md).

The private [GitHub repository](https://github.com/josephjohncox/memory-worlds)
builds the Python wheel/source archive, Pi package, and Helm archive together,
with a manifest and SHA-256 checksums. After CI passes, it builds a private
multi-platform container identified by the tested commit and registry digest.
See [builds and artifact access](docs/releases.md) for local builds and downloads.

The API can run in a container with persistent local storage:

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps api init --user alice
mise run graph:up
```

Save the bootstrap credential securely. The image uses Python 3.15, current
pinned uv, and the locked application dependencies. Its API port is published
only on host loopback. The default Compose stack connects the API to its private FalkorDB service.
Provider URLs must be reachable from the API container. The backend-only task
is for a daemon running directly on the host.

```sh
mise run check
mise run pi:smoke
MEMORY_WORLDS_TEST_FALKORDB_PORT=16379 mise run test:graph
mise run kind:smoke
mise run artifacts:build
```

The live graph test uses real Graphiti and FalkorDB with deterministic extraction
and embedding test clients. It proves backend wiring and scoping, not real-model
recall quality. See [validation](docs/validation.md) for executed checks.

Private conversation exports, runtime data, and credentials
are excluded from package and container inputs. Public fixtures are synthetic.
The Python library and native Pi package are licensed under [Apache-2.0](LICENSE).
Packages have not been published to public registries.
