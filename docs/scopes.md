# Agent and subagent scopes

[Documentation](README.md) · [Scope reference](reference/scopes.md)

A world is an ownership and organizational boundary. A scope defines an audience
inside it. An agent can read selected shared knowledge while keeping working
notes private, then publish a selected conclusion to a broader audience.

| Scope kind | Use it for | Access |
| --- | --- | --- |
| `world-shared` | Published knowledge for a world | Authorized world participants |
| `run-shared` | One task's brief, coordination and findings | Explicitly granted participants |
| `agent-persistent` | A specialist's notes across invocations | Explicitly granted credentials |
| `thread-private` | Recoverable context for a resumable conversation | Creating credential and explicitly granted readers |
| `invocation-private` | One invocation's working notes | Its credential, plus any explicit readers |

UUIDs distinguish scopes, agents, runs and invocations. Names and `@aliases` make
them easier to find; neither names nor UUIDs grant access. Parents and siblings
cannot read a child's private notes by default.

Create a resumable thread with `thread create`, which atomically creates its
private scope and grants. A generic `scope-create` cannot manufacture a thread
binding. Thread expiry hides it from default thread discovery and stops new
archives/jobs; explicitly selected source/graph reads still use live scope grants.
See [thread archives and workers](maintenance.md).

## Create and delegate

For a task with several agents, create one run scope and delegate its read access
to each child. Each child receives a separate invocation-private scope.

Save this JSON outside the checkout as `/private/run.json`:

```json
{"kind":"run-shared","name":"Fixture investigation","alias":"investigation"}
```

Create it using a credential allowed to create scopes in the world:

```sh
mise run cli -- --token-file /private/parent.token \
  scope-create WORLD_ID --input /private/run.json
mise run cli -- --token-file /private/parent.token scopes WORLD_ID
```

An orchestrator uses the typed client's `delegate` operation or
`agent-delegate --input /private/child.json --token-output /private/child.token`.
The grant list is explicit. The child cannot expand the parent's delegable
authority, and revoking a parent invalidates its descendants.

See the [complete Python example and delegation rules](reference/scopes.md#create-and-delegate).
For native Pi sessions, use the
[orchestrator SDK](reference/pi.md#native-subagents-and-review). It starts a fresh
session with a task brief and selected evidence, rather than copying the parent
transcript. Its SDK boundary is not an operating-system process sandbox.

## Read and publish independently

A context records selected reads, an optional write scope, and agent/invocation
attribution. Binding a context checks access; it does not narrow the credential.
Use a delegated credential to give a child a smaller permission ceiling.

```sh
mise run cli -- --token-file /private/agent.token \
  search "bearing material" --world WORLD_ID --scope SCOPE_UUID
```

World-only requests select the world's shared scope. They never include private
notes. An unauthorized scope rejects the complete request before backend access.

Publication creates a new episode at an authorized destination. It can retain
source provenance without copying or relabeling the private transcript. Sharing
to a run or world requires the destination's `publish` operation. Reads never
select or widen a write destination.

Automatic capture also checks audience flow: every current reader of the write
target must be able to read every source scope already exposed to the session.
This allows run context to flow into narrower child notes when the actual grants
permit it. Unknown or foreign-authority exposure stops capture; changing reads,
forking or compacting does not erase exposure.

See [flow checks and publication](reference/scopes.md#read-and-publish-independently)
and [user-enabled chat sharing](chat-sharing.md) for selected automatic publication.

## Named identities

Use a stable agent identity for notes associated with the same specialist across
chats. An invocation remains distinct each time. A named run can coordinate
several agents without sharing all their private memory.

```sh
mise run cli -- identities agent
mise run cli -- identity-resolve agent @research
```

Native Pi accepts `"agent":"@research"` in its configuration. `/memory agent REF`
and `/memory run REF|off` change attribution and stop automatic capture/sharing
until deliberately re-enabled. They retain the selected reads and private target.
Restricted children retain their delegated identities.

See [registration, aliases and visibility](reference/scopes.md#named-identities).

## Graphiti extraction

Graphiti processes each scope in an isolated namespace derived from authority,
world, scope and projection generation. Extraction sees previous episodes from
the same scope only. Authorized retrieval combines attributed results from
separate namespaces.

Capture queues extraction by default. SQLite local journals or PostgreSQL shared
journals retain source records, permissions and receipts even when extraction
fails. Explicit source-only operations support maintenance and recovery. See
[graph extraction details](reference/scopes.md#graphiti-extraction).

## Persistence and archives

Scopes share the service's data volume; they do not need a pod or PVC per agent.
Credential expiry stops access without deleting source. Invocation journals are
not automatically pruned.

Scope-aware `.mworld` archives retain source and audit provenance. Private
archives require explicit private restore targets; restore does not recreate
credentials or live contexts. See [scope persistence](reference/scopes.md#persistence-and-archives)
and [portable archives](portable-archives.md).

## Native Pi graph tools

The Pi extension provides explicit project/global scope bindings, scoped graph
actions and delegated subagents. See the [Pi quickstart](../packages/pi/README.md)
or [Graphiti feature guide](pi-graphiti-features.md).
