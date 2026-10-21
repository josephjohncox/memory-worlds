# Back up and recover an authority

Use an encrypted authority backup to recover the service's policy, private and
shared memory, jobs and publication state together. Only a live authority
administrator can request or download one.

| Artifact | Contains | Use |
| --- | --- | --- |
| Portable `.mworld` export | Selected source evidence; no live credentials or grants | [Move evidence to an authorized destination](portable-archives.md) |
| Native `.mwauthority` backup | Full authority and inventoried FalkorDB namespaces | Restore policy/source/outbox and the original graph |
| Rebuild `.mwauthority` backup | Full authority with graph-rebuild intent | Restore policy/source/outbox, then extract a fresh graph |

A full authority includes users, credential hashes, revocations, world/scope
grants, named identities, contexts, all private/shared source journals, lifecycle
tombstones, projection jobs, generation inventory and publication outbox.

Preserve provider credentials, OIDC configuration, remote mount/token files and
the encryption key separately as operator configuration. A
recovered authority has the grants and revocations that existed at its backup
cut; review subsequent revocations before admitting clients.

## Configure and take a backup

Create a raw 32-byte key outside synchronized folders:

```sh
mise run cli -- authority-keygen /private/keys/authority.key
```

Configure the daemon with absolute operator-owned paths:

```sh
MEMORY_WORLDS_BACKUP_DIRECTORY=/private/backups/authority
MEMORY_WORLDS_BACKUP_KEY_FILE=/private/keys/authority.key
```

The key must be a regular file readable only by its owner or a controlled
read-only group. The chart mounts a Secret key as a regular `subPath` file; see
[Helm backup configuration](reference/helm-values.md#encrypted-authority-backups)
for claims, key mounting and optional scheduling. The service never accepts a
client-selected server path or returns the key.

```sh
mise run cli -- --token-file /private/admin.token \
  authority-backup /private/downloads/authority.mwauthority
mise run cli -- --token-file /private/admin.token authority-backups
```

The default native backup coordinates a maintenance barrier across control,
source and outbox writes and graph operations. SQLite uses a fair thread barrier
and a shared/exclusive Unix file lock across the owning daemon and supported
local CLI/library processes. PostgreSQL uses authority-wide advisory locks across replicas. New
requests receive a retryable 503 during the cut. The artifact becomes visible
atomically after authenticated encryption and fsync, without replacing an
existing file.

Native FalkorDB capture inventories the authority's original namespace keys and
records backend/module versions. It includes graphs for active and inventoried
generations, including quarantined work. A ready active graph cannot be missing.
Other authorities' keys are excluded. Native capture is bounded to 128 MiB per
graph, 2 GiB per archive member, 8 GiB per artifact, and 10,000 members; exceeding
a limit fails explicitly.

For source recovery followed by fresh extraction, select it explicitly:

```sh
mise run cli -- --token-file /private/admin.token \
  authority-backup /private/downloads/sources.mwauthority --graph-mode rebuild
```

Both modes use AES-256-GCM. The reader verifies the authentication tag before
parsing any decrypted archive, then validates checksums, the complete current
table/scope inventory and the current schema. Recovery accepts the current archive schema.

Backup and restore need private temporary space for uncompressed tables and
graph dumps as well as the encrypted artifact. Size the process `TMPDIR` for
that working set. The default Helm `/tmp` budget is 128 MiB; for larger cuts,
point `TMPDIR` at an adequately provisioned private volume through the runtime
Secret. Keep this working directory outside generic file-sync clients. The
artifact transport copies only a completed authenticated archive.

## Restore into an isolated fresh target

Stop admission and graph writers. For SQLite, choose a directory that does not
exist. For PostgreSQL, configure a fresh empty authority target; recovery obtains
the original authority UUID from the authenticated manifest. Native graph
recovery also requires an empty dedicated FalkorDB instance with identical
backend/module versions and explicitly configured graph/model settings.

```sh
mise run cli -- --data-dir /private/recovered \
  authority-restore /private/downloads/authority.mwauthority \
  --key-file /private/keys/authority.key --native-graph
```

Omit `--native-graph` for a `rebuild` artifact. Its selected source episodes
become pending extraction; source-only evidence stays source-only. Unknown
worker ownership and in-flight delivery claims are cleared. Tombstones and
terminal publication receipts remain durable, preventing replay from reviving
deleted sources or duplicating a completed publication.

Recovery preserves original graph keys, vectors, indexes, edges and provenance;
it never renames graph keys or uses Redis RESTORE REPLACE. Graph inventory must
match the restored authority, including every ready active generation. Failed
restores remove attempted keys when the backend remains reachable. An unknown
network outcome requires checking the isolated target before retrying; recovery
never reports success after an unverified backend failure.

SQLite installs a completed staging authority by directory rename. PostgreSQL
restores its source/control/outbox records and schemas in one transaction. A
nonempty target is rejected. Keep the restored system isolated until readiness,
source recall, graph recall, private-scope boundaries and revocations have been
checked.

## Copy completed artifacts to OneDrive or S3

The optional transport uses pinned rclone 1.75.2 with an explicitly configured
named remote. Configure its OAuth or cloud credentials using rclone, keep its
configuration private, and create a Memory Worlds transport file:

```json
{
  "remote": "onedrive:MemoryWorlds/backups",
  "rclone_config": "/private/config/rclone.conf"
}
```

```sh
mise run backup:sync -- /private/downloads/authority.mwauthority \
  --key-file /private/keys/authority.key \
  --config /private/config/artifact-sync.json
```

The transport authenticates the entire archive before contacting the remote,
uses a content-addressed immutable filename, skips existing objects without
overwriting them, and verifies downloaded bytes after copying. A conflicting
remote object fails verification. It leaves other remote files alone. Only the
completed encrypted artifact is transferred; neither the key nor live database
directories are synchronized. A local OneDrive folder can also receive completed
artifacts directly. This is snapshot transport, not merging live authorities.

Transport behavior is tested with rclone's local backend. Cloud account setup is
operator configuration; the local qualification does not claim a live OneDrive
or S3 run. The implementation follows [copyto](https://rclone.org/commands/rclone_copyto/)
and [download comparison](https://rclone.org/commands/rclone_check/) contracts.

Deletion and retention operate on the live authority. Older backups and copies
published to another authority have their own retention; see [lifecycle](lifecycle.md).
