# Lifecycle and retention reference

[Documentation](../README.md) · [Guide](../lifecycle.md)

Every lifecycle operation names one authority, world, scope and explicit episode
IDs. A live world manager also needs the scope's `manage` permission. An
administrative credential can perform operator maintenance across that authority.
An episode's private source reference gives no authority over an independently
published copy in another scope or service.

Retraction removes evidence from ordinary episode lookup, listing, search,
exports and graph extraction. Its original source record remains retained for
authorized lifecycle maintenance. Erasure deletes the retained episode payload,
metadata and projection job. Both actions write a minimal tombstone containing
the source and episode identities, owner, action time and operation ID. Capture,
import and exact replay reject a tombstoned source identity. Erasure cannot be
reversed by changing the operation ID or importing the original content again.

Use a fresh UUID for a new operation. Preview validates every target before
writing anything and reports IDs and affected generation names without source
content. Apply accepts at most 100 unique episode IDs. An exact operation replay
returns its durable receipt; reusing the UUID with different targets or a
different action fails. A retraction can later progress to erasure. An erased
record cannot return to a retained or visible state.

The Python interface is:

```python
from uuid import uuid4

from memory_worlds.lifecycle import EpisodeLifecycle, LifecycleRequest

lifecycle = EpisodeLifecycle(store)
request = LifecycleRequest(
    operation_id=uuid4(),
    action="erase",  # or "retract"
    episode_ids=[episode_id],
)
preview = lifecycle.preview(principal, world_id, scope_id, request)
receipt = lifecycle.apply(principal, world_id, scope_id, request)
receipt = lifecycle.receipt(principal, world_id, scope_id, request.operation_id)
```

The HTTP API provides preview and apply at
`POST /v1/worlds/{world}/scopes/{scope}/lifecycle/preview` and
`POST /v1/worlds/{world}/scopes/{scope}/lifecycle`. Retrieve a receipt at
`GET /v1/worlds/{world}/scopes/{scope}/lifecycle/{operation_id}`. These routes
check current credentials and grants on every call, including retries.

Source removal and graph removal are separate durable steps. A short authority
maintenance barrier drains known graph operations before taking the namespace
inventory and committing the tombstone, source change and purge intents. Normal
reads resume with the evidence hidden. Every generation that could contain the
evidence becomes unavailable for graph use until maintenance succeeds. A SQL
commit alone does not mean the external graph has been erased.

The receipt reports `source_state`, `source_checkpointed`, `graph_state`,
`pending_generations` and `erasure_complete`. For native SQLite storage, secure
deletion is enabled and the source WAL must truncate after the deletion commits.
A busy checkpoint leaves the operation incomplete and retryable. An erase is
complete only after source cleanup and every affected graph namespace purge
succeed. Provider errors are stored as fixed operational categories; provider
queries and source text are excluded from receipts.

Final graph purge is an operator operation. Stop and drain extraction workers
across every replica before supplying `--workers-drained`; it is an assertion
about the deployment, not a mechanism that stops remote processes. The operator
command takes the authority's exclusive maintenance lock, inventories namespaces
again and uses the configured adapter's exact namespace deletion operation.
It is intentionally unavailable as a live HTTP purge endpoint. With Python,
the maintenance equivalent is:

```python
result = await lifecycle.purge(
    administrator,
    world_id,
    scope_id,
    graph_adapter,
    workers_drained=True,
)
```

A failed namespace deletion leaves graph use blocked and can be retried. A
successful retry skips namespaces already purged. Rebuild the graph from the
remaining visible evidence before resuming extraction. A later purge does not
delete a new clean active generation merely because an older lifecycle receipt
exists. An adapter that cannot prove deletion of all derived data fails closed.

These receipts cover the live authority's source records and inventoried graph
namespaces. They do not promise cryptographic erasure of physical media,
filesystem history, PostgreSQL MVCC/WAL archives, old backups or independently
published destination copies. Those stores have separate retention obligations.
Preserve the minimal tombstones and lifecycle receipts when backing up the
current authority; deleting its tombstones would discard the nonresurrection
boundary.

Retention is disabled until an administrator submits an explicit policy. Each
policy accepts one or more of these independently selected cutoffs:

| Policy field | Targets | Preserved |
| --- | --- | --- |
| `source_before` and explicit `scopes` | Source records received before the aware UTC cutoff, including retained retractions | Unselected scopes and newer source records |
| `expired_invocations_before` | Invocation-private journals whose server-issued invocation credential expired before the cutoff | Live invocations, persistent agent scopes and scopes without an expiring invocation |
| `terminal_deliveries_before` | Old delivered or blocked outbox envelopes | Pending or processing delivery envelopes |
| `retired_generations_before` | Retired or failed graph namespaces | Active or building generations |

Cutoffs must include a timezone and cannot be in the future. The default batch
limit is 100 targets; the maximum is 500. Preview is content free. Apply first
persists the exact bounded target plan, then uses deterministic child lifecycle
operation IDs. A crash after one source journal commits can be resumed without
expanding that plan to newly eligible records. `more_available` indicates that a
new operation may be needed to process another batch.

```python
from datetime import UTC, datetime
from uuid import uuid4

from memory_worlds.lifecycle import RetentionEngine, RetentionPolicy, RetentionRequest
from memory_worlds.models import ScopeSelection

retention = RetentionEngine(store)
request = RetentionRequest(
    operation_id=uuid4(),
    policy=RetentionPolicy(
        scopes=[ScopeSelection(world_id=world_id, scope_id=scope_id)],
        source_before=datetime(2026, 1, 1, tzinfo=UTC),
        terminal_deliveries_before=datetime(2026, 1, 1, tzinfo=UTC),
        limit=100,
    ),
)
preview = retention.preview(administrator, request)
receipt = retention.apply(administrator, request)
```

Retention preview/apply routes are `POST /v1/retention/preview` and
`POST /v1/retention`; receipt lookup is `GET /v1/retention/{operation_id}`.
An `applied` retention receipt means its frozen source/envelope cleanup and graph
purge intents committed. Graph deletion still requires the drained-worker
maintenance step. Retention does not schedule itself or silently choose a
retention period.

Terminal delivery cleanup removes the publication payload and captured
principal, and retains the minimal delivery identity, immutable input digest,
destination and receipt status. Exact enqueue replay still returns that receipt;
changed input conflicts and a pruned delivery cannot be retried. Deleting that
identity instead would allow cleanup to cause an old publication to be sent
again. Pending deliveries retain their complete envelopes so delivery can resume
without losing an authorized publication.
