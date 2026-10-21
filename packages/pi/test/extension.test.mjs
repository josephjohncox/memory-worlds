import test from "node:test";
import { randomUUID } from "node:crypto";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registerMemoryWorlds } from "memory-worlds-pi";
import { MemoryWorldsClient } from "memory-worlds-pi/client";
import { parseConfig } from "../dist/config.js";
import { CONTEXT_TYPE, entryEpisode, expose, initialState, EXPOSURE_TYPE, memoryContext, RECEIPT_TYPE, restoreState, STATE_TYPE } from "../dist/state.js";

const rootScope = "00000000-0000-4000-8000-000000000001", privateScope = "00000000-0000-4000-8000-000000000002", siblingScope = "00000000-0000-4000-8000-000000000003";
const world = { scope_id: rootScope, id: "private", name: "Private", authority_id: "a", kind: "personal", owner_id: "alice" };
const timestamp = "2026-10-20T12:00:00.000Z";
function message(id, parentId, text, role = "user") {
  return { type: "message", id, parentId, timestamp, message: { role, content: role === "user" ? text : [{ type: "text", text }] } };
}
function fixture(configOverrides = {}, dependencies = {}) {
  const hooks = new Map(), tools = new Map(), commands = new Map(), renderers = new Map();
  const entries = [], notices = [], captures = [], publications = [], widgets = new Map();
  let branch = entries;
  let generated = 0;
  const pi = {
    on: (name, fn) => hooks.set(name, fn), registerTool: tool => tools.set(tool.name, tool),
    registerCommand: (name, command) => commands.set(name, command), registerMessageRenderer: (name, renderer) => renderers.set(name, renderer),
    appendEntry: (customType, data) => entries.push({ type: "custom", id: `s${++generated}-${randomUUID()}`, parentId: entries.at(-1)?.id ?? null, timestamp, customType, data: structuredClone(data) }),
  };
  const ctx = {
    signal: undefined, hasUI: false, mode: "print", cwd: "/tmp",
    ui: { notify: (text, type) => notices.push({ text, type }), setStatus: (key, value) => widgets.set(key, value), setWidget: (key, value) => widgets.set(key + ":widget", value) },
    sessionManager: { getSessionId: () => "session", getLeafId: () => branch.at(-1)?.id ?? null, getBranch: () => [...branch], getEntries: () => [...entries], getHeader: () => ({}) },
  };
  const client = {
    url: "http://127.0.0.1:8787",
    withCredentialSession: async work => work(client),
    bindContext: async input => ({ ...input, id: randomUUID(), invocation_id: randomUUID(), credential_id: "fixture" }),
    canFlow: async reads => reads.every(scope => (scope.world_id === "private" && [rootScope, privateScope].includes(scope.scope_id)) || (scope.world_id === "shared" && scope.scope_id === privateScope)),
    context: async id => ({ id, invocation_id: siblingScope, credential_id: "fixture", read_scopes: [], write_scope: { world_id: "private", scope_id: privateScope } }),
    identity: async () => ({ authority_id: "a", credential_id: "fixture" }),
    scope: async selected => ({ ...selected, id: selected.scope_id, kind: "invocation-private", name: "Child" }),
    scopes: async () => [],
    captureScope: async (selected, input) => { captures.push({ worldId: selected.world_id, scopeId: selected.scope_id, input }); return { episode: { ...selected, id: "scoped-receipt" }, replayed: false }; },
    searchScopes: async () => ({ results: [{ authority_id: "a", world_id: "private", scope_id: privateScope, record_type: "source", content: "bearing" }], failures: [], complete: true }),
    searchMounts: async () => ({ results: [], failures: [], complete: true }),
    publishScope: async (selected, input) => { publications.push({ selected, input }); return { episode: { ...input, ...selected, id: "published", source_type: "publication", metadata: {} }, replayed: false }; },
    worlds: async () => [world, { ...world, id: "shared", kind: "team" }], mounts: async () => [{ id: "team", name: "Team", world_ids: ["shared"] }], deliveries: async () => [],
    publish: async input => { publications.push(input); return { id: "delivery", state: "pending" }; },
  };
  const config = parseConfig({ writeScope: { world_id: "private", scope_id: privateScope }, readScopes: [{ world_id: "private", scope_id: privateScope }], ...configOverrides }, {});
  registerMemoryWorlds(pi, { config: async () => config, client: () => client, ...dependencies });
  return { pi, hooks, tools, commands, ctx, client, entries, captures, publications, notices, widgets, setBranch: value => { branch = value; }, run: (event, value = {}) => hooks.get(event)({ type: event, ...value }, ctx), command: args => commands.get("memory").handler(args, ctx) };
}

function usabilityClient(f) {
  f.client.worlds = async () => [{ ...world, alias: "personal" }];
  f.client.scopes = async () => [
    { id: rootScope, world_id: "private", scope_id: rootScope, alias: "published", kind: "world-shared" },
    { id: privateScope, world_id: "private", scope_id: privateScope, alias: "notes", kind: "agent-persistent" },
  ];
  f.client.identity = async () => ({ authority_id: "a", credential_id: "fixture", user_id: "alice", agent_id: "editor", worlds: [{ ...world, alias: "personal" }] });
  for (const name of ["resolveWorld", "resolveScope", "resolveSelection"]) f.client[name] = MemoryWorldsClient.prototype[name];
  f.client.identities = async kind => [{ id: rootScope, kind, name: "Named specialist", alias: "architect", owner_id: "alice", created_at: timestamp }];
  f.client.resolveIdentity = async (kind, reference) => {
    const selected = (await f.client.identities(kind)).find(item => item.id === reference || `@${item.alias}` === reference);
    if (!selected) throw new Error("Identity not visible");
    return selected;
  };
  return f;
}

test("configured and command aliases resolve live and persist only canonical scope selections", async () => {
  const f = usabilityClient(fixture({ readScopes: [{ world_id: "@personal", scope_id: "@notes" }], writeScope: { world_id: "@personal", scope_id: "@notes" } }));
  await f.run("session_start", { reason: "startup" });
  await f.command("read @personal @published");
  await f.command("target @personal @notes");
  await f.command("alias project @personal @notes");
  const saved = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  assert.deepEqual(saved.writeScope, { world_id: "private", scope_id: privateScope });
  assert(saved.readScopes.some(item => item.scope_id === rootScope));
  assert.deepEqual(saved.graphAliases.project, saved.writeScope);
  assert(!JSON.stringify(saved).includes("@personal"));
  assert(!f.notices.some(notice => notice.type === "error"));
});

test("named agent configuration remains stable across independent chats with fresh invocations", async () => {
  const first = usabilityClient(fixture({ agent: "@architect" })), second = usabilityClient(fixture({ agent: "@architect" }));
  await first.run("session_start", { reason: "startup" });
  await second.run("session_start", { reason: "startup" });
  const a = first.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  const b = second.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  assert.equal(a.agentUuid, rootScope); assert.equal(b.agentUuid, rootScope);
  assert.equal(a.boundContext.agent_id, rootScope); assert.equal(b.boundContext.agent_id, rootScope);
  assert.notEqual(a.boundContext.invocation_id, b.boundContext.invocation_id);
});

test("configured identities cannot override delegated credential agent UUIDs", async () => {
  const f = usabilityClient(fixture({ agent: "@architect", automaticCapture: true }));
  f.client.identity = async () => ({ authority_id: "a", credential_id: "fixture", user_id: "alice", agent_uuid: siblingScope, restricted: true });
  await f.run("session_start", { reason: "startup" });
  assert(f.notices.some(notice => notice.type === "error" && notice.text.includes("does not belong")));
  assert.equal(f.captures.length, 0);
  assert.equal(f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data.captureEnabled, false);
});

test("agent and run selection rebind attribution without changing reads or private destination", async () => {
  const f = usabilityClient(fixture());
  await f.run("session_start", { reason: "startup" });
  await f.command("capture on");
  await f.command("agent @architect");
  await f.command("run @architect");
  const saved = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  assert.equal(saved.agentUuid, rootScope); assert.equal(saved.runUuid, rootScope);
  assert.equal(saved.boundContext.agent_id, rootScope); assert.equal(saved.boundContext.run_id, rootScope);
  assert.equal(saved.captureEnabled, false);
  assert.deepEqual(saved.writeScope, { world_id: "private", scope_id: privateScope });
  assert.deepEqual(saved.readScopes, [{ world_id: "private", scope_id: privateScope }]);
  assert(![...f.tools.keys()].some(name => /identity|import/.test(name)));
});

test("identity metadata and server aliases change only through explicit native user commands", async () => {
  const f = usabilityClient(fixture()), changes = [];
  f.client.setWorldAlias = async (...args) => { changes.push(["world", ...args.slice(0, 2)]); return world; };
  f.client.setScopeAlias = async (...args) => { changes.push(["scope", ...args.slice(0, 2)]); return {}; };
  f.client.createIdentity = async (kind, input) => { changes.push(["identity", kind, input]); return { ...input, id: rootScope }; };
  await f.run("session_start", { reason: "startup" });
  const previous = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  await f.command("world-alias @personal renamed");
  await f.command("scope-alias @personal @notes archive");
  await f.command("identity agent create specialist Named specialist");
  assert.equal(changes.length, 3);
  assert.deepEqual(changes[1], ["scope", { world_id: "private", scope_id: privateScope }, "archive"]);
  assert.deepEqual(changes[2], ["identity", "agent", { id: undefined, name: "Named specialist", alias: "specialist" }]);
  assert.equal(f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data.agentUuid, previous.agentUuid);
});


test("graph extraction and retrieval are the normal configuration with explicit source opt-outs", () => {
  const normal = parseConfig({}, {});
  assert.equal(normal.projectCaptured, true);
  assert.equal(normal.retrieval.mode, "graph");
  assert.equal(normal.automaticCapture, false);
  const recovery = parseConfig({ projectCaptured: false, retrieval: { mode: "source" } }, {});
  assert.equal(recovery.projectCaptured, false);
  assert.equal(recovery.retrieval.mode, "source");
});

test("native chat sharing is user-enabled, future-only, and independent of private capture", async () => {
  let classified = 0;
  const f = fixture({ sharing: { classifier: { provider: "fixture", model: "small" } } }, {
    classifySharing: async (_ctx, selected) => {
      classified++;
      return selected.map(entry => ({ entryId: entry.entryId, excerpt: entry.text, category: "decision", visibility: "shareable", confidence: 0.99 }));
    },
  });
  f.client.scope = async selected => ({ ...selected, id: selected.scope_id, kind: selected.world_id === "shared" ? "world-shared" : "invocation-private", name: "Selected" });
  f.entries.push(message("before-rule", null, "Earlier private conversation"));
  await f.run("session_start", { reason: "startup" });
  await f.run("agent_end");
  assert.equal(classified, 0);
  assert.equal(f.publications.length, 0);
  await f.command(`sharing auto shared ${rootScope}`);
  assert.match(f.widgets.get("memory-worlds"), /sharing auto/);
  f.entries.push(message("eligible", f.entries.at(-1)?.id, "We decided to use a bounded retry queue."));
  await f.run("agent_end");
  assert.equal(classified, 1);
  assert.equal(f.publications.length, 1);
  assert.equal(f.publications[0].input.content, "We decided to use a bounded retry queue.");
  assert.deepEqual(Object.keys(f.publications[0].input).sort(), ["content", "occurred_at", "project", "source_id"]);
  assert.equal(f.publications[0].input.project, true);
  await f.command("sharing status");
  const sharingStatus = JSON.parse(f.notices.at(-1).text);
  assert.equal(sharingStatus.accepted, 1);
  assert.equal(sharingStatus.failed, 0);
  assert.equal(f.captures.length, 0, "Sharing must not enable raw private transcript capture");
  await f.command("sharing private");
  f.entries.push(message("after-private", f.entries.at(-1)?.id, "Another later decision"));
  await f.run("agent_end");
  assert.equal(classified, 1);
  assert.equal(f.publications.length, 1);
  assert(!JSON.stringify(f.publications).includes("Earlier private conversation"));
});

test("chat sharing configuration cannot enable publication and tree navigation stops its rule", async () => {
  assert.throws(() => parseConfig({ sharing: { classifier: { provider: "fixture", model: "small" }, mode: "auto" } }, {}), /sharing|Unknown/i);
  const f = fixture({ sharing: { classifier: { provider: "fixture", model: "small" } } }, { classifySharing: async () => { throw new Error("Classifier must stay disabled"); } });
  f.client.scope = async selected => ({ ...selected, id: selected.scope_id, kind: selected.world_id === "shared" ? "world-shared" : "invocation-private", name: "Selected" });
  await f.run("session_start", { reason: "startup" });
  await f.command(`sharing auto shared ${rootScope}`);
  assert.match(f.widgets.get("memory-worlds"), /sharing auto/);
  await f.run("session_tree");
  assert.match(f.widgets.get("memory-worlds"), /sharing private/);
  f.entries.push(message("navigated", f.entries.at(-1)?.id, "We decided to keep this private."));
  await f.run("agent_end");
  assert.equal(f.publications.length, 0);
  assert.equal(f.tools.has("memory_publish"), false);
});

test("interactive sharing selection chooses an authorized named scope without model publication tools", async () => {
  const f = fixture({ sharing: { classifier: { provider: "fixture", model: "small" } } });
  f.client.scope = async selected => ({ ...selected, id: selected.scope_id, kind: selected.world_id === "shared" ? "world-shared" : "invocation-private", name: "Selected" });
  f.client.scopes = async worldId => [{ world_id: worldId, id: rootScope, kind: "world-shared", name: "Team knowledge" }];
  await f.run("session_start", { reason: "startup" });
  f.ctx.hasUI = true;
  f.ctx.ui.select = async (title, options) => {
    if (title === "Memory Worlds") { assert(options.includes("sharing")); return "sharing"; }
    if (title === "Chat sharing") return "review";
    assert.equal(title, "Sharing destination");
    assert.equal(options.length, 1, "Read-only mount must not appear as a sharing destination");
    assert.match(options[0], /Team knowledge.*shared\//);
    return options[0];
  };
  await f.command("");
  assert.match(f.widgets.get("memory-worlds"), /sharing review/);
  assert.equal(f.publications.length, 0);
  assert.equal(f.tools.has("memory_publish"), false);
});

test("graceful Pi shutdown preserves a verified sharing rule for the same chat's next session start", async () => {
  const f = fixture({ sharing: { classifier: { provider: "fixture", model: "small" } } }, {
    classifySharing: async (_ctx, selected) => selected.map(entry => ({ entryId: entry.entryId, excerpt: entry.text, category: "procedure", visibility: "shareable", confidence: 0.99 })),
  });
  f.client.scope = async selected => ({ ...selected, id: selected.scope_id, kind: selected.world_id === "shared" ? "world-shared" : "invocation-private", name: "Selected" });
  await f.run("session_start", { reason: "startup" });
  await f.command(`sharing auto shared ${rootScope}`);
  await f.run("session_shutdown");
  await f.run("session_start", { reason: "startup" });
  assert.match(f.widgets.get("memory-worlds"), /sharing auto/);
  assert(!f.widgets.get("memory-worlds").includes("auto paused"));
  f.entries.push(message("resumed-procedure", f.entries.at(-1)?.id, "Isolate the fixture before replacing the bearing."));
  await f.run("agent_end");
  assert.equal(f.publications.length, 1);
  assert.equal(f.publications[0].input.content, "Isolate the fixture before replacing the bearing.");
});

test("the extension registers native tools, command, UI and lifecycle hooks", async () => {
  const f = fixture();
  await f.run("session_start", { reason: "startup" });
  assert.deepEqual([...f.tools.keys()], ["memory_search", "memory_capture", "graph", "memory_status"]);
  assert.equal(f.tools.has("memory_publish"), false);
  assert.match(f.widgets.get("memory-worlds"), /capture off/);
  await f.run("agent_end");
  assert.equal(f.captures.length, 0);
  assert(!JSON.stringify(f.entries).includes("tokenFile"));
});

test("automatic capture is private, future-only, branch-preserving and idempotent", async () => {
  const f = fixture();
  f.entries.push(message("old", null, "before consent"));
  await f.run("session_start", { reason: "startup" });
  await f.command("capture on");
  f.entries.push(message("new", f.entries.at(-1).id, "new evidence"), message("answer", "new", "assistant observation", "assistant"));
  await f.run("agent_end");
  await f.run("agent_end");
  assert.equal(f.captures.length, 2);
  assert.equal(f.captures[0].input.source_id, "pi:session:new");
  assert.equal(f.captures[0].input.occurred_at, timestamp);
  assert.equal(f.captures[1].input.parent_entry_id, "new");
  assert.equal(f.entries.filter(entry => entry.customType === RECEIPT_TYPE).length, 2);
  f.client.scope = async selected => ({ ...selected, id: selected.scope_id, kind: "world-shared" });
  await f.command(`target shared ${rootScope}`);
  assert(f.notices.some(notice => /agent-persistent or invocation-private/.test(notice.text)));
});

test("failed capture retries the exact durable source identity", async () => {
  const f = fixture({ automaticCapture: true });
  await f.run("session_start", { reason: "startup" });
  f.entries.push(message("new", f.entries.at(-1).id, "evidence"));
  const attempts = [];
  f.client.captureScope = async (worldId, input) => { attempts.push(structuredClone(input)); if (attempts.length === 1) throw new Error("offline"); return { episode: { id: "receipt" }, replayed: true }; };
  await f.run("agent_end"); await f.run("agent_end");
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts[0], attempts[1]);
});

test("cross-authority retrieval disables capture before a turn completes and cannot be cleared by branching or compaction", async () => {
  const f = fixture({ automaticCapture: true, retrieval: { enabled: true } });
  await f.run("session_start", { reason: "startup" });
  const cleanBranch = [...f.entries];
  f.client.searchScopes = async () => ({ results: [{ authority_id: "b", world_id: "team", scope_id: siblingScope, record_type: "fact", fact: "work fact" }], failures: [], complete: true });
  const context = await f.run("before_agent_start", { messageType: "user", prompt: "work fact" });
  assert.match(context.message.content, /untrusted data/);
  assert.equal(context.message.customType, CONTEXT_TYPE);
  f.entries.push(message("mixed", f.entries.at(-1).id, "mixed answer", "assistant"));
  await f.run("agent_end");
  assert.equal(f.captures.length, 0);
  f.setBranch(cleanBranch);
  await f.run("session_tree"); await f.run("session_compact");
  await f.command("capture on");
  assert(f.notices.some(notice => /clean session/.test(notice.text)));
  assert.match(f.widgets.get("memory-worlds"), /capture off.*mixed authorities/);
});

test("publication is an explicit selected-text user command without private provenance links", async () => {
  const f = fixture(); await f.run("session_start", { reason: "startup" });
  await f.command("publish team shared Selected public statement");
  assert.equal(f.publications.length, 1);
  assert.deepEqual(Object.keys(f.publications[0]).sort(), ["content", "destination_mount_id", "occurred_at", "project", "source_id", "world_id"]);
  assert.equal(f.publications[0].content, "Selected public statement");
  assert.equal(f.publications[0].project, true);
  await f.command("publish team private Oops");
  assert.equal(f.publications.length, 1);
});

test("forking an old clean entry inherits authority exposure from the complete parent session", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-parent-"));
  try {
    const path = join(dir, "parent.jsonl");
    await writeFile(path, [
      JSON.stringify({ type: "session", id: "parent" }),
      JSON.stringify({ type: "custom", customType: EXPOSURE_TYPE, data: { exposedAuthorities: ["a", "b"], exposedScopes: [] } }),
    ].join("\n"));
    const f = fixture({ automaticCapture: true });
    await f.run("session_start", { reason: "fork", previousSessionFile: path });
    await f.command("capture on");
    assert.match(f.widgets.get("memory-worlds"), /capture off.*mixed authorities/);
    assert(f.notices.some(notice => /clean session/.test(notice.text)));
    assert.deepEqual(f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data.exposedAuthorities, ["a", "b"]);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("an unavailable parent session prevents automatic capture rather than clearing its exposure", async () => {
  const f = fixture({ automaticCapture: true });
  await f.run("session_start", { reason: "fork", previousSessionFile: "/missing/memory-worlds-parent.jsonl" });
  await f.command("capture on");
  assert.match(f.widgets.get("memory-worlds"), /capture off.*exposure unknown/);
  assert(f.notices.some(notice => /clean session/.test(notice.text)));
});

test("an in-memory fork preserves foreign exposure and cannot clear unknown exposure on reload", async () => {
  const f = fixture({ automaticCapture: true, retrieval: { enabled: true } });
  await f.run("session_start", { reason: "startup" });
  const cleanAncestors = structuredClone(f.entries);
  f.client.searchScopes = async () => ({ results: [{ authority_id: "b", world_id: "team", scope_id: siblingScope, record_type: "fact", fact: "work fact" }], failures: [], complete: true });
  await f.run("before_agent_start", { messageType: "user", prompt: "work fact" });
  assert.match(f.widgets.get("memory-worlds"), /capture off.*mixed authorities/);

  // Pi --no-session copies the selected path and supplies no previous session file.
  f.entries.splice(0, f.entries.length, ...cleanAncestors);
  await f.run("session_start", { reason: "fork", previousSessionFile: undefined });
  assert.match(f.widgets.get("memory-worlds"), /capture off.*exposure unknown/);
  assert.deepEqual(f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data.exposedAuthorities, ["a", "b"]);
  await f.command("capture on");
  await f.run("session_start", { reason: "startup" });
  await f.command("capture on");
  assert.match(f.widgets.get("memory-worlds"), /capture off.*exposure unknown/);
  f.entries.push(message("after-fork", f.entries.at(-1).id, "mixed context"));
  await f.run("agent_end");
  assert.equal(f.captures.length, 0);

  // A genuinely new independent session does not inherit the previous session.
  f.entries.splice(0, f.entries.length);
  await f.run("session_start", { reason: "new" });
  assert.match(f.widgets.get("memory-worlds"), /capture → private/);
  assert(!f.widgets.get("memory-worlds").includes("exposure unknown"));
});

test("a clean fork captures future entries without re-ingesting copied ancestors", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-clean-parent-"));
  try {
    const path = join(dir, "parent.jsonl");
    await writeFile(path, JSON.stringify({ type: "session", id: "parent" }) + "\n");
    const f = fixture({ automaticCapture: true });
    f.entries.push({ type: "custom", id: "consent", customType: STATE_TYPE, data: { ...initialState(parseConfig({ writeScope: { world_id: "private", scope_id: privateScope }, readScopes: [{ world_id: "private", scope_id: privateScope }] }, {}), null), captureEnabled: true, captureAuthority: "a", exposedAuthorities: ["a"] } });
    f.entries.push(message("ancestor", "consent", "already present in the parent"));
    await f.run("session_start", { reason: "fork", previousSessionFile: path });
    await f.run("agent_end");
    assert.equal(f.captures.length, 0);
    f.entries.push(message("new-fork-entry", f.entries.at(-1).id, "new fork evidence"));
    await f.run("agent_end");
    assert.equal(f.captures.length, 1);
    assert.equal(f.captures[0].input.source_entry_id, "new-fork-entry");
    assert.equal(f.captures[0].input.occurred_at, timestamp);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("explicit selected text preserves whitespace and line breaks", async () => {
  const f = fixture(); await f.run("session_start", { reason: "startup" });
  await f.command("capture first  line\nsecond line");
  await f.command("publish team shared first  line\nsecond line");
  assert.equal(f.captures[0].input.content, "first  line\nsecond line");
  assert.equal(f.publications[0].content, "first  line\nsecond line");
});

test("repeating publication after a lost acknowledgment reuses its immutable request", async () => {
  const f = fixture(); await f.run("session_start", { reason: "startup" });
  const attempts = [];
  f.client.publish = async input => { attempts.push(input); if (attempts.length === 1) throw new Error("lost response"); return { id: "delivery", state: "pending" }; };
  await f.command("publish team shared Selected statement");
  await f.command("publish team shared Selected statement");
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts[0], attempts[1]);
  assert(!JSON.stringify(f.entries).includes("Selected statement"));
});

test("session shutdown aborts in-flight retrieval and clears UI", async () => {
  const f = fixture({ retrieval: { enabled: true } }); await f.run("session_start", { reason: "startup" });
  let aborted = false;
  f.client.searchScopes = async (_scopes, _query, _mode, _limit, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => { aborted = true; reject(signal.reason); });
  });
  const pending = f.run("before_agent_start", { messageType: "user", prompt: "question" });
  await f.run("session_shutdown", { reason: "reload" });
  await pending;
  assert.equal(aborted, true);
  assert.equal(f.widgets.get("memory-worlds"), undefined);
});

test("state restoration unions exposure outside the active branch and source IDs preserve entry parentage", () => {
  const fallback = { ...initialState(parseConfig({}, {}), null), captureEnabled: true, captureAuthority: "a", exposedAuthorities: ["a"] };
  const all = [{ type: "custom", id: "x", customType: EXPOSURE_TYPE, data: { exposedAuthorities: ["a", "b"], exposedScopes: [] } }];
  const restored = restoreState([], all, fallback);
  assert.equal(restored.captureEnabled, false);
  assert.deepEqual(restored.exposedAuthorities, ["a", "b"]);
  assert.equal(expose(restored, []), false);
  const entry = entryEpisode("session", message("child", "parent", "text"));
  assert.equal(entry.parent_entry_id, "parent");
  assert.deepEqual(entry, entryEpisode("session", message("child", "parent", "text")));
});

test("retrieval bounds evidence and strips terminal control sequences", () => {
  const result = { results: [{ authority_id: "a", world_id: "w", scope_id: rootScope, record_type: "source", id: "e", content: "\x1b[2J" + "x".repeat(5000) }], failures: [], complete: false };
  const context = memoryContext(result, 512);
  assert(context.length <= 512);
  assert(!context.includes("\x1b"));
  assert.match(context, /local\/a\/w\/00000000-0000-4000-8000-000000000001 source e/);
});


test("private invocation capture can live inside a team world without enabling shared transcript capture", async () => {
  const f = fixture({ writeScope: { world_id: "shared", scope_id: privateScope }, automaticCapture: true });
  await f.run("session_start", { reason: "startup" });
  f.entries.push(message("private-work", f.entries.at(-1).id, "private child note"));
  await f.run("agent_end");
  assert.equal(f.captures.length, 1);
  assert.equal(f.captures[0].worldId, "shared"); assert.equal(f.captures[0].scopeId, privateScope);
  f.client.scope = async selected => ({ ...selected, id: selected.scope_id, kind: "world-shared" });
  await f.command(`target shared ${rootScope}`);
  assert(f.notices.some(notice => /requires agent-persistent or invocation-private/.test(notice.text)));
});

test("same-authority sibling-private retrieval stops automatic capture through branch changes and reload", async () => {
  const f = fixture({ writeScope: { world_id: "private", scope_id: privateScope }, automaticCapture: true, retrieval: { enabled: true } });
  await f.run("session_start", { reason: "startup" });
  const clean = [...f.entries];
  f.client.searchScopes = async () => ({ results: [{ authority_id: "a", world_id: "private", scope_id: siblingScope, record_type: "source", content: "sibling scratch" }], failures: [], complete: true });
  await f.run("before_agent_start", { messageType: "user", prompt: "scratch", systemPrompt: "Original" });
  assert.match(f.widgets.get("memory-worlds"), /capture off.*private scope exposure/);
  f.setBranch(clean); await f.run("session_tree"); await f.run("session_compact"); await f.command("capture on");
  assert(f.notices.some(notice => /clean session/.test(notice.text)));
  f.entries.push(message("after-sibling", f.entries.at(-1).id, "mixed context"));
  await f.run("agent_end"); assert.equal(f.captures.length, 0);
});

test("reading the same world's shared scope keeps a narrower private capture target enabled", async () => {
  const f = fixture({ writeScope: { world_id: "private", scope_id: privateScope }, automaticCapture: true, retrieval: { enabled: true } });
  await f.run("session_start", { reason: "startup" });
  f.client.searchScopes = async () => ({ results: [{ authority_id: "a", world_id: "private", scope_id: rootScope, record_type: "source", content: "world shared finding" }], failures: [], complete: true });
  const result = await f.run("before_agent_start", { messageType: "user", prompt: "finding", systemPrompt: "Original" });
  assert.match(result.systemPrompt, /Original/); assert.match(result.systemPrompt, /scope boundaries/);
  f.entries.push(message("private-summary", f.entries.at(-1).id, "narrow private followup"));
  await f.run("agent_end"); assert.equal(f.captures.length, 1);
  assert.equal(f.captures[0].scopeId, privateScope);
});

test("lifecycle capture flushes before compaction and shutdown with immutable receipt retries", async () => {
  const f = fixture({ automaticCapture: true }); await f.run("session_start", { reason: "startup" });
  f.entries.push(message("precompact", f.entries.at(-1).id, "flush before summary"));
  await f.run("session_before_compact"); await f.run("session_compact");
  f.entries.push(message("shutdown-note", f.entries.at(-1).id, "flush at shutdown"));
  await f.run("session_shutdown", { reason: "quit" });
  assert.deepEqual(f.captures.map(capture => capture.input.source_entry_id), ["precompact", "shutdown-note"]);
});

test("scoped selected publication retries exactly and does not enable automatic capture", async () => {
  const f = fixture(); await f.run("session_start", { reason: "startup" });
  await f.command(`share shared ${rootScope} Selected release note`);
  await f.command(`share shared ${rootScope} Selected release note`);
  assert.equal(f.publications.length, 2); assert.deepEqual(f.publications[0], f.publications[1]);
  assert.equal(f.publications[0].input.content, "Selected release note");
  assert(!JSON.stringify(f.entries).includes("Selected release note"));
  assert.match(f.widgets.get("memory-worlds"), /capture off/);
});

test("native graph tool records private projected memory and exposes no publication tool", async () => {
  const selected = { world_id: "private", scope_id: privateScope };
  const f = fixture({ writeScope: selected, graphAliases: { project: selected } });
  await f.run("session_start", { reason: "startup" });
  f.entries.push({ type: "message", id: "call", parentId: f.entries.at(-1).id, timestamp, message: { role: "assistant", content: [{ type: "toolCall", id: "graph-call", name: "graph", arguments: {} }] } });
  const result = await f.tools.get("graph").execute("graph-call", { action: "add", content: "selected durable claim", source: "message" }, undefined, undefined, f.ctx);
  assert.equal(f.captures[0].scopeId, privateScope); assert.equal(f.captures[0].input.project, true);
  assert.equal(f.captures[0].input.metadata.graphiti_source, "message");
  assert.match(result.content[0].text, /scoped-receipt/); assert.equal(f.tools.has("memory_publish"), false);
});


test("forks create a new context invocation while retaining stable agent UUID and monotonic exposure", async () => {
  const f = fixture({ writeScope: { world_id: "private", scope_id: privateScope } });
  await f.run("session_start", { reason: "startup" });
  const first = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  await f.run("session_start", { reason: "fork" });
  const fork = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  assert.equal(first.agentUuid, fork.agentUuid);
  assert.notEqual(first.boundContext.id, fork.boundContext.id);
  assert.notEqual(first.boundContext.invocation_id, fork.boundContext.invocation_id);
});

test("scoped source hits from another private world stop same-authority automatic capture", async () => {
  const f = fixture({ automaticCapture: true, retrieval: { enabled: true } });
  await f.run("session_start", { reason: "startup" });
  f.client.searchScopes = async () => ({ results: [{ authority_id: "a", world_id: "other-private", scope_id: siblingScope, record_type: "source", content: "other context" }], failures: [], complete: true });
  await f.run("before_agent_start", { messageType: "user", prompt: "other context" });
  assert.match(f.widgets.get("memory-worlds"), /capture off.*private scope exposure/);
});

test("scoped read selection changes bind a new immutable context", async () => {
  const f = fixture({ writeScope: { world_id: "private", scope_id: privateScope } });
  await f.run("session_start", { reason: "startup" });
  const first = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data.boundContext;
  await f.command(`read private ${siblingScope}`);
  const changed = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data.boundContext;
  assert.notEqual(first.id, changed.id);
  assert(changed.read_scopes.some(scope => scope.scope_id === siblingScope));
});

test("configured filesystem spool makes shutdown local and survives reopening without credential disclosure", async () => {
  const folder = await mkdtemp(join(tmpdir(), "mw-extension-spool-"));
  try {
    const selected = { world_id: "private", scope_id: privateScope };
    const options = { writeScope: selected, automaticCapture: true, spoolDirectory: join(folder, "queue") };
    const f = fixture(options); await f.run("session_start", { reason: "startup" });
    f.entries.push(message("offline-entry", f.entries.at(-1).id, "private offline note"));
    f.client.identity = async () => assert.fail("shutdown must not contact the daemon");
    f.client.captureScope = async () => assert.fail("shutdown must only enqueue");
    await f.run("session_shutdown", { reason: "quit" });
    const { MemorySpool } = await import("memory-worlds-pi/spool");
    assert.equal((await new MemorySpool(options.spoolDirectory).status()).pending, 1);
    const reopened = fixture(options); reopened.entries.push(...structuredClone(f.entries));
    const captures = [];
    reopened.client.captureScope = async (scope, input) => { captures.push(input); return { episode: { ...input, ...scope, id: "durable" }, replayed: false }; };
    await reopened.run("session_start", { reason: "startup" });
    await reopened.command("spool drain");
    assert.equal(captures.length, 1); assert.equal(captures[0].content, "private offline note");
    assert.equal((await new MemorySpool(options.spoolDirectory).status()).pending, 0);
  } finally { await rm(folder, { recursive: true, force: true }); }
});


test("graph command retries retain their original context after read selection rebinds", async () => {
  const selected = { world_id: "private", scope_id: privateScope };
  const f = fixture({ writeScope: selected, graphAliases: { project: selected } });
  await f.run("session_start", { reason: "startup" });
  await f.commands.get("graph").handler("add Stable selected claim", f.ctx);
  const first = structuredClone(f.captures[0].input);
  await f.command(`read private ${siblingScope}`);
  await f.commands.get("graph").handler("add Stable selected claim", f.ctx);
  assert.equal(f.captures.length, 2); assert.deepEqual(f.captures[1].input, first);
});


test("a proven run-shared audience can feed automatic invocation-private capture", async () => {
  const selected = { world_id: "private", scope_id: privateScope };
  const f = fixture({ writeScope: selected, automaticCapture: true, retrieval: { enabled: true } });
  const flowChecks = [];
  f.client.canFlow = async (reads, target) => { flowChecks.push({ reads, target }); return true; };
  await f.run("session_start", { reason: "startup" });
  f.client.searchScopes = async () => ({ results: [{ authority_id: "a", world_id: "private", scope_id: siblingScope, record_type: "source", content: "run-shared brief" }], failures: [], complete: true });
  await f.run("before_agent_start", { messageType: "user", prompt: "brief" });
  f.entries.push(message("private-working-note", f.entries.at(-1).id, "private analysis after reading shared brief"));
  await f.run("agent_end");
  assert.equal(f.captures.length, 1); assert.equal(f.captures[0].scopeId, privateScope);
  assert(flowChecks.some(check => check.reads.some(scope => scope.scope_id === siblingScope) && check.target.scope_id === privateScope));
  f.client.canFlow = async () => false;
  f.entries.push(message("after-acl-change", f.entries.at(-1).id, "do not capture into a now wider audience"));
  await f.run("agent_end"); assert.equal(f.captures.length, 1);
  assert.match(f.widgets.get("memory-worlds"), /capture off/);
});


test("credential rotation disables inherited capture and explicit re-enable starts at the new leaf", async () => {
  const options = { writeScope: { world_id: "private", scope_id: privateScope }, automaticCapture: true };
  const original = fixture(options); await original.run("session_start", { reason: "startup" });
  original.entries.push(message("old-unacknowledged", original.entries.at(-1).id, "old credential note"));
  const reopened = fixture(options); reopened.entries.push(...structuredClone(original.entries));
  reopened.client.identity = async () => ({ authority_id: "a", credential_id: "rotated" });
  reopened.client.bindContext = async input => ({ ...input, id: randomUUID(), invocation_id: randomUUID(), credential_id: "rotated" });
  await reopened.run("session_start", { reason: "startup" });
  await reopened.run("agent_end"); assert.equal(reopened.captures.length, 0);
  assert.match(reopened.widgets.get("memory-worlds"), /capture off/);
  assert(reopened.notices.some(notice => /credential or authority changed/.test(notice.text)));
  await reopened.command("capture on");
  reopened.entries.push(message("new-credential-entry", reopened.entries.at(-1).id, "new future note"));
  await reopened.run("agent_end");
  assert.deepEqual(reopened.captures.map(capture => capture.input.source_entry_id), ["new-credential-entry"]);
});


test("optional correction review proposes bounded private memories without automatic writes", async () => {
  let reviews = 0;
  const f = fixture({ reviewCorrections: true }, { review: async selected => { reviews++; return [{ content: "Prefer explicit retry bounds", sourceEntryIds: [selected[0].entryId], reason: "correction", confidence: 0.8 }]; } });
  await f.run("session_start", { reason: "startup" });
  f.entries.push(message("correction-source", f.entries.at(-1).id, "Actually, use explicit retry bounds."));
  await f.run("before_agent_start", { messageType: "user", prompt: "Actually, use explicit retry bounds.", systemPrompt: "Base policy" });
  await f.run("before_agent_start", { messageType: "user", prompt: "Actually, use explicit retry bounds.", systemPrompt: "Base policy" });
  assert.equal(reviews, 1); assert.equal(f.captures.length, 0); assert.equal(f.publications.length, 0);
  assert(f.entries.some(entry => entry.customType === "memory-worlds:curation-proposals:v1"));
  assert(f.notices.some(notice => /nothing saved/.test(notice.text)));
});

test("an offline source keeps its original flow guard when later safe reads broaden session exposure", async () => {
  const folder = await mkdtemp(join(tmpdir(), "mw-flow-intent-"));
  try {
    const f = fixture({ writeScope: { world_id: "private", scope_id: privateScope }, automaticCapture: true, retrieval: { enabled: true }, spoolDirectory: join(folder, "queue") });
    f.client.canFlow = async () => true;
    await f.run("session_start", { reason: "startup" });
    f.entries.push(message("before-later-read", f.entries.at(-1).id, "first private note"));
    f.client.captureScope = async () => { throw new Error("lost acknowledgment"); };
    await f.run("agent_end");
    f.client.searchScopes = async () => ({ results: [{ authority_id: "a", world_id: "private", scope_id: siblingScope, record_type: "source", content: "safe run brief" }], failures: [], complete: true });
    await f.run("before_agent_start", { messageType: "user", prompt: "brief" });
    f.entries.push(message("after-later-read", f.entries.at(-1).id, "second private note"));
    const delivered = [];
    f.client.captureScope = async (target, input) => { delivered.push(input); return { episode: { ...input, ...target, id: randomUUID() }, replayed: true }; };
    await f.run("agent_end");
    assert.equal(delivered.length, 2);
    const { MemorySpool } = await import("memory-worlds-pi/spool");
    assert.equal((await new MemorySpool(join(folder, "queue")).status()).pending, 0);
  } finally { await rm(folder, { recursive: true, force: true }); }
});


test("a delegated Pi session binds the server's fixed agent UUID instead of inventing an identity", async () => {
  const fixedAgent = "00000000-0000-4000-8000-000000000099";
  const f = fixture({ writeScope: { world_id: "private", scope_id: privateScope } });
  f.client.identity = async () => ({ authority_id: "a", credential_id: "fixture", restricted: true, agent_uuid: fixedAgent, invocation_id: siblingScope });
  f.client.bindContext = async input => { assert.equal(input.agent_id, fixedAgent); return { ...input, id: randomUUID(), credential_id: "fixture", invocation_id: siblingScope }; };
  await f.run("session_start", { reason: "startup" });
  const state = f.entries.filter(entry => entry.customType === STATE_TYPE).at(-1).data;
  assert.equal(state.agentUuid, fixedAgent); assert.equal(state.boundContext.invocation_id, siblingScope);
});


test("malformed current session scope bindings fail closed", () => {
  const fallback = initialState(parseConfig({}, {}), null);
  const malformed = { ...fallback, captureEnabled: true, readScopes: [{ world_id: "private", scope_id: "invalid" }] };
  const entry = { type: "custom", customType: STATE_TYPE, data: malformed };
  assert.throws(() => restoreState([entry], [entry], fallback), /Invalid scope/);
});
