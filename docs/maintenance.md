# Private thread archives and maintenance workers

[Documentation](README.md) · [Scopes](scopes.md) · [Lifecycle](lifecycle.md)

Create a private thread, archive explicitly selected context, then submit a
summary or enrichment assessment. The service stores recoverable context in the
thread's source journal and runs bounded model work against the exact selected
bundles. Summaries remain tentative, attributed artifacts in the same scope.

This implements explicit archives and jobs. Automatic Pi context replacement,
scheduled expiration/pruning, a claim registry and recursive RLM execution remain
in the [maintenance proposal](proposals/memory-maintenance.md).

## Configure a worker

Maintenance models use their own operator-defined profiles. The request default
is the logical `lightweight` profile; configure it to use your approved Luna route
or another lightweight model. `luna` is a route label, not a universal model ID.
Neither foreground Pi authentication nor Graphiti extraction settings supply an
implicit maintenance model.

Copy [the profile example](examples/maintenance.toml) to a private directory,
set its endpoint, model and absolute key-file path, and protect both files with
mode `0600`. The daemon reads:

```dotenv
MEMORY_WORLDS_MAINTENANCE_CONFIG=/absolute/private/config/maintenance.toml
```

Restart the daemon after changing its environment or profile file. Existing
queued jobs retain their original configuration digest; if the loaded profile
changes, they block with `model_profile_changed`. Submit a new operation UUID to
use the changed configuration. Missing profiles produce
`model_profile_unavailable`, without a model call.

For Kubernetes, use the Helm chart's optional
[`maintenance.existingSecret` configuration](reference/helm-values.md#maintenance-worker-profiles).
It stages the TOML and keys into private owned files; rotating that Secret
requires an explicit rollout restart. Allow the model endpoint through the API's
egress policy.

Only explicitly configured OpenAI-compatible chat-completions endpoints are
supported for these workers. Remote endpoints require HTTPS; loopback HTTP is
allowed. Redirects and ambient proxy configuration are disabled. Keys are read
from protected files and never placed in prompts, receipts or job rows.

The profile bounds request size, output tokens, timeout, attempts, concurrent jobs
and daily token reservations. Input selection is bounded to 1 MiB of serialized
context and 1,000 complete source dependencies before inference. `max_input_tokens` currently uses UTF-8 request bytes
as a conservative admission estimate; it is not an exact tokenizer count. Each
dispatched attempt reserves its full input/output allowance. Reservations remain
charged to that day's admission budget, including requests with unknown outcomes.
This is bounded execution accounting, not exact provider billing or a currency cap.
Set budgets large enough for the configured per-attempt allowance.

Optional `allowed_user_ids` and `allowed_world_ids` restrict profile use. An empty
list adds no restriction beyond the caller's live memory permissions. Configure
only endpoints approved to process the selected authority's data.

## Create and discover a thread

Use an ordinary user credential with write access to the selected world. The
commands use `/private/agent.token` as its protected token file; replace that
path with yours. The default service URL is `http://127.0.0.1:8787`; set
`MEMORY_WORLDS_URL` or add the global `--url` argument for another service.

Save a request as `thread.json`, using a new operation UUID and a discovered
world ID:

```json
{
  "operation_id": "f49e24da-f088-498e-8c3a-0c933f045efb",
  "thread_id": "e34ba59d-4661-4e2e-8736-5c5a0adfc3c8",
  "world_id": "YOUR_WORLD_ID",
  "name": "Fixture investigation",
  "alias": "fixture-investigation"
}
```

```console
mise run cli -- --token-file /private/agent.token thread create --input thread.json
mise run cli -- --token-file /private/agent.token thread list
mise run cli -- --token-file /private/agent.token thread show THREAD_UUID
```

Creation returns a thread UUID and a distinct `thread-private` scope UUID. New
invocations need explicit grants to that scope. Matching thread/agent names,
parentage or UUIDs do not grant access. `thread list --include-expired` includes
authorized expired threads. Expiry removes a thread from default thread discovery
and blocks writes and new jobs; it does not erase it or prevent explicit
source/graph reads under live grants. Native Pi automatic scope selection does
not yet consult thread expiry. A new thread starts at revision `0`.

Thread grants currently belong to individual credentials. Use a stable ordinary
agent credential when a thread must remain accessible across agent invocations.
Refreshing an OAuth/OIDC access token creates a new session credential; it does
not inherit the previous session's thread grants, even for the same signed-in
user. An existing live scope manager must explicitly grant the new credential
access. Arrange that handoff before the previous manager expires, or retain
another authorized manager. Logging in again, reusing a name or presenting the
same thread/agent UUID cannot recover access by itself.

Queued jobs retain their submitting credential. Granting a new session access to
the thread permits authorized inspection and new submissions; it does not renew
the old session, transfer cancellation rights or rebind an existing job's
execution authority. Durable OIDC identity grants and explicit job-authority
renewal require a separate authorization design.

## Archive selected context

Save an archive request as `context.json`:

```json
{
  "operation_id": "6c375963-fb8f-45d9-acaa-9c95a7f9553e",
  "expected_revision": 0,
  "entries": [
    {
      "id": "entry-1",
      "type": "message",
      "role": "user",
      "payload": {"text": "Measure bearing runout before choosing the fixture."}
    }
  ],
  "omissions": [],
  "source_refs": []
}
```

```console
mise run cli -- --token-file /private/agent.token context-archive THREAD_UUID --input context.json
mise run cli -- --token-file /private/agent.token context-read THREAD_UUID BUNDLE_UUID
```

The receipt returns the bundle UUID, committed thread revision, digest, coverage
and exact source references. Reuse the complete original request on a retry;
changing events, timestamps or revision under the same operation UUID conflicts.
Use a new UUID and the current thread revision for the next archive.

Supported event types are `message`, `tool-call`, `tool-result` and `custom`.
Tool events use `payload.call_id`; a result must follow its captured call. Input
is bounded to 200 events and 750 KiB. The service stores gzip/base64 chunks and a
manifest as durable source episodes, without extracting raw tool logs through
Graphiti. Text, Unicode and structured payloads round-trip through these bundles.

Coverage describes the submitted events and declared omissions. It does not
prove that the caller supplied every conversation event or copied an external
attachment. Include surfaced attachment contents explicitly, or record an
omission with reason `excluded`, `unsupported`, `unavailable` or `redacted`.
A temporary file path is not a durable attachment copy.
An archive receipt alone does not authorize evicting a conversation range: a
host adapter must additionally establish exact branch/range coverage and retain
pending tool groups. That native handoff is following work.

`source_refs` optionally attributes existing same-scope evidence using its world,
scope, episode ID and digest. Cross-scope inputs are rejected. Those references
and their derived dependencies are checked on recovery; archiving context does
not authorize publication to an agent-wide, team or company scope.

## Submit, wait and inspect

Save a job request as `job.json` with the returned thread and bundle UUIDs:

```json
{
  "operation_id": "c6486c82-ccf5-4a6c-99c3-f794aa03c33a",
  "thread_id": "e34ba59d-4661-4e2e-8736-5c5a0adfc3c8",
  "bundle_ids": ["6c375963-fb8f-45d9-acaa-9c95a7f9553e"],
  "profile": "lightweight",
  "kind": "summarize"
}
```

```console
mise run cli -- --token-file /private/agent.token maintenance submit --input job.json
mise run cli -- --token-file /private/agent.token maintenance wait JOB_UUID --timeout 30
mise run cli -- --token-file /private/agent.token maintenance status JOB_UUID
mise run cli -- --token-file /private/agent.token maintenance artifact ARTIFACT_UUID
mise run cli -- --token-file /private/agent.token maintenance list
mise run cli -- --token-file /private/agent.token maintenance cancel JOB_UUID
```

`summarize` preserves selected evidence, unresolved questions and contradictions.
`assess-enrichment` instead returns a tentative assessment with flags referencing
the supplied bundles. It does not fetch new evidence or create a verified fact.
The model cannot choose a destination, invoke tools, publish, change grants or
delete sources. Results remain in the thread's scope and Graphiti projection is
queued independently of the artifact's source durability.

Submission returns immediately. Waiting observes the same durable job, for at
most 60 seconds; timeout does not cancel or create another job. Exact request
replay uses the original operation UUID and submitting credential. Cancellation
also requires that credential. A currently authorized credential of the same
user can inspect the job and its sources.

Receipts report `state`, `attempts`, a sanitized `error`, `result_artifact_id`,
the guarded `result`, `usage_uncertain` and `unresolved_effects`. States are
`queued`, `running`, `succeeded`, `failed`, `cancelled`, `blocked` or `stale`.
Cancellation stops new dispatch; already dispatched provider or artifact work can
remain unresolved and may still be billed.

The service rechecks read/write authority before input reads, provider dispatch
and artifact creation. Revoked permissions, deleted evidence or changed source
revisions cannot silently substitute different inputs. Workers renew leases while
inference runs; stale owners cannot commit artifacts. An accepted artifact has a
stable identity, so a lost acknowledgement is reconciled before another model call.

## Clients and lifecycle

The Python `Client` exposes `create_thread`, `threads`, `thread`,
`archive_context`, `context_bundle`, `submit_maintenance`, `maintenance_jobs`,
`maintenance_job`, `wait_maintenance`, `cancel_maintenance` and
`maintenance_artifact`.

MCP exposes thread discovery/inspection, bundle recovery and explicit maintenance
submit/status/wait/cancel/artifact tools. Creation and archival use the HTTP,
Python or CLI interfaces. Tools can recover detail on demand; they do not install
native compaction hooks into a host's context manager.

Retraction or erasure of an input hides dependent artifacts immediately and
includes their payloads in lifecycle cleanup. Bundle receipts identify all chunk
and manifest sources; selecting one source ID alone is not a request to erase an
entire unrelated thread. See [lifecycle operations](lifecycle.md) for explicit
source targets, manager permissions, graph purge and separate backup retention.

Thread revision membership is cumulative and bounded. Removing an archived
member invalidates snapshots that depend on it and prevents further appends to
that head. Create a new thread after removal; a revision-pruning/omission protocol
for resuming that same thread has not been implemented. Portable `.mworld`
exports retain source dependency closure but do not recreate thread/bundle/job
receipts; an encrypted whole-authority backup preserves those control records.

Local SQLite keeps the queue inside the single daemon. PostgreSQL replicas use
the same durable job rows and database leases. The queue processes explicit jobs;
this release does not configure scheduled expiry, autonomous purge, background
web crawling, automatic context eviction or an RLM execution environment.
