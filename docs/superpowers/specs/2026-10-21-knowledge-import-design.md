# Knowledge-base imports

## Purpose and boundary

Import knowledge from existing exports, local Git objects, or output from an
existing client. Memory Worlds owns document normalization, context, chunking,
scope authorization, durable receipts and Graphiti extraction. External tools
own authentication, remote pagination, downloading and synchronization.

Use a typed document bundle rather than raw provider JSON in durable storage.
Passing arbitrary text straight to capture loses revision and audience context;
building provider clients duplicates authentication and synchronization work.
Thin local adapters into one bundle give a useful, testable boundary.

## Document contract

A collection supplies a stable namespace, name, optional project, description
and labels. Each document supplies an object ID, title, exact normalized text,
an immutable revision or content-derived revision, and an aware source timestamp
or explicitly supplied snapshot timestamp. Optional fields preserve source URI,
authors, hierarchy, content kind and source-reported access restrictions.
Project and collection identifiers qualify entity context; labels do not create
an authorization boundary. Use separate scopes when separation must be enforced.

Content kinds distinguish reference material, policy, code, discussion and
decision records. These are source classifications, not assertions of correctness
or organizational approval. The authenticated importer is recorded separately
from the source authors. Source timestamps describe the document or snapshot;
they do not establish when every statement became true.

Context and source access fields are bounded typed data. They do not contain
credentials, grant service permissions or direct the extractor. A document's
sharing declaration defaults to private. Declaring it eligible for an explicit
scope publication does not itself publish anything.

## Inputs

- A normalized JSON bundle or JSONL document stream with collection context.
  Other tools can produce this format without depending on a provider SDK.
- An explicitly selected directory of UTF-8 text, Markdown or HTML files.
  Descriptor-bound reads reject links, special files and mutation. Rendered
  files retain path/snapshot identities; native IDs and dates are not invented.
- Saved Confluence REST-v2 page JSON with IDs, version metadata and supported
  HTML/storage bodies, or an unpacked static HTML export. Exported comments,
  attachments or unsupported macros are not silently claimed as imported.
- Selected files from a pinned commit in an existing local Git repository.
  Read tree/blob objects, never execute content, check out files, fetch, invoke
  filters or traverse submodules. Disable lazy fetching and optional locks.
  Require a Git version that supports explicit lazy-fetch suppression; fail
  closed when that capability is unavailable. Pin Git in the mise toolchain.
  Require explicit file selection and retain commit, blob and repository-relative
  path context. Remote Git operations stay with the user's Git tooling.

No input fetches links or attachments. Archive unpacking and remote acquisition
remain external operations. Diagnostics and preview reports omit document bodies.

## Planning and context

Planning performs no service calls or writes. A deterministic heading-, paragraph-
and fence-aware chunker preserves contiguous exact text and character offsets.
Chunks carry the document identity, revision, heading path, chunk position and
whole-document digest. No overlap or generated summary introduces extra claims.
Identities depend on the collection, object, revision and chunk descriptor, never
the absolute input path or the current clock. Replaying identical input is
idempotent; changed data under an immutable revision conflicts atomically.

Lower documents to the current EpisodeInput contract with typed
`metadata.knowledge`, original source references and Graphiti extraction enabled
by default. Enforce the existing 10,000-episode and 32 MiB request limits, bounded
input budgets and bounded reports before applying anything.

Graphiti receives quoted descriptive context separately from body text. It must
preserve attribution, revision, content kind and temporal uncertainty. Knowledge
chunks do not borrow arbitrary recent episodes from unrelated documents or
revisions as extraction context. Retrieval retains source episode IDs so a
harness can obtain the authorized source document and its context for citations.

## Applying and audience

Private application uses the existing atomic private-scope import path. It
requires the importing user's own personal/private-work world and a private
agent-persistent or invocation-private scope, with current write authorization
and no other live user readers.

Shared application is a distinct, explicit knowledge publication action. It
requires an explicitly chosen world/scope, an explicit shared-mode request,
publication-eligible documents, the world's write role and the scope's publish
grant. It never widens chat-directory import permissions. Validate the entire
knowledge batch before one transaction inserts source records, audit rows and
projection jobs. Replays and source conflicts follow the current source contract.

Confluence restrictions, Git repository visibility and bundle access labels are
snapshots reported by the exporter. They are not equivalent to Memory Worlds
membership and cannot prove that every destination reader is authorized upstream.
Use a private destination unless an operator/user has explicitly mapped the
source audience to the chosen scope. Revocations and disappeared source files do
not automatically retract an imported snapshot. Retraction uses the existing
source lifecycle tools; this feature does not advertise continuous ACL sync or
automatic latest-version views.

## User and harness tools

Provide a local preview/plan command and an authenticated apply command with
explicit world, scope and private/shared mode. Generic bundle input works through
files or stdin; provider adapters perform only local conversion. Add typed Python
and TypeScript submission, and a bounded MCP bundle tool. Native Pi uses the
installed Python planner instead of maintaining a second parser implementation.
Plans can be saved as private bounded files with a canonical SHA-256 digest.
Applying a reviewed plan verifies the requested digest and submits the exact
saved records, without reopening source files or regenerating timestamps. Plan
files contain source content and must not appear in preview output or artifacts.
MCP accepts supplied bundles; filesystem access is not implicitly granted by
enabling the MCP server. Native SDK callers can explicitly invoke local planning.
Preview metadata and source citations remain separate from chat capture, ambient
retrieval, write selection and automatic sharing settings.

## Acceptance

Exercise the actual CLI and HTTP service with synthetic Confluence pages, a local
Git repository and generic bundles. Check exact chunk reconstruction and offsets,
context reaching the Graphiti call, immutable revision replay/conflict, same text
under distinct documents, explicit aware timestamps, source attribution and
retrieval citations. Test atomic rollback, unauthorized/delegated/shared audience
boundaries, default private eligibility, source-only mode and queued extraction.
Check links, binary inputs, mutation, input/output limits, disabled Git network
paths, and sanitized tool errors. Validate the native package and source/artifact
audits through the locked mise tasks. Do not call live knowledge bases or paid
models, or introduce new infrastructure for this import boundary.
