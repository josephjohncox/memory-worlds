# Storage and recovery boundaries

One Unix daemon owns a local data directory. It contains `control.sqlite3` and
`worlds/<opaque-id>/source.sqlite3`, plus `router.sqlite3` for explicit remote
publication envelopes and delivery receipts. The control database owns authority
identity, world metadata, token hashes, OIDC sessions, memberships, and grants.
A scope database owns source episodes,
capture audit, projection jobs, active generation, and public model provenance.
The world-level journal holds the world-shared scope. Additional scope journals live at
`worlds/<world-id>/scopes/<scope-uuid>/source.sqlite3`; scope grants and invocation
contexts remain in `control.sqlite3`. See [agent scopes](scopes.md).
Capture and enqueue commit together with SQLite synchronous FULL transactions.
Files are private to their operating-system owner. Use a local filesystem.

SQLite readers and writers use the daemon; agents do not synchronize open files.
Local administrative import/export uses the same authorization and SQLite
transactions. World metadata is immutable in this version, so a source read
transaction and that metadata establish the portable export boundary.

`.mworld` recovery is source-only. It preserves attributed evidence and audit
records and deliberately excludes live ACLs and credentials. It can reconstruct
a new graph, but model extraction need not reproduce the same interpretation.
The current archive format identifies the selected scope and preserves invocation
provenance. Historical archive formats are not supported. World-only export includes the
world-shared journal; select private scopes explicitly. Private archives require
an explicit compatible private restore destination.
Archive checksums detect corruption, not malicious forgery; the archive is not
signed or encrypted. Only import archives from a trusted source.

`backup` uses SQLite's online backup API and checks the resulting source
database. The native source backup is not an authority backup: it does not carry
the control database's credential or membership state. `restore` accepts portable
archives rather than arbitrary SQLite files. For routine migration use
export/restore. Native SQLite file recovery requires an administrator to preserve
the matching world/authority metadata and stop the daemon before replacing files.
The current `backup` command selects the world-shared source database;
it does not combine private scope journals into one SQLite file.

FalkorDB uses a separate persistent volume. Namespaces separate authorities,
worlds, scopes, and graph generations logically; several worlds may share one backend
snapshot. AOF reduces graph loss across backend restarts but is not an off-host
backup. The source journal remains authoritative when graph delivery is unknown.
The graph deployment requires a database password as well as loopback binding;
only the daemon receives that credential. Routine agents receive API tokens.

For a backend-native RDB snapshot, stop graph writers, use synchronous `SAVE`
and check its success, then copy the completed configured `dump.rdb` into private
backup storage. Record the backend version and matching source/projection state.
Restore only into an isolated, compatible backend first. An existing AOF can
take precedence over RDB on restart; copying one file into an active volume is
not a valid restore procedure. Exact coordinated authority/graph restoration is
not yet automated or qualified by this release's portable-source tests.

Completed portable archives and native backup artifacts can be uploaded to
OneDrive, S3, or another storage service. No cloud credentials are needed by the
memory daemon for a sync-folder workflow. Staging files use the destination
filesystem so final publication is atomic; configure sync exclusions for staging
files. Never place a live source or graph data directory in file synchronization.

For shared deployment, a PVC/StatefulSet can hold a single authoritative backend.
It does not supply replication or backups. Multiple API replicas need shared SQL
storage and durable work ownership; sharing SQLite WAL over a network filesystem
is unsupported. The optional Neptune adapter requires OpenSearch as well as
graph storage. It has offline contract tests; real AWS ingestion and recovery
remain unverified. See [deployment configuration](deployments.md).
