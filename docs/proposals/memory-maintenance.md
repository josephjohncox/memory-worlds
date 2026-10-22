# Proposal: memory maintenance and memory-backed context

**Status: the first delivery is implemented on this PR branch; later stages remain proposed.**

The [maintenance guide](../maintenance.md) documents the working thread-private
scopes, typed compressed archives, selectable summary/enrichment-assessment
workers, durable jobs, dependency-aware erasure, and Python/CLI/MCP interfaces.
The sections below describe the complete direction, including work that follows
this first delivery.

Memory Worlds should preserve useful working material outside an agent's active
token window, recover detail on demand, and maintain the resulting knowledge with
explicit workers. A selectable lightweight model profile should default to a
deployment's Luna route. Foreground agents, maintenance workers and deterministic
lifecycle processes have different responsibilities.

The governing rule is simple: **save recoverable material before evicting it from
context; preserve uncertainty when deriving knowledge; authorize deletion separately.**

## What this adds

| Capability | Proposed behavior |
| --- | --- |
| Model selection | Named worker profiles, lightweight/Luna by default, with per-job overrides and explicit fallback rules |
| Summarization | Immutable summaries over exact source revisions, with coverage, omissions, unresolved questions and provenance |
| Enrichment | Workers and users flag threads, claims and ideas for missing evidence, ambiguity, disagreement or freshness review |
| Tentative knowledge | Durable claims whose epistemic status is independent of model confidence and enrichment-job status |
| Context compaction | Keep a bounded working set plus source handles; archive and acknowledge covered material before replacement |
| Expiration | Stop automatically selecting expired threads or context artifacts; use separate retention policy for erasure |
| Pruning | Produce bounded, inspectable plans to retire redundant derived views or remove policy-eligible sources |
| Purge workers | Execute authorized lifecycle intents and report source/graph cleanup separately; preserve the existing graph-maintenance barrier |
| Sync and async work | One durable job and receipt, whether the caller waits or returns immediately |
| Compression | Reversible storage compression and explicitly lossy semantic summaries, measured separately |
| RLM | A later bounded recursive execution mode over authorized external context; ordinary summaries are not presented as RLM |

## Memory and context

Use three durable layers beneath the active working set:

1. A private thread archive contains the captured, typed conversation and tool
   events that its policy permits, with explicit omissions and attachment handling.
2. Context checkpoints and summaries provide a smaller working representation
   with resolvable references into that archive.
3. Selected claims, decisions, procedures and preferences become reusable knowledge
   in the appropriate agent, thread, run or shared scope.

Local versus corporate is a placement choice. Agent, thread, run and shared scopes
determine audience and lifetime. Saving context locally does not authorize moving
private invocation data into a broader agent or company scope.

The implemented default is a real **thread-private scope** for resumable private
thread context. This adds an authorization boundary to the existing four scope
kinds. A thread UUID or a metadata filter alone must not claim that isolation.
An alternative is a thread collection within agent-persistent memory, with shared
agent-level access; the detailed design compares these choices.

## Workers and explicit processes

Summarization, enrichment assessment and evidence gathering use configurable model
profiles. Scheduling, expiration checks, lease ownership, budget admission and
authorized erasure use deterministic processes. A model may recommend an action;
its output does not create grants, establish truth or authorize a purge.

Async is the normal mode for maintenance. Sync submits the same durable job and
waits within a caller deadline. Waiting less time does not create a different job,
cancel it implicitly, or regenerate an already accepted result.

No maintenance model runs on every Pi startup. Context preservation can require
synchronous acknowledgement before compaction; summary work can be prepared
asynchronously before the context window is full.

## Delivery sequence

1. Thread/privacy contracts, exact context archives, model profiles, durable jobs,
   and summary artifacts with dependency read fences and lifecycle cleanup.
2. Stable claims, enrichment flags and grounded enrichment results.
3. Pi context checkpoints and safe compaction, followed by other harness adapters.
4. Scheduled expiration, pruning and automated lifecycle execution.
5. Bounded RLM execution with an isolated environment and shared descendant budgets.

Stage one has current-contract implementation and qualification. Claim revisions,
native Pi compaction, scheduling and RLM require following deliveries. This PR
does not change the running installation or enable autonomous erasure. Archive
receipts report actual stored/uncompressed bytes; no model-summary quality
improvement has been measured.

The [detailed design](../superpowers/specs/2026-10-21-memory-maintenance-context-design.md)
defines the records, job and compaction protocols, retention behavior, deployment
model, alternatives and acceptance tests.

## Review decisions

- Use thread-private access across invocations, separate from agent-persistent
  memory. Continuation approved implementing this recommended boundary.
- Treat context archiving separately from reusable-knowledge capture, with typed
  events and explicit omissions. The caller selects the declared captured input.
- Require an authorized lifecycle action or retention policy for source erasure.
  Lightweight-model judgments can propose pruning but cannot authorize erasure.

RLM terminology follows the [original paper](https://arxiv.org/abs/2512.24601)
and [official implementation](https://github.com/alexzhang13/rlm).
