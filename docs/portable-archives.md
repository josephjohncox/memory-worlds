# Portable world archives and file imports

[Documentation](README.md)

Use `.mworld` archives to transfer one scope's source evidence between worlds or
keep a portable snapshot. For encrypted recovery of identities, permissions, jobs and
the native graph, use [whole-authority backup](recovery.md).

For extracted ChatGPT/Claude directories and exported text documents, start with
[directory import](chat-import.md).

## Export, restore and single-file commands

These are local administrative operations: run them where the authority's data
directory is accessible and use its administrator token. Set `data_dir` to the
actual authority directory, including for a guided installation.

Each archive covers one scope. Without `--scope`, the command selects the world's
world-shared scope. Private archives require an explicit destination scope of the
same audience kind or an invocation-private scope. Create the destination world
and scope first; restore imports records into them rather than creating them.

```sh
data_dir="/private/path/to/authority"
operator="$HOME/.config/memory-worlds/admin.token"
mise run cli -- --data-dir "$data_dir" --token-file "$operator" \
  export WORLD_ID /private/backups/notes.mworld --scope SOURCE_SCOPE_UUID
mise run cli -- --data-dir "$data_dir" --token-file "$operator" \
  restore TARGET_WORLD_ID /private/backups/notes.mworld --scope TARGET_SCOPE_UUID
```

## Import one conversation file

Using the same data directory and operator file:

```sh
mise run cli -- --data-dir "$data_dir" --token-file "$operator" \
  import-chatgpt WORLD_ID /private/path/conversations.json --scope PRIVATE_SCOPE_UUID
mise run cli -- --data-dir "$data_dir" --token-file "$operator" \
  import-pi WORLD_ID /private/path/session.jsonl --scope PRIVATE_SCOPE_UUID
```

The ChatGPT importer accepts a conversation object or list of conversation
objects, preserves original roles/times/IDs/parents, and selects the active branch
by default. Pi JSONL uses the current leaf; `--all-branches` imports alternatives
without splicing them into a conversation. Extending an active-branch import to
all branches is idempotent. Unsupported parts are retained/reported as source
metadata or references; referenced attachment bytes are not imported.

## What a portable archive preserves

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
limits documented in the [directory import guide](chat-import.md).

## Sync completed artifacts

For OneDrive or another file-sync tool, sync completed `.mworld` artifacts.
Exports stage a `.pending` file on the destination filesystem, then atomically
publish the final name without overwriting a previous artifact. A cloud client
may see staging files: exclude `*.pending` in the sync tool where possible, and
consume only finalized `.mworld` files. These archives are plaintext. Keep live
SQLite/WAL/FalkorDB directories outside synced folders. Export is a snapshot,
not bidirectional multi-writer database synchronization.

See [storage and recovery](storage.md) for the difference between portable source
archives, exact graph snapshots and encrypted authority backups.
