# LangGraph memory adapter and Pi tracing composition

The user requested a feature PR for the supplied adapter-first architecture.
This document makes that approved scope concrete. LangGraph owns execution
checkpoints; Memory Worlds owns authorized durable knowledge; LangSmith owns
explicit telemetry. No generic BaseStore or Memory Worlds checkpointer is added.

## Runtime and lifecycle

`MemoryWorldsAdapter(Client, journal_dir)` is an application runtime dependency.
`execution(execution_key, agent_id, read_scopes, write_scope, run_id=None)` binds
one stable workflow execution to an existing registered agent, a registered run,
and an immutable server context. The adapter creates a named run if omitted.
Selected scopes already exist and remain subject to the HTTP service's live ACLs.
Caller keys, thread IDs, UUIDs, aliases and checkpoint namespaces confer no grants.

The private journal is bound to service URL, authority and credential ID. A new
credential or authority cannot silently resume another binding. A lifecycle record
survives process restarts; every resumed operation verifies its live context.
Creating a context has no server idempotency key. A crash before saving the first
reply can leave an unused context, but no capture payload references it before
its acknowledged lifecycle record is durable.

Clients and delegated bearer credentials remain in runtime context and private
credential channels, outside graph state and checkpoint payloads. Applications
create attenuated delegated credentials using the existing client API, choose
worker-private journals and own their expiry/revocation. The adapter does not
serialize or mint hidden credentials.

## Durable writes

A bounded private journal freezes the entire original EpisodeInput or Publication
before any POST, including generated timestamp, context, metadata and stable source
ID. A separate immutable receipt record acknowledges completion. Reusing an
operation key with different caller content, timestamp, destination or metadata
fails. Retrying the original operation preserves the first generated values.
Receipts are revalidated against live authorization; a cached result grants nothing.

`capture` writes to the fixed context destination after checking current audience
containment for all recorded exposure. `publish_selected` requires the application
to assert exact user approval for the selected text and fixed destination. This is
an explicit selected declassification action, not automatic publication or an LLM
approval tool. The runnable graph uses an interrupt to obtain that approval.

## Exposure and workers

Search records its selected qualified scopes durably before any result can escape.
It validates returned authority and scope identities; malformed or foreign results
make exposure unknown. Exposure is a monotonic union per persisted context and
survives failed requests, concurrent nodes and restarts. Automatic capture rejects
unknown exposure, foreign authorities, excessive scope sets and denied live flows.

Trusted worker composition accepts a live WorkflowMemory handle, verifies the
child context's parent invocation and run, records the link, and unions its ledger
before returning selected findings. Worker private destinations are conservatively
included. Serialized model output is never accepted as proof of exposure. Failed
or unverified handoffs set a sticky unknown marker. A trusted Pi controller accepts
an explicit harness envelope and durably updates Pi's existing exposure ledger
before the harness returns workflow content. It does not parse arbitrary tool
results or grant access from scope IDs.

## Tracing

The Python adapter emits only selected correlation fields to a TraceSink. The
LangSmith sink defaults off; explicitly enabled IDs-only mode excludes source text,
queries, credentials, arbitrary metadata, exception bodies and callback state.
Selected-content mode exports only separately supplied selected excerpts. Entire
LangGraph invocation helpers suppress ambient LangSmith tracing so graph state is
not independently uploaded behind that policy. Other caller callbacks and graph
checkpointers remain caller-controlled copies and must have appropriate access.

Pi's official LangSmith extension is explicitly composed with a sanitized client
and the Memory Worlds extension. Isolated Pi sessions accept only an explicitly
constructed tracing factory. Parent propagation accepts a bounded validated
langsmith-trace header with the owned client; baggage and credential headers do not
propagate. Tracing failure cannot change memory delivery or journal semantics.

Current tested pins: LangGraph1.2.16, SQLite checkpointer3.1.2, Python LangSmith0.14.12,
official Pi tracing extension0.2.1 and its compatible LangSmith JS0.10.13.

## Validation and deliverables

Use actual LangGraph Runtime context and SQLite interrupt/resume/reopen tests
against an isolated HTTP memory authority. Cover lost capture acknowledgments,
changed retries, live revocation, concurrent exposure, parent/sibling isolation,
unknown provenance and exact explicit publication approval. Inspect saved graph
state for credentials. Run both Pi extensions in the pinned real Pi runtime with
synthetic model responses and a local trace collector; verify IDs-only payloads
and explicit selected-content behavior, including cross-process parent headers.

Ship the optional Python extra, reusable adapter, tested tracing composition,
runnable checkpointed example, mise target and concise guide. Test the current
contracts, audit public source, commit to the isolated feature branch and open a
reviewable PR. Keep the local installed daemon and Pi package unchanged.

Primary references: [runtime context](https://docs.langchain.com/oss/python/concepts/context),
[persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
[official Pi extension](https://github.com/langchain-ai/langsmith-pi-extension).
