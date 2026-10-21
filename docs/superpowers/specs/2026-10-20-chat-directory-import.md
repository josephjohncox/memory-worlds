# Chat directory import

Import previously extracted ChatGPT and Claude archives through the shared HTTP
service. The CLI reads a directory on the client's machine; the service receives
validated source episodes and stores the complete batch in one private scope.

## Boundary and selected approach

Use a small standard-library parser and a typed, write-free `ImportPlan`.
Requiring another exporter or SDK would introduce credentials and network access
into a task that only needs existing files. Supporting native JSON alone would
exclude directories produced by the requested richlow exporter. The selected
approach accepts native JSON and its precise Markdown rendering, with an explicit
document mode for other UTF-8 text.

The public command is:

```text
import-directory WORLD DIRECTORY --scope UUID|@alias
  [--format auto|chatgpt|claude|richlow-md|documents]
  [--all-branches] [--source-timezone IANA_ZONE]
  [--fallback-timestamp AWARE_ISO_TIMESTAMP] [--dry-run] [--source-only]
```

The destination is an `agent-persistent` or `invocation-private` scope in the
requesting user's `personal` or `private-work` world. Shared scopes and shared
worlds are rejected. The source files cannot select a destination, grant access,
or bind an agent context. Existing single-file administrative import and portable
recovery commands retain their separate purpose.

## Planning and application

`plan_directory()` opens the selected directory without following links, walks
its visible tree, and binds file reads to regular descriptors. Hidden entries
are skipped. Visible symlinks and special files fail the plan. Reads detect file
replacement or mutation and enforce aggregate byte, entry, depth, episode, and
request-size budgets.

Every selected input is parsed before a token is read or an HTTP client is
constructed. A malformed final file therefore cannot leave an earlier file
imported. The plan holds the parsed bytes' meaning in `EpisodeInput` values;
application uses those values rather than reopening the source paths.

`--dry-run` emits the bounded report and requested destination, without resolving
aliases, reading credentials, opening the local database, or contacting the
service. It validates input, not the existence or permission of the destination.
The report includes relative file paths, formats, sizes, hashes, counts, and
fidelity warnings; it omits transcript content and absolute source paths.

Application resolves world and scope aliases using the current credential's
visible service objects, then sends one request to
`POST /v1/worlds/{world}/scopes/{scope}/imports`. The server checks the live
principal, ownership, destination kind, and write authorization, then commits
all sources and graph jobs in one transaction. Any conflict rolls back the batch.
The returned report includes canonical destination IDs and imported/replayed
counts. Imports queue Graphiti extraction by default. `--source-only` explicitly
sets `project=false` for every planned episode.

## Source formats and identity

- `auto` recognizes native ChatGPT JSON, observed native Claude JSON, and exact
  richlow Markdown headers. Unrelated JSON and Markdown are reported as skipped;
  malformed selected formats abort planning.
- ChatGPT JSON preserves native conversation/node IDs and the active branch.
  `--all-branches` adds alternate nodes without flattening their parent links.
- Claude JSON uses conversation/message UUIDs, sender, text/content blocks, and
  aware timestamps. Messages remain separate evidence. Message parent/index
  fields are retained as metadata; mutable active-leaf selection is not persisted.
  No active branch or linear turn chain is invented.
- Richlow Markdown remains a complete rendered snapshot. The exporter omits
  native IDs and writes unescaped bodies, so role-looking text and fenced code
  are not split into invented messages. Snapshot identity derives from file bytes.
- `documents` explicitly selects UTF-8 `.txt` and `.md` files as opaque snapshots
  and requires `--fallback-timestamp`. It never infers speakers or branches.

Long rendered/document snapshots use consecutive bounded chunks. Each chunk's
identity includes the complete file hash and its chunk index; chunking preserves
all source text without inventing turn boundaries.

Native IDs and snapshot hashes are independent of filenames and directory roots.
Exact copies and unchanged reimports deduplicate or replay. Changed native source
content conflicts with the existing identity. Changed rendered bytes create a
new snapshot. Native JSON and rendered snapshots of the same chat have different
identities; the report warns when both classes are selected, and `--format` lets
the user choose one representation.

Naive richlow timestamps require `--source-timezone`; ambiguous or nonexistent
daylight-saving times fail. Unknown creation dates require an explicit aware
fallback timestamp. Neither filesystem times nor the current clock supply source
dates. Unsupported blocks and attachment references are counted and retained as
bounded source evidence where available; links and attachment bytes are never
fetched.

## Verification

Test local credential-free preview, required scope selection, option forwarding,
default extraction and explicit source-only imports, live alias resolution,
remote application with no local database access, replay after moving an input
root, private destination enforcement, malformed final input before service
contact, and safe CLI errors. Parser and service tests cover filesystem races,
symlinks/FIFOs, hidden entries, duplicate/conflicting identities, timestamp and
fidelity constraints, whole-batch rollback, and live authorization changes.

## Implementation sequence

1. Add source parsers and a bounded immutable import plan.
2. Add the authenticated transactional private-scope import route.
3. Wire the CLI preview/application boundary and document exact commands.
4. Run focused parser, service, and CLI tests, then the repository checks.

This implements the requested directory workflow directly. No exporter login,
scraper, MCP tool, Pi feature, compatibility subsystem, or new dependency is
required.
