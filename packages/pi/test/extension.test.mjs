import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registerMemoryWorlds } from "../dist/extension.js";
import { parseConfig } from "../dist/config.js";
import { CONTEXT_TYPE, entryEpisode, expose, EXPOSURE_TYPE, memoryContext, RECEIPT_TYPE, restoreState, STATE_TYPE } from "../dist/state.js";

const world = { id: "private", name: "Private", authority_id: "a", kind: "personal", owner_id: "alice" };
const timestamp = "2026-10-20T12:00:00.000Z";
function message(id, parentId, text, role = "user") {
  return { type: "message", id, parentId, timestamp, message: { role, content: role === "user" ? text : [{ type: "text", text }] } };
}
function fixture(configOverrides = {}) {
  const hooks = new Map(), tools = new Map(), commands = new Map(), renderers = new Map();
  const entries = [], notices = [], captures = [], publications = [], widgets = new Map();
  let branch = entries;
  let generated = 0;
  const pi = {
    on: (name, fn) => hooks.set(name, fn), registerTool: tool => tools.set(tool.name, tool),
    registerCommand: (name, command) => commands.set(name, command), registerMessageRenderer: (name, renderer) => renderers.set(name, renderer),
    appendEntry: (customType, data) => entries.push({ type: "custom", id: `s${++generated}`, parentId: entries.at(-1)?.id ?? null, timestamp, customType, data: structuredClone(data) }),
  };
  const ctx = {
    signal: undefined, hasUI: false, mode: "print", cwd: "/tmp",
    ui: { notify: (text, type) => notices.push({ text, type }), setStatus: (key, value) => widgets.set(key, value), setWidget: (key, value) => widgets.set(key + ":widget", value) },
    sessionManager: { getSessionId: () => "session", getLeafId: () => branch.at(-1)?.id ?? null, getBranch: () => [...branch], getEntries: () => [...entries], getHeader: () => ({}) },
  };
  const client = {
    worlds: async () => [world, { ...world, id: "shared", kind: "team" }], mounts: async () => [{ id: "team", name: "Team", world_ids: ["shared"] }], deliveries: async () => [],
    capture: async (worldId, input) => { captures.push({ worldId, input }); return { episode: { id: "receipt" }, replayed: captures.length > 1 }; },
    search: async () => ({ results: [{ authority_id: "a", world_id: "private", record_type: "source", content: "bearing" }], failures: [], complete: true }),
    publish: async input => { publications.push(input); return { id: "delivery", state: "pending" }; },
  };
  const config = parseConfig({ writeWorldId: "private", readWorldIds: ["private"], ...configOverrides }, {});
  registerMemoryWorlds(pi, { config: async () => config, client: () => client });
  return { pi, hooks, tools, commands, ctx, client, entries, captures, publications, notices, widgets, setBranch: value => { branch = value; }, run: (event, value = {}) => hooks.get(event)({ type: event, ...value }, ctx), command: args => commands.get("memory").handler(args, ctx) };
}

test("the extension registers native tools, command, UI and lifecycle hooks", async () => {
  const f = fixture();
  await f.run("session_start", { reason: "startup" });
  assert.deepEqual([...f.tools.keys()], ["memory_search", "memory_capture", "memory_status"]);
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
  await f.command("target shared");
  assert(f.notices.some(notice => /personal or private-work/.test(notice.text)));
});

test("failed capture retries the exact durable source identity", async () => {
  const f = fixture({ automaticCapture: true });
  await f.run("session_start", { reason: "startup" });
  f.entries.push(message("new", f.entries.at(-1).id, "evidence"));
  const attempts = [];
  f.client.capture = async (worldId, input) => { attempts.push(structuredClone(input)); if (attempts.length === 1) throw new Error("offline"); return { episode: { id: "receipt" }, replayed: true }; };
  await f.run("agent_end"); await f.run("agent_end");
  assert.equal(attempts.length, 2);
  assert.deepEqual(attempts[0], attempts[1]);
});

test("cross-authority retrieval disables capture before a turn completes and cannot be cleared by branching or compaction", async () => {
  const f = fixture({ automaticCapture: true, retrieval: { enabled: true } });
  await f.run("session_start", { reason: "startup" });
  const cleanBranch = [...f.entries];
  f.client.search = async () => ({ results: [{ authority_id: "b", world_id: "team", record_type: "fact", fact: "work fact" }], failures: [], complete: true });
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
  assert.equal(f.publications[0].project, false);
  await f.command("publish team private Oops");
  assert.equal(f.publications.length, 1);
});

test("forking an old clean entry inherits authority exposure from the complete parent session", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-parent-"));
  try {
    const path = join(dir, "parent.jsonl");
    await writeFile(path, [
      JSON.stringify({ type: "session", id: "parent" }),
      JSON.stringify({ type: "custom", customType: EXPOSURE_TYPE, data: { exposedAuthorities: ["a", "b"] } }),
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
  f.client.search = async () => ({ results: [{ authority_id: "b", world_id: "team", record_type: "fact", fact: "work fact" }], failures: [], complete: true });
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
    f.entries.push({ type: "custom", id: "consent", customType: STATE_TYPE, data: { version: 1, readWorldIds: ["private"], readMountIds: [], writeWorldId: "private", captureEnabled: true, captureAfterId: null, captureAuthority: "a", exposedAuthorities: ["a"] } });
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
  f.client.search = async (_worlds, _mounts, _query, _mode, _limit, signal) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => { aborted = true; reject(signal.reason); });
  });
  const pending = f.run("before_agent_start", { messageType: "user", prompt: "question" });
  await f.run("session_shutdown", { reason: "reload" });
  await pending;
  assert.equal(aborted, true);
  assert.equal(f.widgets.get("memory-worlds"), undefined);
});

test("state restoration unions exposure outside the active branch and source IDs preserve entry parentage", () => {
  const fallback = { version: 1, readWorldIds: [], readMountIds: [], captureEnabled: true, captureAfterId: null, captureAuthority: "a", exposedAuthorities: ["a"] };
  const all = [{ type: "custom", id: "x", customType: EXPOSURE_TYPE, data: { exposedAuthorities: ["a", "b"] } }];
  const restored = restoreState([], all, fallback);
  assert.equal(restored.captureEnabled, false);
  assert.deepEqual(restored.exposedAuthorities, ["a", "b"]);
  assert.equal(expose(restored, []), false);
  const entry = entryEpisode("session", message("child", "parent", "text"));
  assert.equal(entry.parent_entry_id, "parent");
  assert.deepEqual(entry, entryEpisode("session", message("child", "parent", "text")));
});

test("retrieval bounds evidence and strips terminal control sequences", () => {
  const result = { results: [{ authority_id: "a", world_id: "w", record_type: "source", id: "e", content: "\x1b[2J" + "x".repeat(5000) }], failures: [], complete: false };
  const context = memoryContext(result, 512);
  assert(context.length <= 512);
  assert(!context.includes("\x1b"));
  assert.match(context, /local\/a\/w source e/);
});
