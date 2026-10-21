# Memory Worlds: local service and shared worlds

Status: approved by the user on 2026-10-20; local implementation in progress.

Public project name: Memory Worlds. Package names are provisional until registry
availability is checked. Public code, examples, and documentation are vendor
neutral and must not identify an employer or internal system.

## Purpose and delivery order

Build one Memory Worlds library and service with clients for Pi and other
agents. Start with a complete local service: multiple agents use one user's
durable memory, remote agents can connect through SSH, and the user can export
or back up a world into a cloud-synchronized directory. Then add remote work
spaces, federation, the rich Pi extension, and kind/Kubernetes deployments.

The supplied conversation export informs the requirements but remains a private
input outside the published source tree, package, container context, and test
fixtures. Its assistant messages are proposals, not verified product
documentation or accepted factual memory. Public fixtures are synthetic. This
specification updates that proposal against upstream documentation checked on
2026-10-20.

Delivery sequence:

1. Local daemon, durable episodes, world access rules, real Graphiti adapter,
   multi-agent clients, ChatGPT import, SSH instructions, export and restore.
2. Router connecting local and remote authorities, authorized shared
   publication, per-destination delivery receipts, and rich Pi integration.
3. Shared work service with OIDC, per-user work contexts, team/company/ad-hoc
   spaces, and kind/Kubernetes deployment and recovery checks.
4. Neptune/OpenSearch deployment alternative after backend compatibility and
   operating requirements are demonstrated.

Later stages remain part of the requested system. They are not prerequisites
for using the first local deliverable.

## Alternatives and recommendation

| Approach | Benefit | Cost or limitation |
| --- | --- | --- |
| SQLite journal plus Graphiti/FalkorDB | Durable local capture, temporal graph, recoverable source data, a path to shared deployment | Local graph requires a graph process and configured extraction/embedding providers |
| SQLite-only memory and explicit edges | Smallest installation and no model requirement | Defers Graphiti extraction and temporal semantic retrieval |
| Neptune-first service | Managed graph infrastructure | Requires AWS networking, identity, and OpenSearch as well as Neptune; does not solve local availability |

Recommendation: the first option. The local journal and lexical search work
without a model or graph connection. Graphiti is an actual supported adapter,
with a local Compose profile and an integration test; it is not replaced by
keyword search presented as semantic graph retrieval.

Use Python for the domain library, daemon, CLI, and Graphiti adapter. Use
TypeScript for the Pi package and its typed HTTP client. Keep them in one
workspace, sharing a versioned HTTP/OpenAPI contract. Do not introduce an
independent service for each transport or fork Pi unless a verified API gap
requires it.

## Authority, identity, and worlds

A deployment is an authority with a stable ID. A world belongs to exactly one
authority and has a stable ID, owner, kind, description, membership policy,
and lifecycle. Supported kinds are personal, private-work, team, company,
and ad-hoc. The label does not grant permissions.

Personal and company authorities are independent. Work-private data is private
to an employee within the work authority; it is not personal non-work data.
Access control does not hide a work user's data from the operators who control
the work infrastructure. Export policy can restrict work data independently
of personal data.

The local daemon normally serves one human owner and several agent clients.
Each client has its own revocable credential and attribution, with explicit
world grants and read/write capabilities. Administrative credentials are
separate from routine agent credentials. A caller cannot impersonate another
user by supplying an owner field or select a backend namespace directly.

The later work service resolves identity from verified OIDC tokens and grants
access through world memberships. Team and company membership come from
explicit configuration or mapped identity groups, not naming conventions.
Ad-hoc spaces have explicit members and no inherited company-wide access.

Read selection and write selection are independent:

- A request searches an explicit set of mounted, authorized worlds. An empty
  selection searches nothing. An unauthorized selection fails before any
  retrieval rather than silently expanding or partially searching it.
- Capture has one explicit private target. A session does not infer its write
  target from whichever worlds it read most recently.
- Shared publication uses a separate operation and an explicit destination.
  No automatic publication or dual-write is enabled by default.
- Every operation, including source lookup, graph traversal, status, export,
  import, and publication, applies authorization before reading source data.

Graphiti receives server-resolved group IDs only. Its `group_id` is a namespace
filter, not a substitute for these rules.

## Local storage and concurrent access

One daemon owns the live local storage directory. HTTP clients never open the
database files, whether they run on the same machine or another machine.
Run one daemon process for a local data directory; detect a second owner at
startup rather than rely on an undocumented multi-worker configuration.

Use a small control database for authority identity, world metadata, credential
hashes, and grants. Give each local world its own directory and SQLite source
database, containing episodes, audit events, and projection jobs. World IDs are
opaque validated identifiers; request fields never become arbitrary paths.
SQLite uses transactions, a bounded busy timeout, and WAL on a local filesystem.
Credentials and data directories use restrictive file permissions.

FalkorDB runs as a separate local process/container with persistent storage.
World namespaces are logically separate; they are not separate FalkorDB
processes or separate physical snapshot files. Where physical graph isolation
is required, assign a dedicated backend and volume. Expose only the memory API
to agent clients; keep raw graph ports private.

An episode contains a stable source identity, world ID, original occurrence
time, server receipt time, actor, originating agent/session, content, source
type, source reference, and content digest. A request's identity and digest
are scoped to its world. Replay of the same identity and content returns the
original durable receipt; reuse with different content returns a conflict.

For projection-enabled capture, ingestion commits the episode and its projection
job in one transaction before acknowledging it. Source-only capture explicitly
records that projection is disabled. The receipt distinguishes durable capture
from completed graph extraction. First-milestone corrections are new attributed
episodes that reference the prior source; they do not silently overwrite it.
Retraction and erasure are deferred until source retention, graph removal,
backups, and downstream publication behavior have one tested contract.

## Graph projection and retrieval

The graph is a derived interpretation of source evidence. Background processing
serializes mutations within each world and allows independent worlds to make
progress concurrently. Persist job attempts, provider/model configuration,
projection generation, failure state, and successful completion.

Graphiti writes are not assumed to be exactly-once merely because an episode
UUID is supplied. A worker crash after graph mutation but before local receipt
is an ambiguous outcome. The adapter must define and test reconciliation; if
the backend cannot prove a complete write, mark the generation degraded and
stop graph reads and further mutations for that world. Source capture and
lexical reads remain available. An operator-triggered rebuild reconstructs an
isolated generation from durable sources; it becomes active only after complete
success. Do not claim a retry suppressed all duplicate or partial graph writes.
Automatic repair can follow after this recovery contract is exercised.

Return separate lexical/source and graph/fact results with world, provenance,
record type, and projection status. A graph outage must be visible; the service
must not silently label lexical fallback as semantic retrieval. Do not compare
scores from different authorities as if they shared a calibrated scale.

Extraction and embeddings use configured providers. Local storage alone does
not imply that model inference is local. No provider is enabled implicitly,
and imports default to source-only capture without runnable projection jobs,
even when a provider is configured. Projecting imported data requires an
explicit subsequent opt-in operation.
Assistant statements remain attributed evidence or proposed claims; repetition
does not make them accepted decisions.

## API, transports, and SSH

The Python application service is the common implementation boundary for the
HTTP API, CLI, and MCP adapter. Publish versioned typed request/response models
and an OpenAPI schema. Expose narrow operations for listing worlds, capture,
source lookup, selected-world search, projection status, and publication.
Do not expose arbitrary Cypher, arbitrary file paths, or unrestricted upstream
Graphiti tools to agents.

Bind the local HTTP API to `127.0.0.1` by default and authenticate agent requests
even on loopback. SSH transports the same authenticated HTTP API:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8788:127.0.0.1:8787 user@memory-host
```

The remote agent uses `http://127.0.0.1:8788` and its own credential. SSH access
does not grant memory API administration. This example assumes the daemon
listens on port 8787 on `memory-host`. Document the inverse topology for a
remote agent tunneling back to a laptop when needed. Non-loopback service
deployments require an explicit configuration and authenticated TLS ingress.

Local administrative export/import and credential management are CLI operations.
An HTTP agent cannot ask the service to write a backup to an arbitrary server
filesystem path. A stdio MCP client, when provided, is a client of the daemon;
it does not start another owner of the local database.

## Import, export, backup, and cloud storage

The first deliverable imports the ChatGPT conversation JSON shape, including stable
conversation/message IDs, roles, timestamps, and branch parentage. Import the
selected active branch by default and offer an explicit all-branches mode.
Never flatten sibling branches into one conversation. Missing active pointers,
non-text parts, malformed records, and duplicate IDs have deterministic reported
handling. Retain raw records where needed to preserve unsupported content;
never claim an attachment was imported when only its reference was retained.
Pi session import follows the same normalized episode contract.

Portable world export contains a versioned manifest, world metadata, episodes,
source links, audit/correction records needed to interpret them, checksums, and
projection configuration with secrets removed. It never contains credentials,
raw OIDC tokens, or grants that silently become active on restore. Graph-derived
records can be included as a versioned sidecar; source-only recovery explicitly
requires re-extraction and may produce a different derived graph.

Build exports from a consistent read view and publish completed immutable
artifacts only. Atomic rename requires a staging path on the destination
filesystem. If the destination is a directly synchronized directory, document
that cloud clients may observe staging files; consumers accept only finalized
artifacts. For remote object storage or a cross-filesystem copy, verify the
completed upload/copy and publish its completion marker last.

Restore verifies schema and checksums before a transactional import into an
explicitly selected world. Bind restored records to the destination authority,
world, and authorized importer; retain original attribution as provenance, not
as a grant of identity. Reject conflicting identities rather than silently
overwriting data. Source ownership, credentials, and ACLs never become live
authorization on portable restore.

Provide exact SQLite backups through the online backup API. Exact graph recovery
additionally requires a tested FalkorDB snapshot procedure. Whole-backend RDB
snapshots can include several worlds and are authority-administrator operations,
not permission-scoped world exports. Pause projection workers while establishing
the snapshot checkpoint; capture may continue beyond its recorded watermark.
Record source and projection watermarks so a backup does not pretend that
independent files form one distributed transaction. Credentials are provisioned
separately on recovery.

OneDrive, rclone, S3, or similar tools may copy completed artifacts. The live
SQLite/WAL/FalkorDB directories stay outside file synchronization. The first
milestone provides the safe sync-folder output boundary, not OAuth integration
with each cloud vendor or bidirectional database merging. Backup encryption
can be delegated to a configured archival tool; plaintext output is identified
as such and is never represented as encrypted.

## Router and publication

The second stage mounts configured authorities with separate credentials.
Federated reads keep authority/world attribution and report unavailable
authorities. Durable local capture continues when a remote service is down.

Publishing is a separate request containing selected text and a destination,
with its own immutable content hash and destination-scoped delivery ID. An
outbox tracks durable acknowledgment independently for each destination. Merely
reading or mounting a shared world never publishes local content to it.

The destination authorizes the authenticated publisher. Source provenance in
the shared artifact must itself be publishable: do not include private episode
text, private transcript links, or an API that lets shared readers dereference
private sources. The local private receipt may retain a richer origin link.
Publication does not imply that a team accepted the published claim as true.

Mixing personal and work facts in one model context creates an information-flow
problem that ACLs on storage alone cannot solve. The initial extension uses
one capture authority per session and disables automatic whole-turn capture
once context from multiple authorities has entered a session. That exposure
persists through unmounting, branching, and compaction; a clean session is
required to resume automatic capture. Explicit capture/publication remains
available. This is a conservative workflow rule, not a claim of complete
information-flow control over text transformed by an LLM.

## Rich Pi extension

Use the current native Pi extension API, pinned to a verified compatible
version. It already supports tools, slash commands, pre-turn context hooks,
session lifecycle, status/widgets, custom terminal UI, and MCP registration.
Begin with a Memory Worlds extension and direct typed HTTP client; a fork is justified
only by a demonstrated missing capability or reusable upstream code.

The extension provides `/memory` status, world selection, capture target,
search, capture, and explicit publication. Show mounted worlds, capture target,
connectivity, and pending delivery state in the terminal UI. Persist world
selection per session; account for switching, branching, compaction, reload,
and cancellation. Local conversation capture uses stable session/entry IDs
and the durable ingestion API so repeated lifecycle events do not duplicate
evidence. Retrieval is bounded and source-labeled; memory content is data and
cannot silently become system instructions.

MCP is an additional transport for other agents. It does not limit the richer
Pi experience to generic tool calls or make the Pi extension the storage owner.

## Shared service and deployments

The shared work service uses the same domain/API contract. Replace local
control/source persistence with a shared durable SQL implementation when
multiple API replicas are introduced; do not mount one SQLite WAL file across
pods. OIDC verifies issuer, audience, signature, expiry, and subject. A gateway
authenticates traffic but the service enforces world authorization.

For kind, provide a complete development deployment with persistent storage,
secrets supplied out of band, health/readiness probes, and a recovery smoke
test. For a PVC deployment, FalkorDB is a StatefulSet with explicit persistence
and backup behavior. A PVC alone does not supply replication, backups, or
high availability. Treat a single replica as a single-node deployment.

For AWS, Graphiti's Neptune driver also requires OpenSearch Serverless for
search. A future deployment therefore includes Neptune, OpenSearch, IAM,
networking, and the authoritative SQL/source store; it is not a backend URL
replacement. Do not provision paid infrastructure as part of the local stage.

## Open-source packaging

Use neutral package/module names and synthetic users, domains, and worlds. Do not
copy internal instructions, private exports, local paths, source credentials, or
company-specific identity/group configuration into the public repository.
Exclude private inputs from source control, source distributions, wheels, npm
packages, and Docker build contexts. Verify the generated archives and image
context, not only the visible README. Preserve license notices for reused
upstream code. The project license and public release destination remain owner
choices; implementation can proceed without publishing or inventing ownership.

## First milestone acceptance criteria

1. A new installation starts the local daemon with documented commands and
   restrictive local defaults; a second daemon cannot take the same directory.
2. Independent agent clients capture concurrently, then retrieve each other's
   authorized records without lost writes. Restart preserves acknowledged data.
3. Duplicate submissions return the same receipt; conflicting reuse fails.
   A process crash around commit and graph delivery has a tested recovery path.
4. Client grants constrain every source/search/export operation. Personal,
   private-work, and shared-world fixtures demonstrate no cross-user leakage,
   and read access never grants capture/publication permission.
5. An SSH-forwarded client uses the same authenticated API without opening raw
   graph ports. Verify real forwarding when an SSH endpoint is available;
   otherwise record that operational check as unrun.
6. A representative ChatGPT export imports deterministically and idempotently,
   preserving active-branch identities, roles, and dates. A branching fixture
   demonstrates that alternatives are not joined into one transcript.
7. Export during writes is internally consistent; restore in an isolated
   directory passes checksum/identity checks and reproduces source records.
   Corrupt artifacts and conflicting identities are rejected without partial
   import. Cloud-folder consumers accept completed artifacts only.
8. The configured Graphiti/FalkorDB profile performs real ingestion and scoped
   graph retrieval. Graph/provider failure preserves capture, exposes pending
   status, and recovers through the documented worker/rebuild path. Any live
   model test that cannot run is reported separately from deterministic tests.

## Verified upstream references

- [Graphiti graph namespacing and authorization](https://help.getzep.com/graphiti/core-concepts/graph-namespacing)
- [Graphiti FalkorDB configuration](https://help.getzep.com/graphiti/configuration/falkordb-configuration)
- [Graphiti Neptune/OpenSearch configuration](https://help.getzep.com/graphiti/configuration/neptune-configuration)
- [Graphiti 0.31.1 ingestion implementation](https://github.com/getzep/graphiti/blob/5961293865a57844fe13d4eb061deb3501971fb2/graphiti_core/graphiti.py)
- [FalkorDB persistence and snapshot formats](https://docs.falkordb.com/operations/durability/)
- [Pi native extensions](https://pi.dev/docs/latest/extensions)
- [SQLite online backups](https://www.sqlite.org/backup.html)
- [SQLite WAL concurrency and filesystem limits](https://www.sqlite.org/wal.html)

Confidence: high in the service/authority boundaries and upstream capabilities;
moderate in projection recovery details until verified against pinned Graphiti
code and exercised in integration tests. No deployment or implementation result
is claimed by this proposed design.
