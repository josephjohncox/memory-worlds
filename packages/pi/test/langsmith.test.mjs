import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createLangSmithPiTracing } from "memory-worlds-pi/langsmith";
import { createIsolatedPiSession } from "memory-worlds-pi/orchestrator";
import { registerMemoryWorlds } from "memory-worlds-pi";
import { parseConfig } from "../dist/config.js";
import { EXPOSURE_TYPE } from "../dist/state.js";
import { DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager, createAgentSession } from "@earendil-works/pi-coding-agent";
import { fauxProvider, fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { RunTree } from "langsmith";

const scopeId = "00000000-0000-4000-8000-000000000001";
const contextId = "00000000-0000-4000-8000-000000000002";
const selected = { world_id: "personal", scope_id: scopeId };
const privateCanary = "synthetic-private-memory-canary-utterance";
const queryCanary = "synthetic-private-query-canary";
const promptCanary = "synthetic-private-prompt-canary";
const answerCanary = "synthetic-private-answer-canary";
const tokenCanary = "synthetic-bearer-token-canary";

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "memory-worlds-tracing-"));
  const bodies = [];
  const server = createServer(async (request, response) => {
    let raw = ""; for await (const chunk of request) raw += chunk;
    assert.equal(request.headers["x-api-key"], "synthetic-local-tracing-key");
    bodies.push({ method: request.method, path: request.url, body: raw ? JSON.parse(raw) : null });
    response.writeHead(200, { "content-type": "application/json" }); response.end("{}");
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  const modelRuntime = await ModelRuntime.create({ authPath: join(directory, "auth.json"), modelsPath: null, modelsStorePath: join(directory, "catalog.json"), allowModelNetwork: false, refreshOnCreate: false });
  const provider = fauxProvider({ provider: "memory-worlds-tracing-fixture", models: [{ id: "synthetic-model", input: ["text"] }] });
  modelRuntime.registerNativeProvider(provider.provider);
  const model = modelRuntime.getModel(provider.provider.id, "synthetic-model") ?? modelRuntime.getModel("memory-worlds-tracing-fixture", "synthetic-model");
  assert(model);
  return { directory, bodies, endpoint, modelRuntime, provider, model, close: async () => { await new Promise(resolve => server.close(resolve)); await rm(directory, { recursive: true, force: true }); } };
}

test("official LangSmith and Memory Worlds extensions execute together in actual Pi with IDs-only traces", async () => {
  const f = await fixture(); let session;
  try {
    const parent = new RunTree({ name: "Python workflow", project_name: "not-forwarded-project", inputs: {} });
    const tracing = await createLangSmithPiTracing({ endpoint: f.endpoint, apiKey: "synthetic-local-tracing-key", correlation: { authority_id: "fixture-authority", world_id: "personal", scope_id: scopeId, context_id: contextId }, parentTrace: parent.toHeaders()["langsmith-trace"] });
    const client = {
      url: "http://127.0.0.1:8787", identity: async () => ({ authority_id: "fixture-authority", credential_id: "fixture", user_id: "alice", agent_id: "pi", worlds: [] }),
      worlds: async () => [], mounts: async () => [], deliveries: async () => [],
      bindContext: async input => ({ ...input, id: contextId, credential_id: "fixture", invocation_id: contextId }),
      searchScopes: async () => ({ results: [{ ...selected, authority_id: "fixture-authority", record_type: "source", content: privateCanary }], failures: [], complete: true }),
      searchMounts: async () => ({ results: [], failures: [], complete: true }),
    };
    const settings = SettingsManager.inMemory({});
    const config = parseConfig({ tokenFile: join(f.directory, tokenCanary), readScopes: [selected], retrieval: { enabled: false } }, {});
    const loader = new DefaultResourceLoader({ cwd: f.directory, agentDir: join(f.directory, "agent"), settingsManager: settings,
      noExtensions: true, disabledBuiltinExtensions: ["mcp"], noContextFiles: true, noSkills: true, noPromptTemplates: true, noThemes: true,
      extensionFactories: [pi => { registerMemoryWorlds(pi, { config: async () => config, client: () => client }); }, tracing.extensionFactory],
    });
    await loader.reload();
    const loaded = await createAgentSession({ cwd: f.directory, agentDir: join(f.directory, "agent"), modelRuntime: f.modelRuntime, model: f.model, sessionManager: SessionManager.inMemory(f.directory), settingsManager: settings, resourceLoader: loader, tools: ["memory_search"] });
    assert.deepEqual(loaded.extensionsResult.errors, []);
    session = loaded.session; await session.bindExtensions({ mode: "print" });
    f.provider.setResponses([
      async () => {
        // Native faux providers have no wire request. Exercise Pi's real provider
        // hook with a synthetic payload as a network provider would do.
        await session.extensionRunner.emitBeforeProviderRequest({ messages: [{ role: "user", content: promptCanary }] });
        return fauxAssistantMessage(fauxToolCall("memory_search", { query: queryCanary, mode: "source" }), { stopReason: "toolUse" });
      },
      async context => {
        const result = context.messages.find(message => message.role === "toolResult");
        assert(JSON.stringify(result).includes(privateCanary), "The actual memory tool must deliver evidence to the model");
        assert(session.sessionManager.getEntries().some(entry => entry.type === "custom" && entry.customType === EXPOSURE_TYPE), "Exposure must be durable before evidence returns");
        await session.extensionRunner.emitBeforeProviderRequest({ messages: context.messages });
        return fauxAssistantMessage(answerCanary);
      },
    ]);
    await session.prompt(promptCanary); await tracing.flush();
    assert.equal(session.getLastAssistantText(), answerCanary, JSON.stringify(session.messages));
    const serialized = JSON.stringify(f.bodies);
    for (const secret of [privateCanary, queryCanary, promptCanary, answerCanary, tokenCanary, "not-forwarded-project"]) assert(!serialized.includes(secret), `Trace leaked ${secret}`);
    const runs = f.bodies.filter(item => item.method === "POST");
    assert(runs.length >= 5, "Collector must receive chain/model/tool spans from the official extension");
    assert(runs.some(item => item.body.run_type === "tool"));
    assert(runs.some(item => item.body.run_type === "llm"));
    assert(runs.some(item => item.body.parent_run_id === parent.id));
    assert(runs.every(item => item.body.trace_id === parent.trace_id));
    assert(runs.every(item => item.body.extra.metadata.context_id === contextId));
    assert(runs.every(item => item.body.extra.metadata.memory_worlds_trace_policy === "ids-only"));
    assert(runs.every(item => JSON.stringify(item.body.inputs) === "{}"));
  } finally { session?.dispose(); await f.close(); }
});

test("isolated Pi loads tracing only when explicitly supplied and exports only selected excerpts", async () => {
  const f = await fixture(); let child;
  try {
    const context = { id: contextId, invocation_id: contextId, created_at: "2026-10-21T00:00:00Z", read_scopes: [] };
    const runtime = { context, privateScope: { ...selected, id: scopeId, kind: "invocation-private" }, client: {}, prompt: promptCanary };
    f.provider.setResponses([() => fauxAssistantMessage(answerCanary), () => fauxAssistantMessage(answerCanary)]);
    child = await createIsolatedPiSession(runtime, { modelRuntime: f.modelRuntime, model: f.model, memoryTools: false });
    await child.prompt(promptCanary); await child.dispose(); child = undefined;
    assert.equal(f.bodies.length, 0, "Unconfigured isolated workers must emit no traces");
    const selectedExcerpt = "User-approved synthetic durable finding";
    const tracing = await createLangSmithPiTracing({ endpoint: f.endpoint, apiKey: "synthetic-local-tracing-key", mode: "selected-content", selectedContent: { finding: selectedExcerpt }, correlation: { invocation_id: contextId } });
    child = await createIsolatedPiSession(runtime, { modelRuntime: f.modelRuntime, model: f.model, memoryTools: false, tracing });
    await child.prompt(promptCanary); await child.dispose(); child = undefined;
    assert(f.bodies.length > 0);
    const serialized = JSON.stringify(f.bodies);
    assert(serialized.includes(selectedExcerpt));
    for (const secret of [promptCanary, answerCanary]) assert(!serialized.includes(secret));
    const stored = new Map();
    for (const request of f.bodies) {
      const id = request.method === "POST" ? request.body.id : request.path.split("/").at(-1);
      stored.set(id, { ...stored.get(id), ...request.body });
    }
    const roots = [...stored.values()].filter(run => run.inputs?.selected);
    assert.equal(roots.length, 1, "Final stored root must retain the approved excerpt after PATCH");
    assert.deepEqual(roots[0].inputs, { selected: { finding: selectedExcerpt } });
    assert(roots[0].end_time, "Check the final acknowledged root, not only its initial POST");
  } finally { await child?.dispose(); await f.close(); }
});

test("tracing validates bounded parent identity and rejects implicit content and unsafe endpoints", async () => {
  const valid = { endpoint: "http://127.0.0.1:8787", apiKey: "synthetic-key" };
  for (const options of [
    { ...valid, endpoint: "http://remote.example" }, { ...valid, apiKey: "" },
    { ...valid, parentTrace: "not-a-trace" },
    { ...valid, correlation: { context_id: "a private thought" } },
    { ...valid, correlation: { context_id: "synthetic-private-thought" } },
    { ...valid, correlation: { execution_id: "synthetic-private-thought" } },
    { ...valid, correlation: { world_id: "x".repeat(101) } },
    { ...valid, correlation: { transcript: privateCanary } },
    { ...valid, selectedContent: { finding: privateCanary } },
    { ...valid, mode: "selected-content" },
    { ...valid, mode: "selected-content", selectedContent: { finding: "x".repeat(8001) } },
  ]) await assert.rejects(createLangSmithPiTracing(options));
});

test("upstream error text, partial tool events and ambient replica settings never enter the trace transport", async () => {
  const f = await fixture();
  const previous = process.env.LANGSMITH_RUNS_ENDPOINTS;
  process.env.LANGSMITH_RUNS_ENDPOINTS = JSON.stringify([{ api_url: `${f.endpoint}/unexpected-replica`, api_key: "synthetic-local-tracing-key", project: privateCanary }]);
  try {
    const tracing = await createLangSmithPiTracing({ endpoint: f.endpoint, apiKey: "synthetic-local-tracing-key", correlation: { invocation_id: contextId } });
    const hooks = new Map();
    await tracing.extensionFactory({ on: (name, handler) => hooks.set(name, handler), registerCommand: () => {} });
    const ctx = { cwd: privateCanary, sessionManager: { getSessionId: () => privateCanary }, ui: { setStatus: () => {} }, getContextUsage: () => ({ private: privateCanary }), model: { provider: privateCanary, name: privateCanary } };
    const emit = (name, input) => hooks.get(name)?.(input, ctx);
    await emit("before_agent_start", { prompt: promptCanary });
    await emit("turn_start", { turnIndex: 0, timestamp: Date.now() });
    await emit("before_provider_request", { payload: { messages: [{ role: "user", content: promptCanary }] } });
    await emit("tool_execution_start", { toolName: privateCanary, toolCallId: privateCanary, args: { private: privateCanary } });
    await emit("tool_execution_update", { toolCallId: privateCanary, partialResult: { private: privateCanary } });
    await emit("tool_execution_end", { toolCallId: privateCanary, result: { content: [{ type: "text", text: privateCanary }] }, isError: true });
    const message = fauxAssistantMessage(privateCanary, { stopReason: "error" }); message.errorMessage = privateCanary;
    await emit("message_end", { message });
    await emit("turn_end", { turnIndex: 0, message, toolResults: [{ private: privateCanary }] });
    await emit("agent_end", { messages: [message] });
    await emit("agent_settled", {});
    await emit("session_shutdown", {}); await tracing.flush();
    assert(f.bodies.some(item => item.body.error === "Pi execution failed"));
    assert(!JSON.stringify(f.bodies).includes(privateCanary));
    assert(!JSON.stringify(f.bodies).includes(promptCanary));
    assert(f.bodies.every(item => item.path === "/runs" || item.path.startsWith("/runs/")));
    assert(f.bodies.every(item => !("events" in item.body)));
  } finally {
    if (previous === undefined) delete process.env.LANGSMITH_RUNS_ENDPOINTS;
    else process.env.LANGSMITH_RUNS_ENDPOINTS = previous;
    await f.close();
  }
});
