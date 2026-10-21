# Agent and subagent scopes

A world is an ownership and organizational boundary. Its scopes identify
audiences inside that boundary. The service checks both the world and the scope
before accessing source evidence or a graph projection. Knowing a UUID does not
grant access.

| Scope kind | Purpose | Default audience |
| --- | --- | --- |
| `world-shared` | Published knowledge for a world | Authorized world participants |
| `run-shared` | Task brief and selected coordination results | Explicitly granted participants |
| `agent-persistent` | Specialist memory across invocations | Explicitly granted agent credentials |
| `invocation-private` | Working notes for one invocation | That invocation's credential |

The service issues scope, run, and invocation UUIDs. A stable agent UUID can be
reused across invocations; its name or UUID never confers access to a persistent
scope. Parent invocation IDs describe orchestration, not inherited permission.
Siblings and parents cannot read a child's scratch by default.

A context records selected read scopes, one optional write scope, agent UUID,
and invocation UUID. Binding a context checks access and records defaults and
attribution; it does not restrict the underlying credential. Supplying
`context_id` on capture checks the exact write destination and records the
server-owned invocation identity. Child tools use a delegated credential when
their parent has broader access.

## Create and delegate

Use an ordinary world-writer credential for agent work. Save structured inputs
outside the checkout. This input creates a run scope with a server-issued UUID:

```json
{"kind":"run-shared","name":"Fixture investigation"}
```

```sh
memory-worlds --token-file /private/parent.token \
  scope-create WORLD_ID --input /private/run.json
memory-worlds --token-file /private/parent.token scopes WORLD_ID
```

An orchestrator can use the typed Python client:

```python
from datetime import UTC, datetime

from memory_worlds.client import Client
from memory_worlds.models import (
    DelegateCredential,
    EpisodeInput,
    ScopeGrant,
    ScopeKind,
    SearchRequest,
)

with Client(service_url, parent_token) as parent:
    shared = next(
        scope for scope in parent.scopes(world_id) if scope.kind == ScopeKind.WORLD_SHARED
    )
    receipt = parent.delegate(
        DelegateCredential(
            private_world_id=world_id,
            grants=[ScopeGrant(world_id=world_id, scope_id=shared.id, operations=["read"])],
        )
    )
    try:
        with Client(service_url, receipt.token) as child:
            target = receipt.context.write_scope
            saved = child.capture(
                world_id,
                EpisodeInput(
                    scope_id=target.scope_id,
                    context_id=receipt.context.id,
                    source_id="stable-task-note-1",
                    content="An unreviewed finding for this invocation.",
                    occurred_at=datetime(2026, 10, 21, tzinfo=UTC),
                ),
            )
            evidence = child.search(
                SearchRequest(
                    scopes=receipt.context.read_scopes,
                    query="finding",
                )
            )
            # Return selected findings through the orchestrator's result channel.
            # Keep receipt.token and the parent transcript out of that channel.
    finally:
        parent.request("DELETE", f"/v1/credentials/{receipt.credential_id}")
```

The default child can read its delegated scopes and read/write its new private
scope. It cannot publish globally, manage credentials, inspect federation
deliveries, or inherit its human user's remote mount access. Further delegation
requires `allow_delegation` and explicit delegable scope operations. Descendant
lifetimes cannot exceed ancestors. Every request rechecks ancestor revocation,
identity expiry, world permission, and delegated scope operations.

Set `coordinator_read: true` explicitly when creating a child if its direct
parent must inspect the complete scratch. This grants read access to that new
scope without write, management, or onward delegation rights. The default is
selected-result return only.

The CLI equivalent is `agent-delegate --input /private/child.json
--token-output /private/child.token`. Its token destination must be a new file;
the CLI creates it with mode 0600 and omits the bearer token from stdout.

## Read and publish independently

`search QUERY --select WORLD_ID:SCOPE_UUID` accepts repeated selections.
`search QUERY --world WORLD_ID --scope SCOPE_UUID` selects one scope.
`capture`, `publish`, `status`, and `graph-rebuild` accept `--scope`; capture and
publication also accept `--context` for bound invocation attribution.

World-only requests select world-shared memory exclusively. A private scope
never appears in a world-shared search, episode list, or export. An
unauthorized selection rejects the complete request before any backend query.
Restricted children cannot recover shared access by omitting the scope UUID.

Publication creates a new destination episode with selected text. A scoped
publication can reference authorized source evidence, recording its digest and
provenance without copying the transcript. Source records retain their scope.
Sharing into a run or world requires the destination's `publish` operation.
Reads never select or widen the write destination.

Native Pi [chat sharing modes](chat-sharing.md) can authorize selected automatic
publication from one private source under a user-enabled per-chat rule. The rule
pins the destination and classifier/category/exclusion policy. It does not grant
scope access, expose another invocation's scratch, or change the separate private
capture flow check. Review mode requires approval of an individual excerpt.

For automatic capture, `scope-flow --input FILE.json` checks whether every
current reader of a selected write scope can also read every source scope.
The HTTP endpoint is `POST /v1/scopes/flow`; it returns only `allowed`, without
reader names or counts. This permits shared-run context to flow into child
scratch when the actual grants allow it, and rejects broader destinations or
extra coordinator readers without source access. Pi rechecks before uploading
queued captures. Unavailable or excessively large audience checks fail closed.
This is a check of current grants; later explicit sharing decisions change the
destination's audience.

## Graphiti extraction

Graphiti is the graph engine for every projected scope, whether the service
runs locally, through SSH, in kind, or on shared Kubernetes. SQLite stores the
authoritative source, authorization, contexts, and receipts. Source-only capture
and search remain available independently of Graphiti.

Additional scopes have separate source journals and graph namespaces derived
from authority, world, scope UUID, and generation. Extraction uses previous
episodes from the same scope only. Entity summaries and facts cannot combine
private and shared input during ingestion. Retrieval combines only authorized
projections and retains scope provenance. Graph search supports entities and
relationship facts.

Every scope uses the same namespace derivation, including world-shared memory.
Unknown writes degrade the affected scope rather than siblings;
rebuilds also target one scope. Every capture queues extraction by default. Explicit source-only capture is an
operator or agent recovery choice, and graph failures never discard source.

## Native Pi graph tools

The native extension supports selected read scopes, a private write scope,
project/global aliases, and invocation attribution. Its exported orchestrator
API supplies distinct child credentials and fresh sessions with explicit task
briefs and selected evidence. Scope exposure persists through branching and
compaction. Automatic capture stops when its destination audience is broader
than context already seen; explicit user publication remains separate.

See the [native package](../packages/pi/README.md) for commands and SDK exports,
and the [Pi Graphiti comparison](pi-graphiti-features.md) for supported
upstream actions and intentional differences.

## Persistence and archives

The world-shared journal remains `worlds/WORLD_ID/source.sqlite3`. Additional
journals are `worlds/WORLD_ID/scopes/SCOPE_UUID/source.sqlite3`. All control data
and journals remain on the API data volume. No pod or PVC per agent is required.
Pod replacement retains scope identities, delegations, and source records.

`export WORLD_ID FILE.mworld --scope SCOPE_UUID` produces a version 2 archive
with that scope's source, audit, and audience metadata. This is the single
supported archive format. Private archives require explicit private restore targets;
they cannot silently become world-shared. Restore rebinds the destination scope,
retains original provenance, and restores neither credentials nor live context
bindings. Rebuild graphs separately.

Credential expiry stops access without deleting source. The current service
does not automatically prune expired invocation journals. Use explicit
coordinator inspection when an authorized reader must remain after child
revocation. Whole-store operator backups include private journals and need the
same protection as the data volume.
