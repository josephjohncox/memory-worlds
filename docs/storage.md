# Storage and recovery

Memory Worlds uses SQL journals as the durable source and Graphiti/FalkorDB for
extraction and graph retrieval. Select SQLite for a local authority or PostgreSQL
for a shared authority with several API replicas. PostgreSQL replaces the
control, source, and publication journals; it does not replace the graph backend.

## Local SQLite

One daemon owns the data directory. Agents and SSH clients use that daemon's API.
The daemon lock prevents another local process from opening the same authority
as a second server. Keep the directory on a local filesystem, outside a sync
folder or network filesystem.

The journals are:

| Journal | Contents |
| --- | --- |
| `control.sqlite3` | Authority identity, worlds, scopes, named identities, token hashes, OIDC sessions, memberships, grants, worker leases, generation inventory |
| `worlds/<world-id>/source.sqlite3` | The world-shared scope's episodes, audit, projection jobs and state, lifecycle tombstones and purge intents |
| `worlds/<world-id>/scopes/<scope-uuid>/source.sqlite3` | The corresponding private or run scope's source journal |
| `router.sqlite3` | Explicit publication envelopes, idempotency identities and remote delivery receipts |

Directories are private to their OS owner and database files have mode `0600`.
Connections use WAL, synchronous `FULL`, foreign keys, and secure deletion of
freed SQLite cells. Lifecycle maintenance truncates the WAL after committed
source erasure. These operations do not erase previous filesystem snapshots,
backups, storage-device history, or information already published elsewhere.

## Shared PostgreSQL

Set `MEMORY_WORLDS_STORAGE_BACKEND=postgres`, `MEMORY_WORLDS_DATABASE_URL`, and a fixed
`MEMORY_WORLDS_AUTHORITY_ID` UUID for every replica of one authority. All replicas
must also use the same graph endpoint, extraction configuration, OIDC policy,
and mount configuration. An invalid PostgreSQL configuration fails startup.

SQLAlchemy Core and Psycopg 3 execute the current schema directly. Every table
reference has an explicit schema; `search_path` does not select an authority or
scope. PostgreSQL schemas separate the authority control tables, authority
outbox, and each UUID scope's source tables. Scope schemas are derived from the
authority and scope IDs. Source schemas create independent table/index sets, so
capacity planning must include the number of scopes as well as episode volume.

A database role with schema ownership can inspect its tables. Agent isolation is
enforced by live service authorization and scoped API selection, not by giving
agents SQL accounts. The service exposes no arbitrary SQL, Cypher or graph-key
interface. See [scope rules](scopes.md) and [RDS configuration](rds-postgresql.md).

Nested resource sessions share one PostgreSQL transaction. This allows source,
control and outbox mutations to participate in one fenced commit. Control
operations serialize authority decisions; source write transactions serialize
per scope. PostgreSQL sequences are restored explicitly when recovering rows.
There are no historical schema readers or automatic migrations. Development
schema changes require disposable data to be reset.

## Worker ownership and graph generations

Each scope projection and each outbox envelope has a database lease with an
owner, a monotonic fencing counter, and a deadline measured by the database's
clock. Workers renew leases while external calls run. Another replica does not
reset live processing jobs at startup. Every durable completion checks the
current fence before changing its receipt or activating a generation.

A dispatched graph write cannot be recalled by a SQL lock. If ownership expires
while graph delivery is in flight, the next owner marks that delivery unknown
and blocks the affected projection. Rebuild creates a fresh namespace; the stale
worker cannot activate its earlier namespace. Namespace inventory is recorded
before external writes and retains failed and retired generations for recovery
and erasure. Graph reads also acquire scope ownership so another replica's
partially applied incremental write cannot be returned as a ready projection.

Remote publications have immutable envelopes and source IDs. An expired delivery
may retry the exact envelope; a lost acknowledgement does not authorize a new
publication identity. Terminal retention can remove envelope content while
preserving its deduplication identity. A retained identity cannot be retried.

Authority maintenance blocks new operations and drains in-flight graph calls.
Ordinary PostgreSQL SQL sessions use a transaction-level shared lock on their
own connection. Graph calls retain a separate session-level lock across network
operations, with bounded fail-fast concurrency. Both conflict with the authority's
exclusive maintenance lock. SQLite uses the local daemon's
barrier. Ordinary requests fail promptly with `503` while maintenance is pending
or active. Asynchronous maintenance waits without blocking the event loop.
Lifecycle graph purge also requires explicit worker-drain attestation: a remote
request already dispatched by a failed process may outlive that process's lease.
See [lifecycle and retention](lifecycle.md).

## Exports and authority backups

A `.mworld` export contains the selected scope's evidence and attributed source
audit, without live credentials or membership policy. Import requires an
explicit authorized destination and queues Graphiti extraction by default.
Extraction may produce a different interpretation under a new model. An export
is neither encrypted nor signed; its checksums detect corruption.

An authority backup preserves the current control/source/outbox tables and,
when native graph mode is selected, the inventoried FalkorDB graph keys. The
backup takes an exclusive authority maintenance cut across all replicas and
external graph operations. Its AES-256-GCM archive is authenticated before any
archive members are parsed during recovery. Keep the encryption key separately
from the artifact. Recovery targets a fresh stopped authority; native graph
recovery additionally requires an isolated empty graph backend and the recorded
compatible backend version. Rebuild-mode backups restore evidence and queue
extraction instead of claiming an exact graph copy.

Use `authority-backup` to request/download a completed encrypted artifact and
`authority-restore` for offline recovery. The former per-world native SQLite
backup command is absent: a single source journal omits authority policy and
private scopes. Coordinate database/PVC/provider snapshots with the same
maintenance boundary if using external backup tooling.

FalkorDB has a separate persistent volume. AOF reduces graph loss after a
restart; it is not an off-host backup. Failed or unknown graph delivery leaves
source evidence usable. Native snapshots preserve the exact selected graph key;
recovery never renames a graph key or replaces an existing graph. Neptune
remains experimental and complete erasure is unavailable because its secondary
OpenSearch documents also require verified removal.

Completed exports and encrypted backups may be copied to a OneDrive folder,
S3 or another storage provider. Publish only completed artifacts; exclude
staging files from file synchronization. Never synchronize open database files
or an active graph data directory. Replicated SQL and a PVC improve availability
and durability in different ways; neither supplies a tested recovery procedure
without an actual restore exercise.
