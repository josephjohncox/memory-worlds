# Production completion design

The user authorized the remaining-work backlog as one execution goal on
2026-10-20. This document fixes the implementation boundaries and acceptance
criteria. Existing local, scoped, router, Pi, import and Helm behavior remains
part of the current contract; no historical readers or migrations are added.

## Storage and work ownership

SQLite remains the local backend. PostgreSQL is the shared source, control and
publication-outbox backend, usable with RDS PostgreSQL. One SQLAlchemy Core
statement/schema contract serves both implementations. PostgreSQL namespaces
are derived from validated authority/scope identities, never user-selected SQL
paths. Graphiti and FalkorDB remain the graph extraction/retrieval layer.

Shared workers acquire expiring leases with monotonically increasing fences.
Stale workers cannot finish jobs or activate graph generations. Because a lease
cannot cancel an external graph write, takeover never reuses a namespace whose
write outcome is unknown: it degrades that generation and builds a replacement.
Router retries retain the exact idempotent destination envelope.

Acceptance requires real PostgreSQL tests covering every public storage method,
independent processes, lease loss, worker death, revoked grants, concurrent
imports, exact replay and takeover. Merely accepting a connection URL does not
establish PostgreSQL support. Kubernetes permits multiple API owners only for
the shared backend and tested distributed ownership.

## Recovery

A whole-authority backup includes control/identity/grant state, every private and
shared source journal, projection watermarks, generation inventory, and the
publication outbox. Runtime provider keys, router token files and installation
environment files remain operator-managed secrets outside the archive.

Backups use authenticated encryption with an explicit key supplied through a
private file. Encryption failure, input mutation, unsupported graph snapshot
format or incomplete source coverage fails the backup. Completed archives become
visible atomically and never replace an existing backup silently. Manifests bind
all members to checksums, authority identity, source watermarks and backend
versions. A maintenance barrier or stopped authority makes the cross-store cut
explicit. SQLite WAL files are never copied as independent snapshots.

Restore validates and authenticates all data before changing a fresh target.
Native graph snapshots restore into a separate isolated backend; exact graph
recovery and source-only graph rebuilding are explicitly different modes.
Credentials/grants restore only in the explicit whole-authority operator path;
portable scope imports continue to exclude live grants. Acceptance includes lost
data-volume recovery, private-scope isolation, credential revocation, pending
publication replay, graph retrieval and rollback of an invalid restore.

## Retraction, erasure and retention

Retraction removes evidence from normal source/graph recall and records a durable
receipt. Erasure removes retained source content/metadata and schedules deletion
of every derived graph generation that could contain it. A minimal identity
tombstone prevents a later import or retry from resurrecting erased evidence.
Old backups and independently published destination copies have separate
retention boundaries; receipts state which authority/copies were affected.

Only live authorized scope managers or explicitly authorized source owners may
perform lifecycle actions. A private source reference never authorizes deletion
in a different world. Destructive operations have explicit target IDs, bounded
batch sizes, preview output, idempotent operation identities and audit receipts.
No policy is enabled implicitly. Retention targets expired invocation journals,
terminal outbox deliveries, source records and retired/failed graph generations
under explicit operator policies. Pending deliveries and active generations are
preserved. Graph deletion failure blocks graph use until repaired.

## Named identities and native Pi

An owner-scoped registry names agent, run and invocation UUIDs. A name or alias
does not grant access. UUIDs remain canonical; aliases resolve against the live
authorized registry and are persisted as canonical bindings. Explicitly selected
named agents retain their UUID across independent chats. Unconfigured chats keep
their existing fresh-invocation behavior. Restricted children cannot enumerate
or mutate unrelated identity records.

Native Pi configuration and commands resolve world/scope aliases using the same
visible-object rules as the Python CLI. Directory/chunked imports reuse the
Python write-free planner through a bounded subprocess with fixed argv, private
pipes and no child credential. The Pi client submits the resulting records to
one explicit authorized private scope. Source content is never rendered as a
tool result merely to transport it between the planner and client.

## Qualification

Keep deterministic contract tests and add separately executable real-provider
qualification. The sharing evaluation corpus includes permitted decisions,
procedures, technical findings, attributed team knowledge and explicit opt-ins,
plus prohibited personnel judgments, compensation, credentials, private thoughts,
indirect disclosure, adversarial instructions and mixed sensitive context.
Reports retain model/provider identity, dataset digest, exact configuration,
latency and measured outcomes without credentials or private production chats.
An evaluation failure remains a failure; synthetic tests are not reported as
real-model accuracy. Haiku/Luna runs use explicitly configured Pi registry auth.

The user superseded live qualification with local emulation. A local OIDC server
in kind exercises issuer/audience/client/scope gates, device login, group
mapping/removal, key rotation, expiry and delegated revocation. Local deterministic
model providers exercise the graph and sharing contracts. No live tenant,
production cluster or further paid model calls are required for acceptance.
Reports identify emulated behavior explicitly and do not claim measured external
model quality or a verified production identity provider deployment.

## Operations and releases

Expose bounded-cardinality service/worker/queue metrics without source text,
identity labels or credentials. Add optional monitoring resources, actionable
alerts, scheduled encrypted backups and restore guidance. Validate networking
on a policy-enforcing CNI and persistence on the selected storage class.

Completed backup artifacts may be copied through an explicit configured cloud
transport. Live database files are never synchronized. Cloud credentials remain
in the selected transport's private configuration. Bidirectional live graph
merging is a separate data model and is not implied by artifact synchronization.

Refresh validation documentation from executed checks. Build all distributions
from one clean signed commit and retain durable versioned private release
artifacts and immutable container digests. Repository, package and release
visibility remain private until an explicit public release decision. Public
PyPI/npm/Helm publication requires that release decision; preparation and local
publication checks proceed now.
