import assert from "node:assert/strict";
import { appendFile, chmod, mkdir, mkdtemp, open, readFile, readdir, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { once } from "node:events";
import { createHash } from "node:crypto";
import { execFile as execFileCallback, execFileSync } from "node:child_process";
import { promisify } from "node:util";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createMemoryWorldsPlugin } from "../dist/factory.js";
import { boundedRead, parseConfig } from "../dist/config.js";

const scopeId = "dd820226-39e7-47cd-afbc-565c4109dfc0";
const otherId = "78183979-947a-42a3-8d4d-07eaf7935810";
const selected = { world_id: "world-private", scope_id: scopeId };
const identity = { admin: false, credential_id: "agent-token", user_id: "owner", agent_id: "opencode", authority_id: "local-authority" };
const world = { id: selected.world_id, scope_id: scopeId, authority_id: "local-authority", name: "Private", kind: "personal" };
const worldScope = { id: scopeId, world_id: selected.world_id, name: "OpenCode", kind: "agent-persistent" };
const token = "synthetic-test-token";
const args = { text: "A selected technical finding.", sourceId: "finding-42", occurredAt: "2026-10-21T12:00:00Z" };
const context = () => ({ sessionID: "session-one", messageID: "message-one", agent: "build", directory: "/unused", worktree: "/unused", abort: new AbortController().signal, metadata() {}, async ask() {} });
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

async function fixture(t, handler, profile = {}) {
  const directory = await realpath(await mkdtemp(join(tmpdir(), "memory-worlds-opencode-")));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const tokenFile = join(directory, "agent.token"), configPath = join(directory, "profile.json"), intentDirectory = join(directory, "intents");
  await writeFile(tokenFile, token, { mode: 0o600 });
  const config = { url: "http://127.0.0.1:8787", tokenFile, readScopes: [selected], writeScope: selected, ...profile };
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  const calls = [];
  const transport = async (url, options) => {
    const call = { path: new URL(url).pathname, options, body: options.body === undefined ? undefined : JSON.parse(options.body) };
    calls.push(call);
    assert.equal(options.redirect, "error");
    assert.equal(options.headers.Authorization, `Bearer ${token}`);
    assert.ok(options.signal instanceof AbortSignal);
    if (call.path === "/v1/me") return json(identity);
    return handler(call, { tokenFile });
  };
  const hooks = await createMemoryWorldsPlugin({ configPath, transport, intentDirectory })({});
  assert.deepEqual(Object.keys(hooks), ["tool"]);
  return { tools: hooks.tool, calls, tokenFile, configPath, config, intentDirectory, transport };
}

test("the package entry exports one SDK plugin and the factory stays separate", async () => {
  const entry = await import("../dist/index.js");
  assert.deepEqual(Object.keys(entry), ["MemoryWorldsPlugin"]);
  assert.equal(typeof entry.MemoryWorldsPlugin, "function");
  const manifest = JSON.parse(await readFile(new URL("../package.json", import.meta.url)));
  assert.equal(manifest.dependencies["@opencode-ai/plugin"], "1.19.3");
  assert.ok(!manifest.peerDependencies);
  assert.ok(!JSON.stringify(manifest).includes("pi-coding-agent"));
});

test("discovery selects visible metadata without changing configured search scopes", async t => {
  const f = await fixture(t, async ({ path, body }) => {
    if (path === "/v1/worlds") return json([{ ...world, token: "not-metadata", content: "not-metadata" }]);
    if (path === "/v1/mounts") return json([{ id: "corporate", name: "Work", authority_id: "corporate", world_ids: ["company"], can_publish: false, token: "not-metadata" }]);
    if (path.endsWith("/scopes")) return json([{ ...worldScope, content: "not-metadata" }]);
    if (path === "/v1/search") {
      assert.deepEqual(body.scopes, [selected]);
      return json({ mode: body.mode, worlds: [world], results: [{ ...selected, id: "episode", content: "Evidence." }] });
    }
    assert.fail(path);
  });
  const found = JSON.parse(await f.tools.memory_discover.execute({ includeScopes: true }, context()));
  assert.equal(found.worlds[0].name, "Private");
  assert.equal(found.scopes[selected.world_id][0].kind, "agent-persistent");
  assert.equal(found.mounts[0].can_publish, false);
  assert.ok(!JSON.stringify(found).includes("not-metadata"));
  const result = JSON.parse(await f.tools.memory_search.execute({ query: "Evidence", mode: "source" }, context()));
  assert.equal(result.results[0].authority_id, "local-authority");
  assert.equal(result.results[0].content, "Evidence.");
});

test("graph recall uses the fixed selection and default retrieval settings", async t => {
  const f = await fixture(t, ({ body }) => {
    assert.deepEqual(body, { scopes: [selected], query: "Procedure", mode: "graph", limit: 5, include_entities: false });
    return json({ mode: "graph", worlds: [world], results: [{ ...selected, fact: "Verified fact", fact_id: "fact-one" }] });
  });
  const result = JSON.parse(await f.tools.memory_search.execute({ query: "Procedure" }, context()));
  assert.equal(result.results[0].record_type, "fact");
});

test("a search result from an unselected scope fails closed", async t => {
  const f = await fixture(t, () => json({ mode: "source", worlds: [world], results: [{ world_id: selected.world_id, scope_id: otherId, content: "Unexpected private evidence" }] }));
  await assert.rejects(f.tools.memory_search.execute({ query: "Evidence", mode: "source" }, context()), /unselected scope/);
});

for (const kind of ["agent-persistent", "thread-private", "invocation-private"]) test(`explicit capture accepts ${kind} and adds SDK attribution`, async t => {
  const f = await fixture(t, ({ path, body }) => {
    if (!path.endsWith("/episodes")) return json({ ...worldScope, kind });
    assert.equal(path, `/v1/worlds/${selected.world_id}/scopes/${scopeId}/episodes`);
    assert.deepEqual(body, {
      source_id: args.sourceId, content: args.text, occurred_at: args.occurredAt,
      source_type: "text", session_id: "session-one", source_entry_id: "message-one",
      metadata: { harness: "opencode", opencode_session_id: "session-one", opencode_message_id: "message-one", opencode_agent: "build" },
      project: true, scope_id: scopeId,
    });
    return json({ episode: { id: "episode-one", ...selected, source_id: args.sourceId, content: args.text, metadata: body.metadata }, replayed: false });
  });
  const result = JSON.parse(await f.tools.memory_capture_private.execute(args, context()));
  assert.equal(result.episode.scope_id, scopeId);
  assert.equal(result.episode.id, "episode-one");
  assert.ok(!("content" in result.episode));
  assert.ok(!("metadata" in result.episode));
  assert.equal(f.calls.filter(call => call.options.method === "POST").length, 1);
});

for (const kind of ["world-shared", "run-shared"]) test(`private capture rejects ${kind} before a write`, async t => {
  const f = await fixture(t, () => json({ ...worldScope, kind }));
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /agent-persistent, thread-private or invocation-private/);
  assert.ok(f.calls.every(call => call.options.method === "GET"));
});

test("tools cannot select a destination or silently expand recall", async t => {
  const f = await fixture(t, () => assert.fail("No service operation expected"));
  await assert.rejects(f.tools.memory_capture_private.execute({ ...args, writeScope: { world_id: "company", scope_id: otherId } }, context()), /Invalid Memory Worlds tool arguments/);
  await assert.rejects(f.tools.memory_search.execute({ query: "Secret", world_ids: ["company"] }, context()), /Invalid Memory Worlds tool arguments/);
  assert.deepEqual(f.calls, []);
});

test("status omits the token and private file paths and performs no writes", async t => {
  const f = await fixture(t, () => assert.fail("Only identity expected"));
  const status = JSON.parse(await f.tools.memory_status.execute({}, context()));
  assert.deepEqual(status.readScopes, [selected]);
  assert.deepEqual(status.writeScope, selected);
  assert.equal(status.automaticCapture, false);
  assert.equal(status.automaticSharing, false);
  assert.equal(f.calls.length, 1);
  assert.ok(!JSON.stringify(status).includes(token));
  assert.ok(!JSON.stringify(status).includes(f.tokenFile));
});

test("missing read and write selections do not authorize an operation", async t => {
  const f = await fixture(t, () => assert.fail("No data endpoint expected"), { readScopes: [], writeScope: undefined });
  await assert.rejects(f.tools.memory_search.execute({ query: "Secret" }, context()), /Configure readScopes/);
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /Configure a private writeScope/);
  assert.ok(f.calls.every(call => call.path === "/v1/me"));
});

test("changed private scope metadata and mismatched capture receipts fail closed", async t => {
  let changedScope = true;
  const f = await fixture(t, ({ path }) => path.endsWith("/episodes")
    ? json({ episode: { ...selected, scope_id: otherId, id: "wrong", source_id: args.sourceId }, replayed: false })
    : json({ ...worldScope, ...(changedScope ? { id: otherId } : {}) }));
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /operation failed/);
  assert.ok(f.calls.every(call => call.options.method === "GET"));
  changedScope = false;
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /operation failed/);
});

test("administrator credentials are rejected before discovery or capture", async t => {
  const f = await fixture(t, () => assert.fail("No data endpoint expected"));
  const plugin = createMemoryWorldsPlugin({ configPath: f.configPath, transport: async () => json({ ...identity, admin: true }) });
  const tools = (await plugin({})).tool;
  await assert.rejects(tools.memory_discover.execute({}, context()), /administrator credentials are rejected/);
  await assert.rejects(tools.memory_capture_private.execute(args, context()), /administrator credentials are rejected/);
});

test("private regular credential files are required, including after loading", async t => {
  const f = await fixture(t, () => assert.fail("No transport expected"));
  await chmod(f.tokenFile, 0o644);
  await assert.rejects(f.tools.memory_status.execute({}, context()), /operation failed/);
  await chmod(f.tokenFile, 0o600);
  const target = join(f.tokenFile + "-link");
  await symlink(f.tokenFile, target);
  await writeFile(f.configPath, JSON.stringify({ ...f.config, tokenFile: target }));
  const tools = (await createMemoryWorldsPlugin({ configPath: f.configPath, transport: async () => assert.fail("No transport expected") })({})).tool;
  await assert.rejects(tools.memory_status.execute({}, context()), /operation failed/);
});

test("credentials rotate between operations and stay pinned within one operation", async t => {
  const f = await fixture(t, () => assert.fail("Unused fixture transport"));
  const seen = [];
  const transport = async (url, options) => {
    seen.push(options.headers.Authorization);
    if (new URL(url).pathname === "/v1/me") {
      await writeFile(f.tokenFile, "rotated-synthetic-token", { mode: 0o600 });
      return json(identity);
    }
    if (new URL(url).pathname === "/v1/worlds") return json([world]);
    return json([]);
  };
  const tools = (await createMemoryWorldsPlugin({ configPath: f.configPath, transport })({})).tool;
  await tools.memory_discover.execute({}, context());
  assert.deepEqual(seen, Array(3).fill(`Bearer ${token}`));
  await tools.memory_status.execute({}, context());
  assert.equal(seen.at(-1), "Bearer rotated-synthetic-token");
});

test("HTTP, transport, malformed-body and oversized-body errors never expose private diagnostics", async t => {
  const f = await fixture(t, () => assert.fail("Unused fixture transport"));
  for (const response of [
    () => new Response(`private ${token} ${f.tokenFile}`, { status: 403 }),
    () => { throw new Error(`private ${token} ${f.tokenFile}`); },
    () => new Response(`invalid-json ${token} ${f.tokenFile}`),
    () => new Response("x".repeat(2_097_153)),
  ]) {
    const tools = (await createMemoryWorldsPlugin({ configPath: f.configPath, transport: async () => response() })({})).tool;
    await assert.rejects(tools.memory_status.execute({}, context()), error => {
      assert.ok(!error.message.includes(token));
      assert.ok(!error.message.includes(f.tokenFile));
      assert.equal(error.cause, undefined);
      return true;
    });
  }
});

test("an aborted operation performs no network request", async t => {
  const f = await fixture(t, () => assert.fail("No transport expected"));
  const invocation = context(), controller = new AbortController();
  controller.abort(new Error(`private ${token}`));
  invocation.abort = controller.signal;
  await assert.rejects(f.tools.memory_status.execute({}, invocation), /operation was cancelled/);
  assert.deepEqual(f.calls, []);
});

test("profiles reject credentials inline, aliases, insecure remotes and ambiguous selections", () => {
  const base = { url: "http://127.0.0.1:8787", tokenFile: "/private/agent.token", readScopes: [selected] };
  for (const changes of [
    { token: "inline-secret" }, { automaticCapture: true }, { url: "http://memory.example.org" },
    { url: "http://127.attacker.example.org" },
    { url: "https://user:secret@memory.example.org" }, { url: "https://memory.example.org/path" },
    { tokenFile: "relative.token" }, { readScopes: [selected, selected] },
    { readScopes: [{ world_id: "@team", scope_id: scopeId }] },
    { writeScope: { world_id: "company", scope_id: "@shared" } },
    { readScopes: [{ ...selected, operations: ["write"] }] },
  ]) assert.throws(() => parseConfig({ ...base, ...changes }));
  assert.equal(parseConfig({ ...base, url: "https://memory.example.org" }).url, "https://memory.example.org");
});

test("the real SDK plugin factory calls the HTTP contract through built-in fetch", async t => {
  const paths = [];
  const server = createServer(async (request, response) => {
    paths.push(request.url);
    assert.equal(request.headers.authorization, `Bearer ${token}`);
    response.setHeader("Content-Type", "application/json");
    if (request.url === "/v1/me") return response.end(JSON.stringify(identity));
    if (request.method === "GET") return response.end(JSON.stringify(worldScope));
    const parts = [];
    for await (const part of request) parts.push(part);
    const body = JSON.parse(Buffer.concat(parts).toString("utf8"));
    assert.equal(body.content, args.text);
    assert.equal(body.metadata.opencode_session_id, "session-one");
    response.end(JSON.stringify({ episode: { id: "http-episode", ...selected, source_id: args.sourceId }, replayed: false }));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(async () => { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const f = await fixture(t, () => assert.fail("Use built-in fetch"), { url: `http://127.0.0.1:${server.address().port}` });
  const tools = (await createMemoryWorldsPlugin({ configPath: f.configPath, intentDirectory: f.intentDirectory })({})).tool;
  const receipt = JSON.parse(await tools.memory_capture_private.execute(args, context()));
  assert.equal(receipt.episode.id, "http-episode");
  assert.deepEqual(paths, ["/v1/me", `/v1/worlds/${selected.world_id}/scopes/${scopeId}`, `/v1/worlds/${selected.world_id}/scopes/${scopeId}/episodes`]);
});

test("a lost capture acknowledgment reuses the durable original body after restart and changed SDK context", async t => {
  const posts = [];
  const f = await fixture(t, async ({ path, options, body }) => {
    if (!path.endsWith("/episodes")) return json(worldScope);
    const names = await readdir(f.intentDirectory);
    assert.equal(names.length, 1, "The intent must exist before the first external POST");
    const persisted = JSON.parse(await readFile(join(f.intentDirectory, names[0]), "utf8"));
    assert.deepEqual(persisted.request, body);
    assert.equal((await stat(f.intentDirectory)).mode & 0o777, 0o700);
    assert.equal((await stat(join(f.intentDirectory, names[0]))).mode & 0o777, 0o600);
    assert.ok(!JSON.stringify(persisted).includes(token));
    posts.push(options.body);
    if (posts.length === 1) throw new Error("Lost response after server committed the episode");
    return json({ episode: { id: "durable-episode", ...selected, source_id: args.sourceId }, replayed: true });
  });
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /operation failed/);
  const restarted = (await createMemoryWorldsPlugin({ configPath: f.configPath, transport: f.transport, intentDirectory: f.intentDirectory })({})).tool;
  const resumed = { ...context(), sessionID: "new-session", messageID: "new-message", agent: "new-agent" };
  const receipt = JSON.parse(await restarted.memory_capture_private.execute(args, resumed));
  assert.equal(receipt.replayed, true);
  assert.equal(posts.length, 2);
  assert.equal(posts[0], posts[1]);
  assert.equal(JSON.parse(posts[1]).metadata.opencode_session_id, "session-one");
  await assert.rejects(restarted.memory_capture_private.execute({ ...args, text: "Different finding" }, resumed), /sourceId already records different/);
  await assert.rejects(restarted.memory_capture_private.execute({ ...args, occurredAt: "2026-10-21T13:00:00Z" }, resumed), /sourceId already records different/);
  assert.equal(posts.length, 2, "Conflicting arguments must fail before POST");
});

test("concurrent explicit captures select one complete immutable intent", async t => {
  const posts = [];
  const f = await fixture(t, ({ path, options }) => {
    if (!path.endsWith("/episodes")) return json(worldScope);
    posts.push(options.body);
    return json({ episode: { id: "concurrent-episode", ...selected, source_id: args.sourceId }, replayed: posts.length > 1 });
  });
  await Promise.all([
    f.tools.memory_capture_private.execute(args, context()),
    f.tools.memory_capture_private.execute(args, { ...context(), messageID: "concurrent-message" }),
  ]);
  assert.equal(posts.length, 2);
  assert.equal(posts[0], posts[1]);
  assert.equal((await readdir(f.intentDirectory)).length, 1);
});

test("concurrent conflicting arguments cannot create two external writes", async t => {
  const posts = [];
  const f = await fixture(t, ({ path, options }) => {
    if (!path.endsWith("/episodes")) return json(worldScope);
    posts.push(options.body);
    return json({ episode: { id: "winning-episode", ...selected, source_id: args.sourceId }, replayed: false });
  });
  const results = await Promise.allSettled([
    f.tools.memory_capture_private.execute(args, context()),
    f.tools.memory_capture_private.execute({ ...args, text: "Conflicting technical finding." }, context()),
  ]);
  assert.equal(results.filter(result => result.status === "fulfilled").length, 1);
  assert.equal(results.filter(result => result.status === "rejected" && /sourceId already records different/.test(result.reason.message)).length, 1);
  assert.equal(posts.length, 1);
});

test("a journal key binds the configured service, credential, scope and source", async t => {
  const f = await fixture(t, () => assert.fail("Use the keyed transport"));
  let credentialId = identity.credential_id;
  const bodies = [];
  const transport = async (url, options) => {
    if (new URL(url).pathname === "/v1/me") return json({ ...identity, credential_id: credentialId });
    if (options.method === "GET") return json(worldScope);
    bodies.push(JSON.parse(options.body));
    return json({ episode: { id: "keyed-episode", ...selected, source_id: args.sourceId }, replayed: false });
  };
  const tools = (await createMemoryWorldsPlugin({ configPath: f.configPath, transport, intentDirectory: f.intentDirectory })({})).tool;
  await tools.memory_capture_private.execute(args, context());
  credentialId = "another-credential";
  await tools.memory_capture_private.execute(args, { ...context(), messageID: "another-message" });
  const key = { url: f.config.url, credential_id: identity.credential_id, ...selected, source_id: args.sourceId };
  const filename = createHash("sha256").update(JSON.stringify(key)).digest("hex") + ".json";
  assert.ok((await readdir(f.intentDirectory)).includes(filename));
  assert.equal((await readdir(f.intentDirectory)).length, 2);
  assert.notEqual(bodies[0].source_entry_id, bodies[1].source_entry_id);
});

test("unsafe journal permissions, symlinks and damaged records fail without uploading", async t => {
  const f = await fixture(t, ({ path }) => {
    if (path.endsWith("/episodes")) assert.fail("No POST expected");
    return json(worldScope);
  });
  await mkdir(f.intentDirectory, { mode: 0o755 });
  await chmod(f.intentDirectory, 0o755);
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /private capture intent journal/);
  await chmod(f.intentDirectory, 0o700);
  const key = { url: f.config.url, credential_id: identity.credential_id, ...selected, source_id: args.sourceId };
  const filename = createHash("sha256").update(JSON.stringify(key)).digest("hex") + ".json";
  await writeFile(join(f.intentDirectory, filename), "invalid private record", { mode: 0o600 });
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /private capture intent journal/);
  await writeFile(join(f.intentDirectory, filename), Buffer.alloc(1_048_577, "x"));
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /private capture intent journal/);
  await rm(join(f.intentDirectory, filename));
  await symlink(f.tokenFile, join(f.intentDirectory, filename));
  await assert.rejects(f.tools.memory_capture_private.execute(args, context()), /private capture intent journal/);
  assert.ok(f.calls.every(call => call.options.method === "GET"));
});

test("profile permissions and bounded descriptor reads protect configuration and growing files", async t => {
  const f = await fixture(t, () => assert.fail("No transport expected"));
  await chmod(f.configPath, 0o666);
  await assert.rejects(createMemoryWorldsPlugin({ configPath: f.configPath })({}), /Cannot load OpenCode memory profile/);
  await chmod(f.configPath, 0o644);
  await createMemoryWorldsPlugin({ configPath: f.configPath })({});
  const path = join(f.intentDirectory + "-growing");
  await writeFile(path, "small", { mode: 0o600 });
  const handle = await open(path, "r");
  try {
    assert.ok((await handle.stat()).size < 64);
    await appendFile(path, Buffer.alloc(1_048_576, "x"));
    await assert.rejects(boundedRead(handle, 64), /size limit/);
  } finally { await handle.close(); }
});

test("FIFO token and profile targets fail promptly instead of blocking before validation", async t => {
  const f = await fixture(t, () => assert.fail("No transport expected"));
  const fifo = join(f.intentDirectory + "-fifo");
  execFileSync("mkfifo", ["-m", "600", fifo]);
  const script = `
    import { readToken, loadConfig } from ${JSON.stringify(new URL("../dist/config.js", import.meta.url).href)};
    const action = process.argv[1] === "token" ? readToken : loadConfig;
    try { await action(process.argv[2]); process.exitCode = 1; }
    catch { process.exitCode = 0; }
  `;
  for (const kind of ["token", "profile"]) {
    await promisify(execFileCallback)(process.execPath, ["--input-type=module", "-e", script, kind, fifo], { timeout: 2000 });
  }
});
