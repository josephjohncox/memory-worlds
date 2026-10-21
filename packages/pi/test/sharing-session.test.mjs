import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";
import { ChatSharing, SHARING_DECISION_TYPE, SHARING_INTENT_TYPE, SHARING_RECEIPT_TYPE, SHARING_RULE_TYPE, parseSharingSettings } from "../dist/sharing-session.js";
import { MemoryWorldsClient } from "../dist/client.js";

const sourceId = "00000000-0000-4000-8000-000000000001";
const targetId = "00000000-0000-4000-8000-000000000002";
const contextId = "00000000-0000-4000-8000-000000000003";
const otherId = "00000000-0000-4000-8000-000000000004";
const sharedId = "00000000-0000-4000-8000-000000000005";
const source = { world_id: "personal", scope_id: sourceId };
const target = { world_id: "team", scope_id: targetId };
const timestamp = "2026-10-20T00:00:00.000Z";
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
async function until(predicate) { for (let i = 0; i < 100; i++) { if (predicate()) return; await delay(1); } assert.fail("fixture operation did not start"); }
function decision(candidate, changes = {}) { return { entryId: candidate.entryId, excerpt: candidate.text, category: "technical-finding", visibility: "shareable", confidence: 0.99, ...changes }; }
function fixture(options = {}) {
  const entries = [], notices = [], calls = [], sent = [], accepted = new Map(), classifications = [];
  let branch = entries, sessionId = "chat-fixture", token = "original-token", unknown = false;
  let settings = parseSharingSettings({ classifier: { provider: "fixture", model: "bounded-classifier" }, ...options.settings });
  const state = { version: 1, agentUuid: randomUUID(), captureEnabled: false, captureAfterId: null, readMountIds: [], exposedAuthorities: ["authority"], exposedScopes: [`authority/personal/${sourceId}`], readScopes: [source], writeScope: source, boundContext: { id: contextId, credential_id: "credential", invocation_id: randomUUID(), created_at: timestamp, read_scopes: [source], write_scope: source } };
  const context = structuredClone(state.boundContext);
  const worlds = [{ id: "personal", authority_id: "authority", name: "Personal", kind: "personal", owner_id: "alice", scope_id: sharedId }, { id: "team", authority_id: "authority", name: "Team", kind: "team", owner_id: "alice", scope_id: targetId }];
  const scopes = new Map([[sourceId, { ...source, id: sourceId, kind: "agent-persistent", name: "Private source" }], [targetId, { ...target, id: targetId, kind: "world-shared", name: "Shared team" }], [otherId, { world_id: "personal", scope_id: otherId, id: otherId, kind: "invocation-private", name: "Sibling scratch" }], [sharedId, { world_id: "personal", scope_id: sharedId, id: sharedId, kind: "world-shared", name: "Shared brief" }]]);
  const mount = { id: "work", name: "Work", authority_id: "remote-authority", url: "https://work.example.test", world_ids: ["remote-team"], can_publish: true };
  const control = { before: undefined, flow: true, lost: 0, authorized: true, tamperReceipt: false };
  const respond = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  const client = new MemoryWorldsClient("http://127.0.0.1:8787", async () => token, async (url, request) => {
    const path = new URL(url).pathname, body = request.body ? JSON.parse(request.body) : undefined;
    const credential = request.headers.Authorization === "Bearer original-token" ? "credential" : "rotated-credential";
    calls.push({ path, body, credential });
    if (control.before) await control.before(path, body);
    request.signal.throwIfAborted();
    if (path === "/v1/me") return respond({ credential_id: credential, user_id: "alice", agent_id: "pi", admin: false, authority_id: "authority", worlds });
    if (path === `/v1/contexts/${contextId}`) return respond(context);
    if (path === "/v1/worlds") return respond(worlds);
    if (path === "/v1/mounts") return respond([mount]);
    if (path === "/v1/scopes/flow") return respond({ allowed: control.flow });
    if (path.endsWith("/publications")) {
      assert(entries.some(entry => entry.customType === SHARING_INTENT_TYPE && entry.data.payload.source_id === body.source_id), "immutable private intent must precede any public HTTP dispatch");
      sent.push({ path, body: structuredClone(body), credential });
      if (!control.authorized) return respond({ error: "forbidden" }, 403);
      if (path === "/v1/router/publications") {
        const delivery = { id: `delivery-${accepted.size}`, state: "pending", destination_mount_id: body.destination_mount_id, destination_authority_id: mount.authority_id, world_id: body.world_id, source_id: body.source_id };
        if (!accepted.has(body.source_id)) accepted.set(body.source_id, delivery);
        if (control.lost-- > 0) throw new Error("secret-token body must never reach notifications");
        return respond(accepted.get(body.source_id));
      }
      const episode = { ...body, id: `episode-${accepted.size}`, ...target, source_type: "publication", metadata: {}, context_id: null };
      if (!accepted.has(body.source_id)) accepted.set(body.source_id, episode);
      if (control.lost-- > 0) throw new Error("secret-token body must never reach notifications");
      return respond({ episode: { ...accepted.get(body.source_id), ...(control.tamperReceipt ? { content: "Wrong acknowledged content" } : {}) }, replayed: false });
    }
    const match = path.match(/\/scopes\/([^/]+)$/);
    if (match && scopes.has(match[1])) return respond(scopes.get(match[1]));
    return respond({ error: "not_found" }, 404);
  });
  const pi = { appendEntry: (customType, data) => {
    const entry = { type: "custom", customType, data: structuredClone(data), id: randomUUID(), parentId: branch.at(-1)?.id ?? null, timestamp };
    entries.push(entry); if (branch !== entries) branch.push(entry);
  } };
  const ctx = { sessionManager: { getEntries: () => [...entries], getBranch: () => [...branch], getSessionId: () => sessionId, getLeafId: () => branch.at(-1)?.id ?? null }, ui: { notify: (message, kind) => notices.push({ message, kind }) } };
  const lifecycle = new AbortController();
  const hooks = { client: () => client, state: () => state, settings: () => settings, signal: () => lifecycle.signal, exposureUnknown: () => unknown, privateTarget: async () => source };
  const classify = async (context, candidates, policy, signal) => {
    classifications.push(structuredClone(candidates));
    return options.classify ? options.classify(context, candidates, policy, signal) : candidates.map(candidate => decision(candidate));
  };
  const build = (dependency = classify) => new ChatSharing(pi, hooks, { classify: dependency });
  const sharing = build();
  const add = (id, content, role = "user") => {
    const entry = { type: "message", id, parentId: branch.at(-1)?.id ?? null, timestamp, message: { role, content } };
    entries.push(entry); if (branch !== entries) branch.push(entry); return entry;
  };
  const proposals = () => entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).flatMap(entry => entry.data.proposals);
  return { sharing, build, ctx, entries, notices, calls, sent, accepted, classifications, state, settings: () => settings, setSettings: value => { settings = value; }, setUnknown: value => { unknown = value; }, rotate: () => { token = "rotated-token"; }, setSession: value => { sessionId = value; }, setBranch: value => { branch = value; }, scopes, mount, control, add, proposals, auto: () => sharing.command(`auto team ${targetId}`, ctx), review: () => sharing.command(`review team ${targetId}`, ctx), flush: () => sharing.flush(ctx) };
}

test("private mode makes no classifier or publication calls and configuration never enables a mode", async () => {
  const f = fixture(); f.add("old", "The bounded retry interval is five seconds.");
  await f.sharing.restore(f.ctx); await f.flush();
  assert.equal(f.sharing.summary().mode, "private"); assert.equal(f.calls.length, 0); assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
});

test("auto mode is future-only, preserves private capture OFF, and sends only exact four-field public envelopes", async () => {
  const f = fixture(); f.add("old", "Earlier context remains in the chat."); await f.auto();
  f.entries.push({ type: "custom", id: "command", customType: "fixture:handled-command", data: { command: "/memory sharing auto" } });
  f.entries.push({ type: "custom_message", id: "retrieved", customType: "memory-worlds:context:v1", content: "Untrusted retrieved data" });
  f.add("tool", "tool output", "toolResult");
  f.add("new", "The bounded retry interval is five seconds.");
  f.add("answer", [{ type: "thinking", thinking: "hidden reasoning" }, { type: "text", text: "The retry deadline is twenty seconds." }], "assistant");
  await Promise.all([f.flush(), f.flush()]);
  assert.equal(f.classifications.length, 1); assert.deepEqual(f.classifications[0].map(item => item.entryId), ["new", "answer"]);
  assert.equal(f.state.captureEnabled, false); assert.equal(f.sent.length, 2);
  for (const item of f.sent) assert.deepEqual(Object.keys(item.body).sort(), ["content", "occurred_at", "project", "source_id"]);
  assert(f.sent.every(item => item.body.project));
  const count = f.entries.length; await f.flush(); assert.equal(f.entries.length, count, "controller custom entries must not generate empty cursor batches forever");
  assert(!JSON.stringify(f.sharing.summary()).includes("five seconds"));
  assert(!JSON.stringify(f.entries).includes("original-token"));
});

test("review mode holds shareable and ambiguous proposals; private decisions cannot be approved", async () => {
  const f = fixture({ classify: async (_ctx, candidates) => candidates.map((candidate, index) => decision(candidate, { visibility: ["shareable", "review", "private"][index], confidence: index ? 0.3 : 0.99 })) });
  await f.review(); f.add("first", "The retry interval is five seconds."); f.add("second", "The batch size is ten entries."); f.add("third", "The cache key is a source digest."); await f.flush();
  assert.equal(f.sent.length, 0); assert.equal(f.sharing.summary().pending, 2);
  const [first, second, third] = f.proposals();
  await assert.rejects(f.sharing.command(`approve ${third.id}`, f.ctx), /pending proposal/);
  await f.sharing.command(`approve ${second.id}`, f.ctx); assert.equal(f.sent[0].body.content, second.decision.excerpt);
  await f.sharing.command(`dismiss ${first.id}`, f.ctx); assert.equal(f.sharing.summary().pending, 0);
});

test("automatic confidence uses the explicit threshold and ambiguous decisions remain for review", async () => {
  const f = fixture({ settings: { minConfidence: 0.8 }, classify: async (_ctx, candidates) => candidates.map((candidate, index) => decision(candidate, { confidence: index ? 0.79 : 0.9 })) });
  await f.auto(); f.add("above", "The retry interval is five seconds."); f.add("below", "The retry deadline is twenty seconds."); await f.flush();
  assert.equal(f.sent.length, 1); assert.equal(f.sharing.summary().pending, 1);
});

test("deterministic exclusions stop credentials, HR, project/person opinions and custom excluded topics before the classifier", async () => {
  const f = fixture({ settings: { excludedTopics: ["embargoed protocol"] } }); await f.auto();
  for (const [index, value] of ["password=unsafe-example", "HR compensation review", "Alice is brilliant.", "The project is terrible.", "This project is great.", "Discuss the embargoed protocol.", "[private] The retry limit is five."].entries()) f.add(`blocked-${index}`, value);
  await f.flush(); assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
});

for (const changes of [{ excerpt: "Invented text" }, { category: "unknown" }, { confidence: NaN }, { excerpt: "\ud83d" }]) {
  test(`dependency output is independently validated: ${JSON.stringify(changes)}`, async () => {
    const f = fixture({ classify: async (_ctx, candidates) => [decision(candidates[0], changes)] }); await f.auto(); f.add("source", "The fixture uses a lathe 🛠 safely."); await f.flush();
    assert.equal(f.sent.length, 0); assert.equal(f.sharing.summary().status, "paused");
  });
}

test("foreign, unknown and sibling-private exposure stop sharing without a classifier call", async () => {
  for (const kind of ["foreign", "unknown", "sibling"]) {
    const f = fixture(); await f.auto(); f.add("source", "The retry interval is five seconds.");
    if (kind === "foreign") f.state.exposedAuthorities.push("foreign-authority");
    else if (kind === "unknown") f.setUnknown(true);
    else f.state.exposedScopes.push(`authority/personal/${otherId}`);
    await f.flush(); assert.equal(f.sharing.summary().mode, "private"); assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
  }
});

test("shared exposure requires current audience containment and private scope exposure cannot pass even when flow would", async () => {
  for (const allowed of [true, false]) {
    const f = fixture(); await f.auto(); f.state.exposedScopes.push(`authority/personal/${sharedId}`); f.control.flow = allowed;
    f.add("source", "The retry interval is five seconds."); await f.flush();
    assert.equal(f.sent.length, allowed ? 1 : 0);
    assert(f.calls.some(call => call.path === "/v1/scopes/flow"));
  }
});

test("same-authority private exposure added while flow validation awaits invalidates the snapshot", async () => {
  const f = fixture(), gate = deferred(); await f.auto(); f.state.exposedScopes.push(`authority/personal/${sharedId}`);
  let started = false; f.control.before = async path => { if (path === "/v1/scopes/flow") { started = true; await gate.promise; } };
  f.add("source", "The retry interval is five seconds."); const operation = f.flush(); await until(() => started);
  f.state.exposedScopes.push(`authority/personal/${otherId}`); gate.resolve(); await operation;
  assert.equal(f.sharing.summary().mode, "private"); assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
});

test("remote publication has only routing plus selected text, and accepted deliveries remain accepted after private mode", async () => {
  const f = fixture(); await f.sharing.command("auto mount work remote-team", f.ctx); f.add("source", "The retry interval is five seconds."); await f.flush();
  assert.equal(f.sent.length, 1); assert.deepEqual(Object.keys(f.sent[0].body).sort(), ["content", "destination_mount_id", "occurred_at", "project", "source_id", "world_id"]);
  assert.equal(f.sharing.summary().accepted, 1); await f.sharing.command("private", f.ctx); await f.flush();
  assert.equal(f.sharing.summary().mode, "private"); assert.equal(f.sharing.summary().accepted, 1); assert.equal(f.sent.length, 1);
  assert(f.notices.some(item => /remain queued/.test(item.message)));
});

test("remote publication rejects other local memory exposure because cross-authority audiences cannot be proved", async () => {
  const f = fixture(); await f.sharing.command("auto mount work remote-team", f.ctx); f.state.exposedScopes.push(`authority/personal/${sharedId}`); f.add("source", "The retry interval is five seconds."); await f.flush();
  assert.equal(f.sent.length, 0); assert.equal(f.classifications.length, 0); assert.equal(f.sharing.summary().mode, "private");
});

test("lost acknowledgment replays the identical first intent and drains remaining automatic decisions without reclassification", async () => {
  const f = fixture(); await f.auto(); f.control.lost = 1; f.add("first", "The retry interval is five seconds."); f.add("second", "The retry deadline is twenty seconds."); await f.flush();
  assert.equal(f.sharing.summary().status, "paused"); assert.equal(f.sent.length, 1); assert.equal(f.classifications.length, 1);
  await f.sharing.command("retry", f.ctx);
  assert.equal(f.sent.length, 3); assert.deepEqual(f.sent[0], f.sent[1]); assert.notEqual(f.sent[2].body.source_id, f.sent[0].body.source_id);
  assert.equal(f.classifications.length, 1); assert.equal(f.accepted.size, 2); assert.equal(f.sharing.summary().pending, 0);
  assert(!JSON.stringify(f.notices).includes("secret-token"));
});

test("restart restores matching private policy but holds unacknowledged intents for explicit immutable retry", async () => {
  const f = fixture(); await f.auto(); f.control.lost = 1; f.add("source", "The retry interval is five seconds."); await f.flush();
  const reopened = f.build(async () => assert.fail("retry must not reclassify stored decisions")); await reopened.restore(f.ctx);
  assert.equal(reopened.summary().status, "paused"); await reopened.flush(f.ctx); assert.equal(f.sent.length, 1);
  await reopened.command("retry", f.ctx); assert.equal(f.sent.length, 2); assert.deepEqual(f.sent[0], f.sent[1]);
});

test("private then auto while a classifier ignores cancellation discards the old generation and excludes its history", async () => {
  const gate = deferred(); let attempt = 0;
  const f = fixture({ classify: async (_ctx, candidates) => ++attempt === 1 ? gate.promise : candidates.map(item => decision(item)) }); await f.auto(); f.add("old-generation", "The retry interval is five seconds.");
  const operation = f.flush(); await until(() => attempt === 1); await f.sharing.command("private", f.ctx); const reactivation = f.auto();
  await operation; await reactivation; gate.resolve([{ entryId: "old-generation", excerpt: "The retry interval is five seconds.", category: "technical-finding", visibility: "shareable", confidence: 0.99 }]);
  await f.flush(); assert.equal(f.sent.length, 0);
  f.add("new-generation", "The retry deadline is twenty seconds."); await f.flush(); assert.equal(f.sent.length, 1); assert.equal(f.sent[0].body.content, "The retry deadline is twenty seconds.");
});

test("credential rotation, context change and settings change during classification invalidate the original binding", async () => {
  for (const kind of ["credential", "context", "settings", "source"]) {
    let f;
    f = fixture({ classify: async (_ctx, candidates) => {
      if (kind === "credential") f.rotate();
      if (kind === "context") f.state.boundContext.id = randomUUID();
      if (kind === "settings") f.setSettings({ ...f.settings(), minConfidence: 0.5 });
      if (kind === "source") f.entries.find(item => item.id === "source").message.content = "The source changed.";
      return candidates.map(item => decision(item));
    } });
    await f.auto(); f.add("source", "The retry interval is five seconds."); await f.flush(); assert.equal(f.sent.length, 0); assert.equal(f.sharing.summary().mode, "private");
  }
});

test("revoked publication grants stop the policy and never overwrite the private original", async () => {
  const f = fixture(); await f.auto(); const original = f.add("source", "The retry interval is five seconds."); f.control.authorized = false; await f.flush();
  assert.equal(f.accepted.size, 0); assert.equal(f.sharing.summary().mode, "private"); assert.equal(original.message.content, "The retry interval is five seconds."); await assert.rejects(f.sharing.command("retry", f.ctx), /Enable the matching/);
});

test("forks, explicit stops and changed settings do not inherit an active publication policy", async () => {
  for (const kind of ["fork", "stop", "settings"]) {
    const f = fixture(); await f.auto();
    if (kind === "stop") f.sharing.stop(f.ctx);
    if (kind === "settings") f.setSettings({ ...f.settings(), maxExcerptChars: 800 });
    const reopened = f.build(); await reopened.restore(f.ctx, kind === "fork"); f.add("source", "The retry interval is five seconds."); await reopened.flush(f.ctx);
    assert.equal(reopened.summary().mode, "private"); assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
  }
});

test("tampered receipt or persisted intent never causes deletion or mutation of the immutable request", async () => {
  const f = fixture(); await f.auto(); f.control.tamperReceipt = true; f.add("source", "The retry interval is five seconds."); await f.flush();
  assert.equal(f.sharing.summary().status, "paused"); assert.equal(f.sharing.summary().failed, 1); assert(!f.entries.some(item => item.customType === SHARING_RECEIPT_TYPE));
  const intent = f.entries.find(item => item.customType === SHARING_INTENT_TYPE); intent.data.payload.content = "Injected alternate content";
  const reopened = f.build(); await reopened.restore(f.ctx); await assert.rejects(reopened.command("retry", f.ctx), /immutable source/);
  assert.equal(reopened.summary().mode, "private"); assert.equal(f.sent.length, 1);
});

test("classifier errors and deadlines leave sources private and redact arbitrary provider diagnostics", async () => {
  for (const timeout of [false, true]) {
    const f = fixture({ settings: { timeoutMs: 100 }, classify: async () => { if (timeout) return new Promise(() => {}); throw new Error("secret-token private source payload"); } });
    await f.auto(); f.add("source", "The retry interval is five seconds.");
    const keeper = setTimeout(() => {}, 2000); try { await f.flush(); } finally { clearTimeout(keeper); }
    assert.equal(f.sent.length, 0); assert.equal(f.sharing.summary().status, "paused"); assert(!JSON.stringify(f.notices).includes("secret-token"));
  }
});


test("graceful runtime suspension preserves the saved policy and future window for a verified same-chat resume", async () => {
  const f = fixture(); await f.auto(); f.add("first-before-close", "The retry interval is five seconds."); await f.flush();
  const savedRules = structuredClone(f.entries.filter(entry => entry.customType === SHARING_RULE_TYPE));
  assert(savedRules.at(-1).data.rule);
  f.sharing.suspend();
  assert.deepEqual(f.entries.filter(entry => entry.customType === SHARING_RULE_TYPE), savedRules, "runtime interruption must not persist an explicit private-mode decision");
  assert.equal(f.sharing.summary().status, "paused");
  f.add("future-on-resume", "The retry deadline is twenty seconds."); await f.flush(); assert.equal(f.sent.length, 1);
  const resumed = f.build(); await resumed.restore(f.ctx);
  assert.equal(resumed.summary().mode, "auto"); assert.equal(resumed.summary().status, "active");
  await resumed.flush(f.ctx); assert.equal(f.sent.length, 2); assert.equal(f.sent[1].body.content, "The retry deadline is twenty seconds.");
  assert.equal(f.classifications.length, 2);
});

test("runtime suspension aborts an ignoring classifier without persisting stale decisions or disabling the saved policy", async () => {
  const gate = deferred(); let attempts = 0;
  const f = fixture({ classify: async (_ctx, candidates) => ++attempts === 1 ? gate.promise : candidates.map(item => decision(item)) });
  await f.auto(); f.add("future-before-close", "The retry interval is five seconds."); const operation = f.flush(); await until(() => attempts === 1);
  f.sharing.suspend(); await operation;
  gate.resolve([{ entryId: "future-before-close", excerpt: "The retry interval is five seconds.", category: "technical-finding", visibility: "shareable", confidence: 0.99 }]);
  await delay(0);
  assert.equal(f.sent.length, 0); assert.equal(f.proposals().length, 0);
  assert(f.entries.filter(entry => entry.customType === SHARING_RULE_TYPE).at(-1).data.rule);
  const resumed = f.build(); await resumed.restore(f.ctx); await resumed.flush(f.ctx);
  assert.equal(f.sent.length, 1); assert.equal(attempts, 2); assert.equal(resumed.summary().mode, "auto");
});


test("invalid, inactive-policy and private proposal approvals leave a healthy automatic policy active", async () => {
  const f = fixture({ classify: async (_ctx, candidates) => candidates.map(item => decision(item, { visibility: item.text.includes("withheld") ? "private" : "shareable" })) });
  await f.review(); f.add("earlier-review", "The retry interval is five seconds."); await f.flush(); const old = f.proposals()[0];
  await f.auto();
  for (const id of [old.id, randomUUID()]) {
    await assert.rejects(f.sharing.command(`approve ${id}`, f.ctx), /pending proposal/);
    assert.equal(f.sharing.summary().status, "active"); assert.equal(f.sharing.summary().mode, "auto");
  }
  f.add("withheld-current", "The withheld candidate uses a bounded cache."); await f.flush();
  const withheld = f.proposals().find(item => item.source.entryId === "withheld-current"); assert(withheld);
  await assert.rejects(f.sharing.command(`approve ${withheld.id}`, f.ctx), /pending proposal/);
  assert.equal(f.sharing.summary().status, "active"); assert.equal(f.sent.length, 0);
  f.add("later-eligible", "The retry deadline is twenty seconds."); await f.flush();
  assert.equal(f.sent.length, 1); assert.equal(f.sent[0].body.content, "The retry deadline is twenty seconds.");
});


test("excluded user context withholds its keyword-free assistant reply, then a clean future turn remains eligible", async () => {
  const f = fixture(); await f.auto();
  f.add("sensitive-user", "HR review: Alice performance");
  f.add("orphan-reply", "She needs more time to meet expectations.", "assistant");
  await f.flush();
  assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
  const withheld = f.entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).at(-1).data;
  assert.equal(withheld.withheld, "excluded-window"); assert.deepEqual(withheld.processed.map(item => item.entryId), ["sensitive-user", "orphan-reply"]);
  assert.equal(withheld.proposals.length, 0); assert.equal(withheld.cursor, "orphan-reply");
  assert(!JSON.stringify(withheld).includes("Alice")); assert(!JSON.stringify(withheld).includes("meet expectations"));
  f.add("clean-next-user", "The retry deadline is twenty seconds.");
  f.add("clean-next-reply", "The retry interval is five seconds.", "assistant");
  await f.flush(); assert.equal(f.classifications.length, 1); assert.equal(f.sent.length, 2);
  assert.deepEqual(f.classifications[0].map(item => item.entryId), ["clean-next-user", "clean-next-reply"]);
});

test("full pending windows are inspected past candidate and character limits without publishing a partial prefix", async () => {
  for (const limit of ["candidates", "characters"]) {
    const f = fixture({ settings: { maxInputChars: limit === "characters" ? 128 : 12000 } }); await f.auto();
    if (limit === "candidates") for (let index = 0; index < 5; index++) f.add(`prefix-${index}`, `The retry interval is ${index + 1} seconds.`);
    else f.add("oversized-prefix", "The bounded retry interval uses five seconds. ".repeat(4));
    f.add("excluded-after-budget", "HR compensation review");
    f.add("reply-after-budget", "She needs more time to meet expectations.", "assistant");
    await f.flush(); await f.flush();
    assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
    const withheld = f.entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).at(-1).data;
    assert.equal(withheld.withheld, "input-budget"); assert.equal(withheld.cursor, "reply-after-budget");
    assert(withheld.processed.length <= 5); assert.equal(withheld.windowMessages, limit === "candidates" ? 7 : 3);
    f.add("clean-future", "The retry deadline is twenty seconds."); await f.flush();
    assert.equal(f.classifications.length, 1); assert.equal(f.sent.length, 1); assert.equal(f.sent[0].body.content, "The retry deadline is twenty seconds.");
  }
});

test("a wholly over-budget clean window is withheld intact rather than retried as separate publication batches", async () => {
  const f = fixture(); await f.auto();
  for (let index = 0; index < 6; index++) f.add(`over-${index}`, `The retry interval is ${index + 1} seconds.`);
  await f.flush(); await f.flush();
  assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
  const withheld = f.entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).at(-1).data;
  assert.equal(withheld.withheld, "input-budget"); assert.equal(withheld.cursor, "over-5");
  f.add("clean-future", "The retry deadline is twenty seconds."); await f.flush(); assert.equal(f.sent.length, 1);
});


test("an oversized raw message or long pending window incurs bounded source audit and no capture parser failure", async () => {
  for (const kind of ["raw-size", "message-count"]) {
    const f = fixture(); await f.auto();
    if (kind === "raw-size") f.add("huge-input", "x".repeat(250000));
    else for (let index = 0; index < 50; index++) f.add(`many-${index}`, "The retry interval is five seconds.");
    f.add("last-reply", "She needs more time to meet expectations.", "assistant");
    await f.flush();
    assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0); assert.equal(f.sharing.summary().status, "active");
    const withheld = f.entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).at(-1).data;
    assert.equal(withheld.withheld, "input-budget"); assert.equal(withheld.cursor, "last-reply"); assert(withheld.processed.length <= 5);
    assert.equal(withheld.windowMessages, kind === "raw-size" ? 2 : 51); assert(JSON.stringify(withheld).length < 6000);
    f.add("clean-future", "The retry deadline is twenty seconds."); await f.flush(); assert.equal(f.sent.length, 1);
  }
});


test("path-prefixed user text stays in privacy windows and ordinary slash-prefixed excerpts remain valid", async () => {
  const f = fixture(); await f.auto();
  f.add("sensitive-path", "/tmp/fixture HR performance review");
  f.add("sensitive-reply", "The next step needs more work.", "assistant");
  await f.flush();
  assert.equal(f.classifications.length, 0); assert.equal(f.sent.length, 0);
  const withheld = f.entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).at(-1).data;
  assert.equal(withheld.withheld, "excluded-window"); assert.deepEqual(withheld.processed.map(item => item.entryId), ["sensitive-path", "sensitive-reply"]);
  f.add("clean-path", "/tmp/fixture uses a bounded retry interval of five seconds.");
  f.add("clean-path-reply", "/tmp/cache stores the source digest.", "assistant");
  await f.flush();
  assert.equal(f.classifications.length, 1); assert.equal(f.sent.length, 2);
  assert.deepEqual(f.classifications[0].map(item => item.entryId), ["clean-path", "clean-path-reply"]);
  assert.deepEqual(f.sent.map(item => item.body.content), ["/tmp/fixture uses a bounded retry interval of five seconds.", "/tmp/cache stores the source digest."]);
});


test("unreadable or incomplete source input withholds the full window without orphaning its assistant reply", async () => {
  for (const kind of ["invalid-scalar", "invalid-date", "empty-text", "unsupported-content", "image-only", "mixed-attachment", "malformed-part"]) {
    const f = fixture(); await f.auto();
    const original = f.add("unreadable-user", "The bounded retry interval is five seconds.");
    if (kind === "invalid-scalar") original.message.content = "HR performance review \ud800";
    if (kind === "invalid-date") original.timestamp = "not-a-date";
    if (kind === "empty-text") original.message.content = "   ";
    if (kind === "unsupported-content") original.message.content = { type: "unreadable" };
    if (kind === "image-only") original.message.content = [{ type: "image", data: "fixture", mimeType: "image/png" }];
    if (kind === "mixed-attachment") original.message.content = [{ type: "text", text: "See the attached discussion." }, { type: "image", data: "fixture", mimeType: "image/png" }];
    if (kind === "malformed-part") original.message.content = [null];
    f.add("innocuous-reply", "The next step needs more work.", "assistant");
    await f.flush();
    assert.equal(f.classifications.length, 0, kind); assert.equal(f.sent.length, 0, kind); assert.equal(f.sharing.summary().status, "active", kind);
    const withheld = f.entries.filter(entry => entry.customType === SHARING_DECISION_TYPE).at(-1).data;
    assert.equal(withheld.proposals.length, 0, kind); assert.equal(withheld.cursor, "innocuous-reply", kind); assert(withheld.withheld, kind);
    f.add("clean-next-user", "The retry deadline is twenty seconds.");
    f.add("clean-next-assistant", [{ type: "thinking", thinking: "private reasoning" }, { type: "text", text: "The retry interval is five seconds." }, { type: "toolCall", id: "call", name: "fixture", arguments: {} }], "assistant");
    await f.flush();
    assert.equal(f.classifications.length, 1, kind); assert.equal(f.sent.length, 2, kind);
    assert.deepEqual(f.classifications[0].map(item => item.entryId), ["clean-next-user", "clean-next-assistant"], kind);
  }
});
