# Retract, erase and retain memory

[Documentation](README.md)

Lifecycle operations target explicit episode IDs within one authority, world and
scope. A world manager also needs scope management permission; an administrator
can perform authority-wide maintenance. A separately published copy has its own
lifecycle at its destination.

## Choose an action

| Action | Source result | Replay behavior |
| --- | --- | --- |
| Retract | Hidden from normal lookup, search, export and extraction; retained for maintenance | The source identity remains tombstoned |
| Erase | Retained payload, metadata and projection job removed | The source identity remains tombstoned |
| Retain by policy | A bounded, frozen set of old sources, expired invocations, terminal deliveries or retired generations is selected | Retry resumes that exact plan |

Erasure can follow retraction. An erased source cannot be restored by replaying
its old capture or import request. Preserve tombstones and lifecycle receipts
when backing up an authority.

## Preview, apply and check the receipt

Use a new operation UUID for a new request. Preview validates targets without
writing and reports IDs rather than source contents. Apply accepts at most 100
unique episode IDs; retries with the same UUID must use the same action and targets.

| Operation | HTTP route |
| --- | --- |
| Preview | `POST /v1/worlds/{world}/scopes/{scope}/lifecycle/preview` |
| Apply | `POST /v1/worlds/{world}/scopes/{scope}/lifecycle` |
| Receipt | `GET /v1/worlds/{world}/scopes/{scope}/lifecycle/{operation_id}` |

The receipt separates source cleanup from graph cleanup. Check `source_state`,
`source_checkpointed`, `graph_state`, `pending_generations` and `erasure_complete`.
A source commit alone does not establish that external graph data was removed.
See the [Python examples and receipt contract](reference/lifecycle.md).

## Finish graph cleanup

Source changes queue purge intents and block affected graph generations. Stop
and drain workers across all replicas before running operator graph purge with
`--workers-drained`. That flag records an operator assertion; it does not stop
remote processes. Purge is not a live HTTP endpoint.

Retry failed cleanup, then rebuild from the remaining visible evidence before
resuming extraction. See [Graphiti operations](graphiti.md) and the
[purge procedure](reference/lifecycle.md).

## Apply a retention policy

Retention starts only when an administrator submits explicit cutoffs. It can
select old source records in named scopes, expired invocation journals, terminal
outbox envelopes and retired graph generations. It preserves live invocations,
persistent agent scopes, pending deliveries and active/building generations.

Preview the plan, apply it with a new operation UUID, and follow its receipt.
The default limit is 100 targets and the maximum is 500; `more_available` indicates
that another operation may be needed. Retention does not schedule itself.
See the [policy fields and examples](reference/lifecycle.md).

These operations cover the live authority and inventoried graph namespaces.
Backups, filesystem history, PostgreSQL archives and independently published
copies need their own retention policies. See [authority recovery](recovery.md).
