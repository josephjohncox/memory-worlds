# Chat import reference

[Documentation](../README.md) · [Guide](../chat-import.md)

`import-directory` reads existing files on the machine running the CLI and sends
one validated batch to the memory service. It supports native ChatGPT and Claude
JSON, directories produced by
[richlow/chatGPT_extractor](https://github.com/richlow/chatGPT_extractor), and an
explicit mode for other text documents. It does not log in to either chat service.

Import into an `agent-persistent` or `invocation-private` scope in your own
`personal` or `private-work` world. The service rejects shared destinations for
this operation. An alias names the scope; live credentials authorize it.

## Create a private archive scope

Use `discover --scopes` to find an existing private scope, or create one with a
credential that has a writer role on your own personal or private-work world.
Replace `PERSONAL_WORLD_ID` with its canonical ID. These commands do not require
assigning a world alias, which is an administrator operation.

```sh
umask 077
mkdir -p ~/.config/memory-worlds
cat > ~/.config/memory-worlds/import-scope.json <<'JSON'
{"kind":"agent-persistent","name":"Imported chat archive","alias":"history"}
JSON
mise run cli -- --token-file /private/agent.token \
  scope-create PERSONAL_WORLD_ID --input ~/.config/memory-worlds/import-scope.json
mise run cli -- --token-file /private/agent.token \
  discover --world PERSONAL_WORLD_ID --scopes --json
```

The response includes the new scope UUID. `@history` resolves within the selected
world; agents that should read this archive still need explicit scope grants.
See [scopes and delegation](../scopes.md).

## Preview, then apply

A preview requires no token, daemon, or network connection:

```sh
mise run cli -- import-directory PERSONAL_WORLD_ID /private/chat-exports \
  --scope @history --dry-run
```

The JSON report includes relative paths, formats, byte sizes, SHA-256 hashes,
episode counts, skipped-file counts, and warnings. It omits transcript content.
Preview preserves the requested world/scope references and does not check their
existence or your permission to use them.

Apply the same selection through the authenticated service:

```sh
mise run cli -- --token-file /private/agent.token \
  import-directory PERSONAL_WORLD_ID /private/chat-exports --scope @history
```

Add the global `--url` option before `import-directory` to use another service,
including one reached through an [SSH tunnel](../remote-access.md).
The input directory stays on the CLI machine. The command resolves aliases at
the selected service and sends the already parsed batch to
`POST /v1/worlds/{world}/scopes/{scope}/imports`; it never opens local service data.

All selected files must validate before any HTTP request. The service writes the
batch in one transaction: an invalid record or conflicting source ID leaves no
partial batch. The result adds `imported` and `replayed` counts and canonical
destination IDs to the report. Graphiti extraction is queued by default and
completes separately from source storage. `--source-only` explicitly disables
extraction for the imported records.

Preview and apply are separate scans. If files change between commands, the apply
command validates their new contents. Mutation or replacement detected while a
file is being read fails the plan; application sends its validated in-memory
records without reopening the files.

## Choose a representation

| `--format` | Selected files and fidelity |
| --- | --- |
| `auto` | Recognized native ChatGPT/Claude JSON and exact richlow Markdown; unrelated files are skipped |
| `chatgpt` | Native ChatGPT JSON; conversation and node IDs, roles, timestamps, and parent links are retained |
| `claude` | Observed native Claude JSON with `uuid`, `chat_messages`, and `sender`; messages remain separate evidence |
| `richlow-md` | A complete snapshot of each richlow Markdown transcript |
| `documents` | Explicitly selected UTF-8 `.txt` and `.md` snapshots |

For a directory containing several representations, select one:

```sh
mise run cli -- import-directory PERSONAL_WORLD_ID /private/chat-exports \
  --scope @history --format chatgpt --all-branches --dry-run
```

ChatGPT defaults to its active branch. `--all-branches` imports alternate nodes
without splicing them into one transcript. Claude messages are imported
individually; an active branch or linear turn chain is not inferred. Supplied
message parent/index fields remain metadata; mutable active-leaf selection is
not persisted. Unsupported content and attachment references
are counted and retained where available; attachment links are never fetched.

Native source IDs remain stable after a directory is moved or files are renamed.
Exact copies deduplicate within a batch, and unchanged reimports replay existing
records. Changed content or retained metadata under an existing native ID returns
a conflict; this includes editing the conversation title. Renaming the export
file leaves native payloads unchanged. Use the
same projection setting when replaying; switching `--source-only` changes the
source record's projection intent and can also conflict.

Rendered transcripts lack native message identities. Their identity is a hash of
the file bytes: unchanged copies deduplicate, while edited files create a new
snapshot. JSON and Markdown representations of one chat can therefore overlap;
the mixed-format report warns about this instead of guessing that they are the
same source. Select `--format` to avoid importing both.

## Rendered timestamps and other documents

The richlow exporter writes local dates at minute precision without a UTC offset.
Supply the timezone used by the exporting machine:

```sh
mise run cli -- --token-file /private/agent.token \
  import-directory PERSONAL_WORLD_ID /private/chatgpt_export --scope @history \
  --format richlow-md --source-timezone America/Los_Angeles
```

The importer rejects ambiguous or nonexistent daylight-saving times. It preserves
the entire Markdown document, including code fences, role labels, separators, and
literal links. It cannot recover omitted branches, original message IDs, exact
message times, or attachment bytes from that rendering.

When the richlow creation date is `Unknown`, supply an explicit aware timestamp
with `--fallback-timestamp`. For other extracted text, opt into document mode:

```sh
mise run cli -- --token-file /private/agent.token \
  import-directory PERSONAL_WORLD_ID /private/claude-markdown --scope @history \
  --format documents --fallback-timestamp 2026-10-20T12:00:00Z
```

The fallback is recorded as your supplied date. File modification times and the
current clock are never substituted. Document mode preserves opaque text without
inventing speakers, native IDs, or conversation structure. Long richlow and
document snapshots are split into consecutive source episodes within the service
content limit; chunk IDs include the complete file's hash and chunk index.

## Filesystem and batch limits

Hidden files and directories are skipped. Visible symlinks, FIFOs, sockets, and
other special files are rejected. Imports do not follow linked directories,
execute source text, or fetch referenced assets. A malformed selected file aborts
the entire plan; unknown files contribute to the skipped count.

A batch is bounded to 20,000 directory entries, 16 nested levels, 128 MiB of
candidate input, 10,000 episodes, and a 32 MiB encoded request. Individual source
records retain the service's content and record-size limits. Reports list at
most 200 selected files and count the omitted rows. Split larger archives into
smaller directories and preview each selection.

The [portable archive workflow](../portable-archives.md)
exports existing Memory Worlds evidence and audit provenance for recovery.
Directory import brings external source files into a new private memory scope.
