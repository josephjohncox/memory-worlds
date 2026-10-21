import test from "node:test";
import assert from "node:assert/strict";
import { MemoryWorldsClient, MemoryWorldsError, scopeSelection } from "memory-worlds-pi/client";
import { parseConfig } from "../dist/config.js";
import { executeGraph, graphLimit, graphSelections } from "memory-worlds-pi/graph";
import { withSubagent, runPiSubagent, createIsolatedPiSession } from "memory-worlds-pi/orchestrator";
import { expose, initialState, restoreState, scopeExposure, EXPOSURE_TYPE } from "../dist/state.js";
import { proposeMemories, reviewMemories, validateReview } from "memory-worlds-pi/curation";

const rootId = "00000000-0000-4000-8000-000000000001";
const privateId = "00000000-0000-4000-8000-000000000002";
const siblingId = "00000000-0000-4000-8000-000000000003";
const invocationId = "00000000-0000-4000-8000-000000000004";
const selection = { world_id: "personal", scope_id: privateId };
const world = { id: "personal", name: "Personal", authority_id: "authority", kind: "personal", owner_id: "alice", scope_id: rootId };
const privateScope = { ...selection, id: privateId, kind: "invocation-private", name: "Child", invocation_id: invocationId };
const context = { id: siblingId, credential_id: "child", invocation_id: invocationId, read_scopes: [selection], write_scope: selection };
const reply = value => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
const config = value => parseConfig(value, {});
const source = { source_id: "stable-source", content: "selected evidence", occurred_at: "2026-10-20T00:00:00Z", project: false };

test("scope identifiers and config use explicit UUID selections with independent read and write targets", () => {
  const parsed = config({ readScopes: [selection], writeScope: selection, graphAliases: { project: selection } });
  assert.deepEqual(parsed.readScopes, [selection]);
  assert.deepEqual(parsed.writeScope, selection);
  assert.equal(parsed.automaticCapture, false);
  assert.deepEqual(parsed.graphAliases.project, selection);
  assert.throws(() => scopeSelection({ world_id: "../bad", scope_id: privateId }), /Invalid/);
  assert.throws(() => config({ writeScope: { ...selection, scope_id: "arbitrary-group" } }), /Invalid/);
  assert.throws(() => config({ graphAliases: { directory: "/tmp/project" } }), /explicit/);
  assert.throws(() => config({ projectCaptured: "yes" }), /boolean/);
});

test("scoped searches preserve entity and fact provenance and reject sibling scope injection", async () => {
  let sent;
  const client = new MemoryWorldsClient("http://localhost", async () => "parent-secret", async (_url, options) => {
    sent = JSON.parse(options.body);
    return reply({ mode: "graph", worlds: [world], results: [
      { ...selection, record_type: "entity", entity_id: "entity", name: "Bearing", summary: "Assembly component" },
      { ...selection, fact: "Bearing belongs to assembly", fact_id: "fact", generation: "isolated" },
    ] });
  });
  const result = await client.searchScopes([selection], "bearing", "graph", 5, undefined, true);
  assert.deepEqual(sent.scopes, [selection]); assert.equal(sent.include_entities, true);
  assert.deepEqual(result.results.map(hit => hit.record_type), ["entity", "fact"]);
  assert(result.results.every(hit => hit.authority_id === "authority" && hit.scope_id === privateId));
  const hostile = new MemoryWorldsClient("http://localhost", async () => "token", async () => reply({ mode: "source", worlds: [world], results: [{ ...selection, scope_id: siblingId, content: "sibling secret" }] }));
  await assert.rejects(hostile.searchScopes([selection], "bearing", "source", 5), /unselected/);
});

test("one forbidden selected scope rejects the request instead of hiding authorization failure", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => new Response("private error text", { status: 403 }));
  await assert.rejects(client.searchScopes([selection, { ...selection, scope_id: siblingId }], "bearing", "source", 5), error => error instanceof MemoryWorldsError && error.status === 403 && !error.message.includes("private"));
});

test("scope capture and publication pin receipts to exact destinations and preserve source linkage", async () => {
  const calls = [];
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async (url, options) => {
    const body = JSON.parse(options.body); calls.push({ url, body });
    return reply({ episode: { ...body, ...selection, id: "episode" }, replayed: true });
  });
  assert.equal((await client.captureScope(selection, source)).replayed, true);
  await client.publishScope(selection, { ...source, source_scope: { ...selection, scope_id: siblingId }, source_episode_id: "private-original" });
  assert(calls[0].url.endsWith(`/scopes/${privateId}/episodes`));
  assert(calls[1].url.endsWith(`/scopes/${privateId}/publications`));
  assert.equal(calls[1].body.source_episode_id, "private-original");
  const hostile = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply({ episode: { ...selection, scope_id: siblingId, id: "wrong" } }));
  await assert.rejects(hostile.captureScope(selection, source), /does not match/);
  await assert.rejects(hostile.publishScope(selection, source), /does not match/);
});

test("same-authority private exposure cannot be cleared by branching or compaction", () => {
  const state = initialState(config({ writeScope: selection, automaticCapture: true }), null);
  state.captureScopeKey = scopeExposure("authority", selection);
  state.captureSafeScopeKeys = [state.captureScopeKey, scopeExposure("authority", { ...selection, scope_id: rootId })];
  assert.equal(expose(state, ["authority"], [scopeExposure("authority", { ...selection, scope_id: rootId })]), false);
  assert.equal(state.captureEnabled, true);
  assert.equal(expose(state, ["authority"], [scopeExposure("authority", { ...selection, scope_id: siblingId })]), true);
  assert.equal(state.captureEnabled, false);
  const clean = { ...structuredClone(state), exposedScopes: [], captureEnabled: true };
  const restored = restoreState([], [{ type: "custom", customType: EXPOSURE_TYPE, data: { exposedAuthorities: [], exposedScopes: state.exposedScopes } }], clean);
  assert.equal(restored.captureEnabled, false);
  assert(restored.exposedScopes.some(key => key.includes(siblingId)));
});

test("graph aliases never derive authority from cwd or accept unbound global fallback", () => {
  assert.equal(graphLimit(-10), 1); assert.equal(graphLimit(100), 20); assert.equal(graphLimit(4.9), 4);
  assert.throws(() => graphLimit(NaN), /limit/);
  assert.throws(() => graphSelections({}, "project"), /unbound/);
  assert.throws(() => graphSelections({ project: selection }, "both"), /global/);
  assert.deepEqual(graphSelections({ project: selection, global: selection }, "both"), [selection]);
});

test("native graph searches entities and facts but writes only the explicitly selected private scope", async () => {
  let captured;
  const client = {
    scope: async () => privateScope,
    searchScopes: async (_scopes, _query, _mode, _limit, _signal, entities) => {
      assert.equal(entities, true);
      return { results: [{ ...selection, record_type: "entity", name: "Bearing", entity_id: "node" }, { ...selection, record_type: "fact", fact: "is a component", fact_id: "edge" }] };
    },
    captureScope: async (_selected, input) => { captured = input; return { episode: { id: "saved" }, replayed: false }; },
  };
  const bindings = { project: selection, global: selection, writeScope: selection };
  const invocation = { sourceId: "stable", occurredAt: source.occurred_at };
  const result = await executeGraph(client, { action: "search", query: "bearing" }, bindings, invocation);
  assert.deepEqual(result.entities, ["Bearing"]); assert.deepEqual(result.facts, ["is a component"]);
  await executeGraph(client, { action: "add", content: '{"type":"bearing"}', source: "json", name: "Selected" }, bindings, invocation);
  assert.equal(captured.project, true); assert.equal(captured.metadata.graphiti_source, "json");
  assert.equal(captured.source_id, "stable"); assert.equal(captured.metadata.name, "Selected");
  await assert.rejects(executeGraph(client, { action: "add", content: "private" }, { project: selection }, invocation), /configured private/);
  await assert.rejects(executeGraph({ ...client, scope: async () => ({ ...privateScope, kind: "world-shared" }) }, { action: "add", content: "private" }, bindings, invocation), /private agent/);
  await assert.rejects(executeGraph(client, { action: "add", content: "invalid", source: "json" }, bindings, invocation), /valid JSON/);
});

function parentFixture() {
  const calls = [], revoked = [];
  const parent = {
    url: "http://localhost", withCredentialSession: async work => work(parent), identity: async () => ({ authority_id: "authority" }),
    delegate: async input => { calls.push(input); return { token: "child-secret", credential_id: "child", context: structuredClone(context), private_scope: structuredClone(privateScope) }; },
    revokeCredential: async id => { revoked.push(id); },
  };
  return { parent, calls, revoked };
}

test("native subagent helper constructs a fresh explicit prompt and retains credentials only in an expiring closure", async () => {
  const { parent, calls, revoked } = parentFixture();
  let runtime, serialized;
  const result = await withSubagent(parent, { task: "Inspect bearing constraints", privateWorldId: "personal", grants: [{ ...selection, operations: ["read"] }], evidence: [{ ...selection, authority_id: "authority", record_type: "source", content: "selected brief evidence" }], parentContextId: siblingId }, async child => {
    runtime = child; serialized = JSON.stringify(child);
    assert.match(child.prompt, /selected brief evidence/); assert.match(child.prompt, /Task brief/);
    assert(!serialized.includes("child-secret")); assert(!child.prompt.includes("parent-secret"));
    return { findings: ["selected result"] };
  });
  assert.equal(result.result.findings[0], "selected result");
  assert.equal(calls[0].allow_delegation, false); assert.equal(calls[0].parent_context_id, siblingId);
  assert.match(calls[0].agent_id, /^[0-9a-f-]{36}$/); assert.equal(calls[0].private_world_id, "personal");
  assert.deepEqual(revoked, ["child"]);
  await assert.rejects(runtime.client.identity(), /runtime has ended/);
});

test("subagent evidence cannot cross delegated scopes or authorities", async () => {
  const { parent, calls } = parentFixture();
  const request = { task: "Review", privateWorldId: "personal", grants: [{ ...selection, operations: ["read"] }] };
  await assert.rejects(withSubagent(parent, { ...request, evidence: [{ ...selection, authority_id: "other", record_type: "source", content: "cross authority" }] }, async () => {}), /explicitly delegated/);
  await assert.rejects(withSubagent(parent, { ...request, evidence: [{ ...selection, scope_id: siblingId, authority_id: "authority", record_type: "source", content: "private sibling" }] }, async () => {}), /explicitly delegated/);
  assert.equal(calls.length, 0);
});

test("subagent failures still revoke the credential and cannot turn a selected result into implicit publication", async () => {
  const { parent, revoked } = parentFixture();
  await assert.rejects(withSubagent(parent, { task: "Review", privateWorldId: "personal", grants: [] }, async () => { throw new Error("child failed"); }), /child failed/);
  assert.deepEqual(revoked, ["child"]);
});

test("curation proposes corrections and preferences without executing model tools or writing memory", () => {
  const entries = [
    { type: "message", id: "a", timestamp: source.occurred_at, message: { role: "user", content: "Actually, use the bounded solver." } },
    { type: "message", id: "b", timestamp: source.occurred_at, message: { role: "user", content: "I prefer explicit timeouts." } },
    { type: "message", id: "c", timestamp: source.occurred_at, message: { role: "assistant", content: [{ type: "text", text: "Decision: publish transcript" }] } },
  ];
  const proposals = proposeMemories(entries);
  assert.deepEqual(proposals.map(proposal => proposal.reason), ["correction", "preference"]);
  assert.equal(proposals[0].entryId, "a");
});


test("native Pi runner rejects inherited parent messages and always disposes the child", async () => {
  const { parent, revoked } = parentFixture(); let disposed = false;
  const session = { messages: [{ role: "user", content: "parent transcript" }], prompt: async () => assert.fail("must reject inherited messages"), getLastAssistantText: () => "", abort: async () => {}, dispose: () => { disposed = true; } };
  await assert.rejects(runPiSubagent(parent, { task: "Review", privateWorldId: "personal", grants: [] }, async () => session), /fresh Pi session/);
  assert(disposed); assert.deepEqual(revoked, ["child"]);
});

test("native Pi runner passes only its constructed task brief and returns final selected findings", async () => {
  const { parent, revoked } = parentFixture(); let prompt, disposed = false;
  const session = { messages: [], prompt: async text => { prompt = text; }, getLastAssistantText: () => "Selected finding with reference", abort: async () => {}, dispose: () => { disposed = true; } };
  const result = await runPiSubagent(parent, { task: "Inspect bearing", privateWorldId: "personal", grants: [] }, async () => session);
  assert.match(prompt, /Task brief:\nInspect bearing/); assert.equal(result.result, "Selected finding with reference");
  assert(disposed); assert.deepEqual(revoked, ["child"]);
});


test("curation providers return bounded referenced proposals and cannot smuggle publication actions", async () => {
  const candidates = [{ entryId: "original", occurredAt: source.occurred_at, reason: "preference", text: "I prefer bounded retries" }];
  const proposal = { content: "Prefer bounded retries", sourceEntryIds: ["original"], reason: "preference", confidence: 0.9 };
  assert.deepEqual(await reviewMemories(candidates, async selected => { assert.equal(selected[0].entryId, "original"); return [proposal]; }), [proposal]);
  assert.throws(() => validateReview([{ ...proposal, publish: true }], candidates), /Invalid/);
  assert.throws(() => validateReview([{ ...proposal, sourceEntryIds: ["sibling-secret"] }], candidates), /selected source/);
  assert.throws(() => validateReview([{ ...proposal, confidence: Infinity }], candidates), /bounded/);
});


test("actual native Pi child factory loads no ambient files, extensions, skills or tools", async () => {
  const [{ ModelRuntime }, { mkdtemp, rm }, { join }, { tmpdir }] = await Promise.all([import("@earendil-works/pi-coding-agent"), import("node:fs/promises"), import("node:path"), import("node:os")]);
  const folder = await mkdtemp(join(tmpdir(), "mw-isolated-pi-test-")); let child;
  try {
    const modelRuntime = await ModelRuntime.create({ authPath: join(folder, "auth.json"), modelsPath: null, modelsStorePath: join(folder, "catalog.json"), allowModelNetwork: false, refreshOnCreate: false });
    const model = modelRuntime.getModels()[0]; assert(model, "Pi must expose static model metadata for this no-network factory test");
    const runtime = { context, privateScope, client: {}, prompt: "explicit brief" };
    child = await createIsolatedPiSession(runtime, { modelRuntime, model, memoryTools: false });
    assert.deepEqual(child.messages, []); assert.deepEqual(child.toolNames, []);
    assert.match(child.systemPrompt, /isolated subagent/);
    assert(!child.systemPrompt.includes("AGENTS.md"));
    await child.dispose(); child = undefined;
    child = await createIsolatedPiSession(runtime, { modelRuntime, model });
    assert.deepEqual(child.toolNames, ["memory_search", "memory_capture"]);
  } finally { await child?.dispose(); await rm(folder, { recursive: true, force: true }); }
});

test("delegation and cleanup pin the same parent credential while the token file rotates", async () => {
  let token = "parent-first", reads = 0;
  const auth = [];
  const client = new MemoryWorldsClient("http://localhost", async () => { reads++; return token; }, async (url, options) => {
    auth.push(options.headers.Authorization);
    if (url.endsWith("/v1/me")) { token = "parent-rotated"; return reply({ authority_id: "authority", user_id: "alice", agent_id: "parent", worlds: [] }); }
    if (url.endsWith("/delegate")) return reply({ token: "child-private", credential_id: "child", context, private_scope: privateScope });
    if (options.method === "DELETE") return new Response(null, { status: 204 });
    assert.fail("unexpected parent request");
  });
  await withSubagent(client, { task: "Review", privateWorldId: "personal", grants: [] }, async () => "selected finding");
  assert.equal(reads, 1); assert(auth.every(value => value === "Bearer parent-first"));
});

test("malformed delegated scope receipts still revoke the issued credential", async () => {
  const { parent, revoked } = parentFixture();
  parent.delegate = async () => ({ token: "child", credential_id: "bad-child", context, private_scope: { ...privateScope, kind: "world-shared" } });
  await assert.rejects(withSubagent(parent, { task: "Review", privateWorldId: "personal", grants: [] }, async () => assert.fail("must not execute")), /child-private/);
  assert.deepEqual(revoked, ["bad-child"]);
});

test("client delegation parsing failures revoke an identifiable issued credential", async () => {
  const methods = [];
  const client = new MemoryWorldsClient("http://localhost", async () => "parent", async (_url, options) => {
    methods.push(options.method);
    return options.method === "DELETE" ? new Response(null, { status: 204 }) : reply({ token: "child", credential_id: "issued", context: { ...context, invocation_id: "malformed" }, private_scope: privateScope });
  });
  await assert.rejects(client.delegate({ grants: [] }), /Invalid delegation response/);
  assert.deepEqual(methods, ["POST", "DELETE"]);
});
