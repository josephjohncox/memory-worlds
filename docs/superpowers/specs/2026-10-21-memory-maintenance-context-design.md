# Memory maintenance and memory-backed context

**Status: proposed. This PR contains a design, not an implementation.**

[Proposal overview](../../proposals/memory-maintenance.md) ·
[Current architecture](../../architecture.md) · [Current lifecycle](../../lifecycle.md)

## 1. Purpose and governing distinctions

Give agents recoverable working memory outside their active context windows and
give users explicit processes for summarization, enrichment, expiration,
compaction, pruning and purge. Model selection is configurable; the recommended
default is a lightweight profile resolving the deployment's Luna route.

Keep these operations distinct:

| Operation | Effect |
| --- | --- |
| Context eviction | Remove material from a particular model request |
| Semantic compaction | Create a smaller, lossy representation of identified inputs |
| Lossless compression | Reduce stored bytes while retaining reversible content |
| Thread/context expiration | Exclude an object from automatic selection according to server-time policy |
| Retrieval pruning | Retire or deprioritize redundant derived views |
| Source retraction/erasure | Change source availability through the existing lifecycle contract |
| Graph purge | Complete external namespace cleanup under the existing maintenance protocol |

None implicitly authorizes another. A model's confidence is not verification.
A thread identifier is not a grant. A summary is another copy of information,
not a means of weakening its audience boundary.

## 2. Existing foundations and substantive gaps

The following are implemented on main when this proposal is written. The inspected
native Pi contract is version 1.3.0; implementation must qualify the selected
current release rather than assuming these interfaces never change:

- Immutable textual episodes, stable source identity, request replay checks,
  capture receipts and source-side provenance in `models.py` and `store.py`.
- World-shared, run-shared, agent-persistent and invocation-private scopes, live
  credentials and scope-flow validation in `scopes.py`.
- Per-scope Graphiti extraction/retrieval and fenced graph workers in `graph.py`.
- Database-clock leases and monotonic fencing in `persistence.py`.
- Retract/erase tombstones, graph purge intents, frozen retention plans and
  separate source/graph receipts in `lifecycle.py`.
- Bounded Pi proposal reviewers and explicit model resolution in
  `packages/pi/src/curation.ts` and `sharing.ts`.
- Pi capture, exposure tracking, pre-turn retrieval and compaction hooks.

Important gaps constrain the proposal:

1. Current Pi capture preserves user/assistant text. It omits tool calls/results,
   attachments and other structured blocks; it is not a reconstructible context
   archive. Capture can be disabled, bounded or fail.
2. The current precompaction hook flushes capture but does not cancel compaction
   on incomplete preservation. Existing capture receipts do not prove complete
   context coverage.
3. Graphiti search objects are generation-specific and labeled
   `derived-unreviewed`. Their graph UUIDs are not stable accepted-claim IDs.
4. Extraction currently chooses nearby predecessor episodes within a scope,
   rather than an explicit thread/branch revision. Unrelated threads can provide
   semantic context to one another even though scope isolation is preserved.
5. There is no general thread registry, claim revision history, enrichment queue,
   derivation dependency registry or maintenance-job scheduler.
6. Retention is explicit and unscheduled. Graph purge requires drained-worker
   operator attestation; a background timer cannot truthfully replace that barrier.

## 3. Recommended architecture and alternatives

Use a library-owned maintenance coordinator and one durable job protocol. Keep
Graphiti as the semantic projection and retrieval engine. Store authoritative
thread manifests, artifacts, claims, jobs and lineage alongside the existing
SQLite/PostgreSQL source journals. Blob storage is an optional payload backend,
not an independent permission authority.

| Approach | Advantage | Limitation |
| --- | --- | --- |
| Shared coordinator and durable jobs — recommended | One policy and replay contract across local, shared and harness execution | Requires explicit source-side records and scheduler work |
| Independent harness hooks/scripts | Small initial surface | Repeats policy, misses unattended work and fragments retry/retention semantics |
| Require a general workflow engine | Rich orchestration | Adds an operational dependency without replacing source authority or lifecycle checks |

Pi, CLI and future workflow adapters submit work to the same coordinator. A
workflow engine may orchestrate it, but does not own memory permissions or erasure.
This proposal does not depend on the separate LangGraph integration PR merging.

## 4. Placement, scope and thread identity

Local versus corporate determines placement. Audience and lifetime remain
explicit scope properties. There is no automatic upload from a local context
archive to a corporate world or generic cloud-sync folder.

The recommended new boundary is `thread-private`, alongside the existing four
scope kinds. It represents private resumable thread memory across invocations.
It requires implementation throughout grants, flow checks, discovery, delegation,
archives, clients and retention. Adding only an enum or thread filter is insufficient.

A `Thread` has:

- UUID, optional unique alias/name, authority/world and immutable owning scope;
- owning principal, coordinator identity and readers/writers defined by scope grants;
- optional registered agent/run association, without deriving access from IDs;
- branch ancestry, immutable revisions, current head and activity timestamps;
- state `open`, `closed`, `expired` or `retracted`, and a retention-policy reference.

A thread-private scope binds exactly one thread UUID. New invocations receive
explicit grants to it; expired credentials or a matching agent UUID cannot reopen
access. Parent and sibling invocations gain nothing by knowing its name.
The thread registry has no independent ACL: its access view is a projection of
the authoritative scope grants. Discovery filters names and existence signals
through those same grants.

Creation uses an idempotent prepared request and a durable receipt. Where scope
registration and journal initialization span separate stores, retries reconcile
the same planned identities; they do not invent replacement scope/thread IDs.

Scope-flow validation must prove the destination is permitted for every input.
Unknown relationships fail closed. In particular, invocation-private input does
not automatically flow to a thread readable by future invocations. Thread-private
input does not automatically become agent-persistent or shared knowledge.

An alternative is a `Thread` collection inside an existing agent-persistent
scope. That is a smaller implementation, but every credential authorized for that
scope can access its threads. If selected during review, describe it as collection
selection and do not promise exclusive thread privacy. Existing shared/run-scoped
threads can also use their explicitly selected audience rather than thread privacy.

## 5. Durable objects and source lineage

These are logical contracts; physical table layout is an implementation decision
subject to the existing control/journal transaction boundaries.

| Object | Required contract |
| --- | --- |
| Thread revision | Ordered membership/branch manifest, source watermark, revision digest and exact predecessor references |
| Source reference | Qualified authority/world/scope, episode or blob ID, digest, entry identity and optional span/chunk coordinates |
| Context bundle | Typed captured events, coverage, omissions, chunk manifest and immutable receipt |
| Artifact | Kind, content digest, exact input manifest, output audience, model/profile/prompt provenance and immutable creation receipt |
| Claim revision | Stable claim UUID, kind, statement, epistemic state, attribution and supporting/contradicting source references |
| Enrichment flag | Target revision, reason, requested evidence, priority, assignment/work status and resolution receipt |
| Maintenance job | Frozen intent, execution state, attempts, lease/fence, budget and result receipt |
| Derivation edge | Qualified input revision/range to output revision, dependency purpose and invalidation state |
| Context pin | Explicitly bounded recovery dependency, expiry and requesting workflow/context identity |

Artifacts include summaries, context checkpoints, enrichment reports and pruning
proposals. Their content is immutable; revising one creates a new artifact.
Graph fact IDs plus generation may be attached as projection references. Durable
claims and enrichment flags remain anchored to journal sources and digests.

Do not infer a durable thread from arbitrary `session_id` metadata. Register
membership and branch/head changes explicitly. Graphiti extraction uses bounded
predecessors from that declared thread revision. Cross-thread enrichment names
its additional inputs and runs as a separate authorized operation.

Semantic records remain projected through Graphiti. Raw context archives need not
turn every tool-output byte into graph knowledge. Archival acknowledgement is a
source-durability guarantee and does not wait for graph extraction to finish.

Derivation registration, live dependency checks and lifecycle participation ship
with the first retained artifact. Automatic derivatives are initially same-scope.
Any later cross-scope derived view must also require current access to all of its
inputs on reads, including after grant changes; its Graphiti projection must obey
the same fence. Explicitly authorized publication/declassification is a distinct
copy boundary and is not retrospectively treated as an ordinary derived view.

## 6. Tentative claims, ideas and enrichment

Keep three dimensions independent:

- **Kind:** fact, hypothesis, idea, decision, procedure or preference.
- **Epistemic state:** tentative, supported, disputed, superseded or retracted.
- **Work state:** open, assigned, running, resolved, deferred or dismissed.

Supported means identified evidence supports the statement. Verification records
a separately identified reviewer or deterministic check and its evidence; a second
model agreeing does not constitute verification. Model-reported confidence, if
retained, is explicitly labeled as heuristic output.

Several enrichment flags can coexist: `missing-evidence`, `missing-source`,
`ambiguous-entity`, `conflicting-sources`, `stale-source`, `missing-context` and
`needs-validation`. Flags attach to an exact thread, claim or artifact revision.
They do not imply that an idea is false or that a successful worker has established
a fact. Resolution cites new evidence or an attributed authorized decision.

A lightweight assessor may create flags or propose claim revisions inside its
allowed scope. Evidence gathering uses configured read-only connectors or
supplied snapshots; it does not silently crawl new services. Content is untrusted
evidence. Enrichment never expands readable scopes or publication destinations.

The retrieval API exposes epistemic state, source age, unresolved flags and
attribution. Tentative information remains available when requested, with visible
qualification. Compaction must retain contradictions and uncertainty rather than
rewrite them as consensus. Independently promoting a selected claim into a broader
scope uses the existing explicit publication/sharing authorization.

## 7. Selectable workers and model profiles

Use a provider-neutral profile contract. Native Pi workers may use the Pi model
registry; unattended service workers need their own configured provider adapter.
A daemon cannot depend on an interactive Pi session being alive.

Illustrative proposed configuration, not accepted by the current config parser:

```toml
[maintenance]
default_profile = "lightweight"

[maintenance.profiles.lightweight]
route = "luna"
max_input_tokens = 12000
max_output_tokens = 1600
timeout_seconds = 45
max_attempts = 2

[maintenance.tasks.summarize]
profile = "lightweight"
execution = "async"

[maintenance.tasks.enrich]
profile = "lightweight"
execution = "async"

[maintenance.retention]
automatic_source_erasure = false
```

`luna` is the deployment's configured lightweight route, not a promised universal
provider/model ID. Setup should prefer the available Luna route and require an
explicit mapping when unavailable. Model selection and per-task/per-job overrides
are user-configurable. Never silently borrow the foreground conversation model,
switch providers, or fall back to a different paid endpoint.

Callers may choose only profiles permitted by their execution policy. Custom
provider/model/endpoint configuration requires profile-management authorization.
Before every dispatch, validate that the approved resolved route satisfies the
processing/egress policies of every input authority. Read permission alone does
not authorize disclosure to an arbitrary provider. Unavailable or forbidden
routes produce a visible blocked job.

Persist the requested profile, resolved provider/model, endpoint policy,
capabilities, prompt/output-schema revisions and configuration digests in the job.
Credentials remain protected references, outside prompts, results and traces.
Changing a profile affects new jobs; it does not silently rewrite queued intents.

Model workers return bounded typed proposals. They have no generic shell, delete,
grant-management or publication tool. The coordinator validates and applies only
the explicitly permitted output class. Target IDs must belong to the frozen
authorized input/target manifest; valid JSON does not authorize an invented
source, claim, scope or destination. RLM execution is a separate later mode.

## 8. One job contract for synchronous and asynchronous work

Kinds include `summarize`, `checkpoint-context`, `assess-enrichment`, `enrich`,
`plan-pruning`, `expire-thread`, `apply-retention` and `execute-purge-intent`.
The last three use deterministic policy/processes; they do not require an LLM.

Submission freezes:

- operation/job UUID and canonical request digest;
- requester/credential binding, grant ceiling, input/output scopes and policy revision;
- exact source/thread/claim revisions, digests, coverage watermark and destination;
- model profile and resolved execution configuration where applicable;
- source count, input/output tokens, wall time, attempts, concurrency and cost limits;
- parent job, trigger identity and deterministic child-effect IDs.

Async returns the durable receipt immediately. Sync waits on that same job within
a caller wait deadline. A wait timeout returns its ID and current state; it neither
creates another job nor implicitly cancels work. Explicit cancellation stops new
dispatch after a durable cancellation barrier. Report acknowledged effects and
dispatched-but-unresolved effects separately: cancellation cannot recall a remote
request or prove zero billing. Reconcile uncertain child-effect IDs through their
existing exact-replay/receipt protocols rather than generating replacement effects.

Execution state is `queued → running → succeeded | failed | cancelled | blocked | stale`.
Approval and application are separate fields. A succeeded assessment may contain
an unapplied proposal. A succeeded job never means erasure is complete unless its
typed lifecycle receipt proves every required stage.

Use existing database-clock leases and fencing for proposal/effect commits.
Do not hold a database transaction across inference or HTTP requests. Heartbeat
the lease independently; an old fence cannot commit after ownership changes.

Inference is at least once. Persist dispatch attempts and reserve budget before
calling a provider. A lost response may still be billed; record unknown usage
and conservatively retain its reservation. The first durably accepted output is
immutable. Retrying its application reuses that output and deterministic effects,
rather than asking a model to regenerate it.

Revalidate live authorization, source visibility, digests, tombstones and policy
before reading frozen inputs and immediately before every provider/connector
dispatch, including retries. Revalidate again, together with scope flow, before
accepting/applying effects. Revocation while queued must prevent the first model
call. Expired/revoked execution authority blocks work; renewal requires an
authorized recorded action preserving the original grant ceiling and intent.
A matching job or thread UUID grants nothing.

A summary through watermark N can remain useful after N+1 is appended. It must
remain labeled partial coverage and cannot replace current context as though it
covered the newer head. Retraction or invalidation of an input blocks dependent
application. Retries never silently expand the input snapshot.

## 9. Scheduling and operational limits

Triggers are explicit commands, new-source watermarks, thread closure, inactivity,
configured schedules and impending compaction. Coalesce only identical canonical
semantic intents: complete input manifest, task parameters, output destination,
resolved model/prompt/schema configuration, policy revision and authorization
binding. Wait mode/deadline may differ without changing the job identity.
Cross-principal result reuse would require a separately authorized cache contract.
Further messages produce a later bounded snapshot, not duplicate jobs for the same
intent.

Admission uses per-authority/user/profile concurrency, fair queues, per-scope
serialization where effects overlap, source/token limits, deadlines, daily budget
reservations, circuit breakers, bounded job age and retry backoff. Unknown provider
pricing is not zero cost: a required currency cap needs supported accounting or
an explicitly conservative reservation policy.

No model calls are added to every Pi startup. Asynchronous preparation should keep
common compaction work ready before the context window fills. Foreground waits
are explicit and bounded; queue/budget/permission failures remain inspectable.

For local SQLite, scheduling and queue ownership stay inside the single daemon.
Native model subprocesses receive a bounded job and narrow credential channel;
they do not open a second authority daemon over the same storage. With PostgreSQL,
separate worker processes/Deployments may claim the same fenced queue.

Helm may add an optional worker Deployment and scheduled submission CronJobs.
CronJobs submit idempotent requests with task-specific credentials; they do not
run database deletion scripts or expose an administrator token to a model.
Interactive CLI and native Pi commands use the same submission/receipt APIs.

## 10. Context archival, working sets and compaction

Private working-context archival is distinct from reusable-knowledge capture and
shared publication. A policy declares event classes, storage placement, audience,
retention and omissions. Saving working context does not promote every utterance,
tool log or idea into long-lived agent knowledge.

A context bundle records ordered typed events, original entry/parent/branch IDs,
tool call/result pairing, content blocks, attachment/blob references and hashes,
qualified scope provenance, completeness and omitted ranges. Oversized events use
bounded chunks and a manifest. Referenced temporary files require durable copies
or an explicit nonrecoverable omission; a path alone is not preservation.

Archive only surfaced, permitted data. Do not capture provider-internal hidden
state or credential stores. Redaction/exclusion is represented as an omission,
not silently labeled complete. Recovery reconstructs the captured transcript and
working artifacts; it does not promise deterministic re-execution of the agent.

Archive durability and transcript coverage are separate statuses. All required
chunks may be acknowledged while an attachment or event class was intentionally
omitted. The handoff names exact recoverable eviction ranges; managed recoverable
compaction cannot evict an uncovered range under an unqualified complete receipt.
Blob handles are authorized scoped references, not capabilities inferred from a
content hash. Payload deduplication must preserve ownership and lifecycle accounting.

Keep the current request, current governing instructions, active constraints,
pending actions, unresolved questions, necessary evidence and complete tool groups
in the working set. Move older detail to recoverable storage with a small
orientation summary and source handles. Historical instruction text is evidence,
not an instruction to reinstall when a thread resumes. Reading archived tool
results does not re-execute the corresponding side effects.

The memory-backed compaction protocol is:

1. Freeze the branch/head, exact event manifest, coverage, exposure and request ID.
2. Persist all required chunks and the manifest; obtain a durable completeness receipt.
3. Produce or reuse a validated summary/checkpoint over those exact inputs.
4. Persist a prepared handoff with the artifact, source watermark and intended
   harness compaction details.
5. Verify branch/head and current authority again. Then let the harness replace
   active context, retaining unresolved items and resolvable source handles.
6. Record activation after the harness acknowledges its own compaction entry.

Pi's `session_before_compact` can cancel or supply a custom result; its `context`
hook can select messages for one model call. Use these native interfaces rather
than pretending an MCP tool can control the entire context window.

Pi and the memory journal cannot commit atomically. Put operation/bundle IDs in
the compaction details and reconcile prepared handoffs against Pi's actual session
entries after restart. A prepared but unused artifact is harmless. A lost reply
reuses the frozen manifest and accepted output.

Missing required chunks, failed persistence, changed branches, invalid summaries
or cancellation keep the original working context. In managed memory-backed mode,
an unacknowledged local spool is not sufficient permission to evict material. A
separate explicit fallback may retain ordinary Pi-managed history/compaction, but
must report that Memory Worlds offload did not complete. No silent safety downgrade.

Ordinary Pi compaction retains original entries in its session file. Context-window
eviction, Pi file retention and Memory Worlds retention remain separate policies.
Summaries have bounded depth; periodically regenerate from covered originals rather
than repeatedly summarizing summaries until details and uncertainty disappear.
Context replacement, forks, reload and pruning do not reset invocation exposure.
Carry the persisted ledger through every handoff; an incomplete ledger is unknown
exposure. A clean invocation needs a genuinely fresh authorized context and inputs.

## 11. Expiration, pruning and purge

Use server-time predicates for expiry in automatic selection. A delayed scheduler
cannot make an expired object eligible again. Explicit authorized reads/reopening
are distinct from automatic recall. If a policy requires read removal, apply the
existing retraction/erasure mechanism rather than relying on eventual cleanup.

Thread inactivity expiry, context-artifact TTL and claim `review_after` have
different meanings. A stale claim can request review without becoming false or
being deleted. Low retrieval frequency alone is not authority to erase evidence.

Pins protect declared active recovery dependencies within policy limits. They are
bounded leases, not a way to defeat mandatory deletion. Purging required inputs
must either be deferred by an authorized hold or invalidate dependent context
artifacts; do not continue claiming they are reconstructible.

Pruning produces a frozen bounded plan, an explanation, exact target revisions,
dependency effects and a policy digest. Model usefulness judgments are advisory.
Nondestructive view/artifact retirement changes retrieval eligibility without
erasing sources. Source-destructive targets apply separately through
`EpisodeLifecycle`/`RetentionEngine` and deterministic child operation IDs;
preserve current tombstones and exact replay rejection.

Retraction/erasure also affects retained derived copies. The new dependency
registry must invalidate or hide affected summaries, context packages, claims,
enrichment outputs and caches before they can reveal removed material. Recheck
dependencies on reads while any bounded cascade is pending. Lifecycle planning
must include payload-bearing job inputs/results and archival blobs, not only
the original episode row. Minimal replay/tombstone receipts must not retain text.
The first artifact release includes read fences and cleanup participation for
these copies. Erasure cannot report complete while required payload cleanup is
pending. Scheduled cascades and automatic batch execution are later automation,
not prerequisites deferred beyond the first usable erase path.

Graph cleanup remains separately receipted. Preserve drained-worker maintenance
and namespace inventory before executing purge intents; lease expiry alone does
not prove a remote graph request has stopped. Initial purge workers operate only
inside that explicit maintenance protocol. Unattended online graph purge requires
a separately implemented and qualified quiescence mechanism.

Receipts distinguish source changes, dependent-copy cleanup, graph cleanup and
complete/live-authority erasure. Backups, filesystem history, external model/tracing
services and independently published copies have separate retention boundaries.
Never promise that deleting a local source erases all independent copies.

## 12. Lossless and semantic compression

Canonical event bytes and their digests define recoverable content. A bounded
standard lossless codec can reduce stored chunks; record codec, uncompressed
length and digest, and enforce compressed/decompressed byte and ratio limits.
Retrieval decodes through the authorized service, not a model-visible filesystem.

Semantic summaries are lossy derived artifacts. Report storage bytes, token counts,
covered source ranges, omissions and recovery/answer quality separately. A smaller
summary is not automatically better. Evaluate exact constraints, provenance,
contradictions and task continuation alongside compression ratio and latency.

## 13. RLM execution boundary

Here RLM means Recursive Language Models. The original
[paper, version 3](https://arxiv.org/html/2512.24601v3) and
[implementation](https://github.com/alexzhang13/rlm) describe external context that
models can inspect programmatically and decompose through recursive model calls.
Ordinary summaries, retrieval and subagent calls alone do not establish that mode.

First build useful memory-backed context tools: inspect manifests, search selected
threads, read bounded spans and expand source references. A later explicit RLM
executor can expose those through an isolated programmatic environment, with
intermediate values stored outside the root model's active window.

The executor requires a broker enforcing live grants on every read, explicit
delegated child credentials, private invocations, and a common descendant budget
for depth, calls, concurrent work, input/output tokens, cost, time and errors.
Budget reservations must compose across parallel children. Partial or exhausted
work cannot silently become accepted facts or a complete compaction artifact.

Do not run the upstream default host execution environment inside the credential-
bearing daemon. Give the isolated executor bounded source handles and brokered
operations; omit administrator tokens, unrestricted filesystem/network access and
inherited host callables. Journal inputs, accepted intermediate artifacts and
receipts; a live REPL is not the durable state.

Exposure is cumulative across descendants and recorded before results reach a
model. Tracing defaults to IDs/operational metadata; raw context, prompts and
intermediates require an explicit content policy. Published RLM benchmark gains
are not a quality, latency or cost promise for this system.

## 14. Proposed API and harness surface

Names below are design contracts, not current commands or endpoints:

- Thread create/list/show/close/reopen, with UUIDs, aliases and explicit scope grants.
- Context archive/checkpoint/expand, with completeness and handoff receipts.
- Claim propose/revise/show and enrichment flag/list/resolve.
- Maintenance submit/status/wait/cancel/list with one sync/async job protocol.
- Pruning/retention preview/apply and purge-intent status under lifecycle authority.

The CLI, Python client, native Pi library/tools and MCP expose the operations
appropriate to their roles. Automatic context replacement needs a native harness
hook; exposing tools does not imply that Codex, Claude or OpenCode will invoke them
on each turn. Add and qualify each harness adapter separately.

Pi should show compact bottom-bar maintenance state and explicit notifications for
blocked preservation. Detailed queue, model, coverage, enrichment and lifecycle
receipts live in commands/tools. Avoid permanent transcript widgets or startup
catalog/model calls for maintenance.

## 15. Delivery and acceptance

Implement the current contracts directly. No old-schema readers, migrations,
compatibility harnesses or parallel obsolete execution paths are proposed.

| Stage | Deliverable | Required qualification |
| --- | --- | --- |
| 1 | Thread/privacy contract, typed archives, profiles, durable jobs, summaries and baseline dependency lifecycle | Replay, isolation, chunk recovery, fenced commits, model bounds and deleted/revoked-input read fencing with payload cleanup |
| 2 | Claim revisions, enrichment flags and grounded results | Evidence attribution, uncertainty, stale-input conflicts and scoped disclosure |
| 3 | Context working sets and native Pi compaction handoff | Preservation barrier, branch changes, both crash windows and restart reconciliation |
| 4 | Scheduled expiry/pruning and automated lifecycle workers | Server-time selection, frozen plans, bounded cascades, tombstones and source/graph receipts |
| 5 | Isolated bounded RLM executor | Broker isolation, recursive exposure, shared budgets, partial results and recovery |

Minimum tests include concurrent duplicate submission, changed-intent conflicts,
sync/async identity, lost acknowledgements, provider usage uncertainty, worker death,
stale fences, grant revocation, source deletion during inference, exact Unicode
chunk round trips, omitted modalities, tool pairing, mixed-audience rejection,
summary coverage through watermark N, fork/compaction reconciliation, retention of
contradictions, dependency cleanup, nonresurrection and descendant budget exhaustion.

Use synthetic/local provider fixtures and current-contract Pi runtime tests.
Evaluate real summary/enrichment quality separately on an explicit nonprivate corpus
before choosing production defaults. Record cost, latency, task recovery, unsupported
claims, retrieval usefulness and token/storage reduction. Do not equate a schema-
valid model output with a good summary.

## 16. Review decisions and confidence

Recommended decisions are: a true thread-private boundary across invocations;
private working-context archival separate from knowledge capture; lightweight/Luna
as the default selectable model profile; async maintenance with explicit bounded
sync waits; and deterministic authorized erasure. The alternative thread collection
model is documented in section 4 for review.

Confidence is high in the inspected reuse boundaries and required invariants.
The new records, concurrency protocol and model quality remain design proposals
until implemented and tested. This PR changes documentation only and does not
enable workers, alter grants, delete sources or modify the running installation.
