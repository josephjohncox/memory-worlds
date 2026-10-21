# Import extracted chat directories

[Documentation](README.md) · [Import reference](reference/chat-import.md)

Import existing ChatGPT or Claude exports, richlow extractor output, or UTF-8
text documents into private memory. `import-directory` reads files on the CLI
machine, validates them locally and sends one batch to the service. It does not
log in to either chat provider or fetch linked attachments.

The destination must be an `agent-persistent` or `invocation-private` scope in
your own `personal` or `private-work` world. Shared import destinations are
rejected; publish selected findings separately.

## Create a private archive scope

Find an existing destination with your ordinary user or agent credential:

```sh
mise run cli -- --token-file /private/agent.token discover --scopes
```

If needed, create a private scope using a credential with a writer role on that world.
Save `/private/import-scope.json` as:

```json
{"kind":"agent-persistent","name":"Imported chat archive","alias":"history"}
```

```sh
mise run cli -- --token-file /private/agent.token \
  scope-create PERSONAL_WORLD_ID --input /private/import-scope.json
```

Use the returned UUID, or its `@history` alias within that world. Aliases label
objects; [scope grants](scopes.md) determine who can read them.

## Preview, then apply

Preview is offline and requires no token or daemon:

```sh
mise run cli -- import-directory PERSONAL_WORLD_ID /private/chat-exports \
  --scope @history --dry-run
```

The content-free report shows formats, sizes, hashes, episode counts, skipped
files and warnings. Preview does not verify the destination's existence or your
permission to write it.

Apply with an authorized credential:

```sh
mise run cli -- --token-file /private/agent.token \
  import-directory PERSONAL_WORLD_ID /private/chat-exports --scope @history
```

Add global `--url` before `import-directory` for another service or an
[SSH tunnel](remote-access.md). Files stay on the CLI machine until their parsed
records are sent; the command never opens the service's data directory.

The service commits the batch atomically. Unchanged records replay without
creating duplicates; conflicting source identities reject the batch. Graphiti
extraction is queued by default. Use `--source-only` explicitly to omit it.
Preview and apply are separate scans, so apply validates the current file bytes.

## Choose a representation

| Format | Best input |
| --- | --- |
| `auto` | Recognized native JSON and exact richlow Markdown; unrelated files are skipped |
| `chatgpt` | Native ChatGPT JSON with message identities and parent links |
| `claude` | Native Claude JSON using `uuid`, `chat_messages` and `sender` |
| `richlow-md` | Markdown from [richlow/chatGPT_extractor](https://github.com/richlow/chatGPT_extractor) |
| `documents` | Explicit UTF-8 `.txt` and `.md` snapshots |

Select `--format` when several representations of a chat are present. Native JSON
retains message provenance; rendered text is an opaque snapshot. Importing both
can duplicate evidence. ChatGPT defaults to its active branch; `--all-branches`
keeps alternate nodes without joining them into a fabricated transcript.

See [format and retry identity details](reference/chat-import.md#choose-a-representation).

## Rendered timestamps and other documents

Richlow timestamps need the exporting machine's timezone:

```sh
mise run cli -- --token-file /private/agent.token \
  import-directory PERSONAL_WORLD_ID /private/chatgpt-export --scope @history \
  --format richlow-md --source-timezone America/Los_Angeles
```

For other extracted text, explicitly choose document mode and an aware fallback
timestamp:

```sh
mise run cli -- --token-file /private/agent.token \
  import-directory PERSONAL_WORLD_ID /private/documents --scope @history \
  --format documents --fallback-timestamp 2026-10-20T12:00:00Z
```

The importer never substitutes file modification time or invents speakers and
message IDs. Long snapshots split into consecutive lossless chunks. See
[timestamp, branch and fidelity limits](reference/chat-import.md#rendered-timestamps-and-other-documents).

## Filesystem and batch limits

Hidden entries are skipped; visible symlinks and special files are rejected.
Malformed selected input aborts the batch. Large archives need smaller input
directories; see [exact limits](reference/chat-import.md#filesystem-and-batch-limits).

For native Pi, use `/memory import DIRECTORY --preview`, then
`/memory import DIRECTORY` with a selected private target. See
[Pi import commands and SDK](reference/pi.md#import-extracted-directories).
For existing Memory Worlds data, use [portable archives](portable-archives.md).
