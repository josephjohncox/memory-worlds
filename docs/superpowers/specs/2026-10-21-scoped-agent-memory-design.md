# Scoped agent memory

Status: approved for implementation by the owner on 2026-10-20 (local time).

The owner approved the four audiences and the delegation, publication, extraction,
and context rules described in the preceding architectural review. This document
records that approved design and its implementation constraints.

## Boundaries and identities

A world remains an ownership and organizational authorization boundary. Within
each world, a memory scope has one immutable UUID and one of four kinds:

| Kind | Audience and purpose |
| --- | --- |
| world-shared | Published memory available to authorized world participants |
| run-shared | An explicitly assigned task's participants |
| agent-persistent | A stable agent identity and explicitly granted readers |
| invocation-private | A single invocation and explicitly granted readers |

Runs and agent invocations use server-issued UUIDs. An invocation can reference
its parent invocation. A scope can reference its parent scope. These relationships
describe orchestration; they do not grant read access. Role names and display
labels are not identities. Persistent agent memory and per-invocation scratch are
separate objects.

An agent context selects readable scopes and exactly one default write scope.
Read selections never infer write destinations. World-shared means global to
that world's authorized participants, not global to every world or authority.

## Authorization and delegation

Every access requires world authorization and an explicit applicable scope grant.
World-level API calls select world-shared memory exclusively. Existing
world grants do not reveal newly created private scopes.

Child credentials are attenuated capabilities: their worlds, readable scopes,
operations, lifetime, and delegation authority cannot exceed the delegator's
authority. The service checks the live parent chain, expiry, identity state, and
revocation on every operation. Child credentials never become administrative
credentials or broad OIDC membership sessions. Router access cannot recover
broader permissions through the originating user's grants.

Scope operations distinguish read, write, publish, delegate, and manage where
applicable. Creating a private child scope gives its invocation access to that
new scope; it does not grant access to an existing sibling scope. A parent may
receive a separately granted coordinator inspection capability. The default
return contract discloses a selected result, not the child's complete scratch.

Unauthorized explicit selections fail before querying storage. Direct lookup,
list, status, search, export, restore, graph processing, and sync obey the same
boundary. Expiring a credential does not itself delete durable source evidence.

## Source storage and graph extraction

Every episode belongs to one scope. Scope changes are not in-place mutations.
Publishing selected text creates a separate destination episode with source
linkage, attribution, stable retry identity, and a receipt. Automatic capture
cannot promote a private transcript into shared memory.

The world-level journal contains world-shared memory. Every episode carries its
canonical scope UUID, including records selected through world-level routes.
Additional scope journals are isolated under the same world's directory, without
requiring a pod, process, or PVC per agent. Scope identifiers are validated before
filesystem access. Source archives preserve audience boundaries and do not
restore credentials or broaden private scopes.

Graphiti remains the sole graph extraction engine for both local and shared
deployments. SQLite remains authoritative for source evidence, authorization,
receipts, and orchestration. Graph projection is opt-in; source capture and search
work while the graph is disabled or unavailable.

Graph namespaces include authority, world, scope, and generation. Extraction's
previous-episode context stays in the same scope. Facts and entity summaries
cannot combine evidence from different audiences during ingestion. Queries fan
out only to authorized scope projections, preserving provenance. Publications
are projected independently at their destination. Cross-scope synthesis is not
automatically persisted into a broader audience. One current namespace formula applies to all scope kinds.

## Native Pi graph features

The native client exposes typed scope/context/delegation APIs for orchestrators.
An orchestrator constructs child prompts from the task brief and explicitly
delegated context, binds child tools to a distinct restricted credential, and
does not copy the parent's bearer credential or entire transcript. Retrieved
scope exposure follows forks, compaction, and summaries; automatic capture stops
when its destination audience does not cover the observed private context.

Feature coverage is evaluated against the released `pi-graphiti` package and pinned
upstream evidence. Its `graph` tool and project/global concepts map to explicit
authorized scopes. Native graph tools never accept arbitrary backend group IDs,
unbounded graph deletion, private evidence disclosure, or publication that
bypasses user-selected destinations. A checked feature matrix distinguishes
equivalent, extended, restricted, and missing behavior; no blanket superset claim
is made without executable coverage. Additional curation and lifecycle features
must retain durable receipts, opt-in capture, and scope restrictions.

## Validation

Required evidence includes sibling isolation, explicit coordinator access,
restricted recursive delegation, scope and operation escalation rejection,
expiry and ancestor revocation, UUID collision/idempotency behavior, current-contract
source replay, scoped direct lookups and exports, isolated graph
extraction, and explicit publication provenance. Native Pi tests cover separate
contexts, safe prompt handoff, exposure through branches, and the supported
native graph tool surface. Existing Python, native Pi, packaging, and
Kubernetes persistence checks remain release gates.
