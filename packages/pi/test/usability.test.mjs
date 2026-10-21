import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, chmod, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { MemoryWorldsClient, importDirectory, planDirectory, scopeReferenceSelection } from "memory-worlds-pi/client";

const scopeId = "00000000-0000-4000-8000-000000000001", otherId = "00000000-0000-4000-8000-000000000002";
const selected = { world_id: "private", scope_id: scopeId };
const world = { id: "private", scope_id: otherId, authority_id: "local", owner_id: "alice", kind: "personal", name: "Personal", alias: "personal", created_at: "2026-10-20T12:00:00Z", projection_state: "disabled" };
const scope = { ...selected, id: scopeId, name: "Archive", alias: "notes", kind: "agent-persistent", created_at: world.created_at, projection_state: "disabled" };
const identity = { credential_id: "credential-one", user_id: "alice", agent_id: "fixture", admin: false, authority_id: "local", worlds: [world] };
const named = { id: scopeId, kind: "agent", owner_id: "alice", name: "Architect", alias: "architect", created_at: world.created_at };
const episode = { source_id: "synthetic-native-message", content: "Synthetic private import content", occurred_at: world.created_at, source_type: "claude", session_id: null, source_entry_id: null, parent_entry_id: null, role: null, source_ref: "claude:synthetic-native-message", metadata: {}, project: true, scope_id: null, context_id: null };
function plan() {
  return { episodes: [episode], report: { files: [{ path: "archive.json", format: "claude", bytes: 200, sha256: "a".repeat(64), episodes: 1 }], omitted_files: 0, scanned_files: 1, skipped_files: 0, selected_files: 1, episodes: 1, deduplicated: 0, formats: { claude: 1 }, conversations: 1, unsupported_parts: 0, references_only: 0, skipped: 0, warnings: [], payload_bytes: 500 } };
}
function client(handler, token = async () => "synthetic-fixed-credential") {
  const calls = [];
  const transport = async (url, options) => { calls.push({ url, ...options, body: options.body && JSON.parse(options.body) }); return new Response(JSON.stringify(await handler(new URL(url).pathname, options)), { status: 200 }); };
  return { api: new MemoryWorldsClient("http://127.0.0.1:8787", token, transport), calls };
}
async function plannerFixture(value, run) {
  const folder = await mkdtemp(join(tmpdir(), "memory-worlds-native-planner-"));
  const script = join(folder, "planner.mjs");
  await writeFile(script, `#!${process.execPath}\nimport assert from 'node:assert/strict';\nassert.equal(process.argv[2], 'plan-directory');\nassert(process.argv.includes('--emit-records'));\nassert(!Object.keys(process.env).some(key => /(?:MEMORY_WORLDS|API_KEY|TOKEN|SECRET|NODE_OPTIONS|PYTHONPATH)/.test(key)));\nprocess.stdout.write(${JSON.stringify(JSON.stringify(value))});\n`, { mode: 0o700 });
  await chmod(script, 0o700);
  try { return await run(script, folder); } finally { await rm(folder, { recursive: true, force: true }); }
}

test("scope references accept explicit aliases while canonical selections remain separate", () => {
  assert.deepEqual(scopeReferenceSelection({ world_id: "@personal", scope_id: "@notes" }), { world_id: "@personal", scope_id: "@notes" });
  for (const scope_id of ["@Upper", "@../secret", "not-a-uuid"]) assert.throws(() => scopeReferenceSelection({ world_id: "private", scope_id }));
});

test("world and scope aliases resolve against live visible objects with one pinned credential", async () => {
  let tokenReads = 0;
  const f = client(path => path === "/v1/worlds" ? [world] : path.endsWith("/scopes") ? [scope] : scope, async () => `synthetic-${++tokenReads}`);
  assert.deepEqual(await f.api.resolveSelection("@personal", "@notes"), selected);
  assert.equal(tokenReads, 1);
  assert(f.calls.every(call => call.headers.Authorization === "Bearer synthetic-1"));
  assert.deepEqual(f.calls.map(call => new URL(call.url).pathname), ["/v1/worlds", "/v1/worlds/private/scopes", `/v1/worlds/private/scopes/${scopeId}`]);
});

test("missing or ambiguous aliases do not select the first object", async () => {
  const missing = client(() => []);
  await assert.rejects(missing.api.resolveWorld("@hidden"), /No visible/);
  const ambiguous = client(() => [world, { ...world, id: "another" }]);
  await assert.rejects(ambiguous.api.resolveWorld("@personal"), /ambiguous/);
  assert(ambiguous.calls.every(call => call.method === "GET"));
});

test("alias SDK mutations use current PUT routes and validate returned identities", async () => {
  const f = client(path => path.includes("/scopes/") ? { ...scope, alias: null } : { ...world, alias: "renamed" });
  assert.equal((await f.api.setWorldAlias("private", "renamed")).alias, "renamed");
  assert.equal((await f.api.setScopeAlias(selected, null)).alias, null);
  assert(f.calls.every(call => call.method === "PUT"));
  await assert.rejects(f.api.setWorldAlias("private", "Upper"), /Alias/);
  const wrong = client(() => ({ ...scope, id: otherId }));
  await assert.rejects(wrong.api.setScopeAlias(selected, "notes"), /does not match/);
});

test("named identity SDK uses typed generic registry routes and linked UUID validation", async () => {
  const f = client((path, options) => path.endsWith("/agent") && options.method === "GET" ? [named] : options.method === "PATCH" ? { ...named, name: "Updated" } : named);
  assert.equal((await f.api.resolveIdentity("agent", "@architect")).id, scopeId);
  assert(f.calls[0].url.endsWith("/v1/identities/agent/resolve?reference=%40architect"));
  assert.equal((await f.api.createIdentity("agent", { name: "Architect", alias: "architect" })).id, scopeId);
  assert.equal((await f.api.updateIdentity("agent", scopeId, { name: "Updated" })).name, "Updated");
  assert.equal(f.calls.at(-1).method, "PATCH");
  assert.equal((await f.api.namedIdentity("agent", scopeId)).id, scopeId);
  const wrong = client(() => ({ ...named, parent_id: "not-a-uuid" }));
  await assert.rejects(wrong.api.namedIdentity("agent", scopeId), /linked identity/);
});

test("atomic SDK import pins one explicit destination and validates receipt counts", async () => {
  const source = { ...episode }; delete source.scope_id; delete source.context_id;
  const f = client(() => ({ imported: 1, replayed: 0 }));
  assert.deepEqual(await f.api.importEpisodes(selected, [source]), { imported: 1, replayed: 0 });
  assert.equal(source.scope_id, undefined);
  assert.equal(f.calls[0].body.episodes[0].scope_id, scopeId);
  for (const input of [{ ...source, scope_id: otherId }, { ...source, context_id: otherId }, { ...source, source_type: "publication" }]) await assert.rejects(f.api.importEpisodes(selected, [input]), /cannot select/);
  const wrong = client(() => ({ imported: 0, replayed: 0 }));
  await assert.rejects(wrong.api.importEpisodes(selected, [source]), /receipt/);
});

test("native planner uses fixed argv and a credential-free environment, preserving private records off UI", async () => {
  const canary = `MEMORY_WORLDS_SYNTHETIC_CANARY_${randomUUID().replaceAll("-", "")}`;
  process.env[canary] = "synthetic-private-value";
  try {
    await plannerFixture(plan(), async (cliPath, folder) => {
      const result = await planDirectory(join(folder, "path with spaces;$(not-a-shell)"), { cliPath, format: "claude", allBranches: true });
      assert.equal(result.episodes[0].content, episode.content);
      assert.equal(result.episodes[0].source_ref, episode.source_ref);
      assert(!JSON.stringify(result.report).includes(episode.content));
      assert.equal(result.episodes[0].scope_id, undefined);
    });
  } finally { delete process.env[canary]; }
});

test("native planner counts Unicode scalars at the Python chunk boundary and rejects malformed text", async () => {
  const unicode = plan(); unicode.episodes = [{ ...episode, content: "😀".repeat(200_000) }];
  await plannerFixture(unicode, async (cliPath, folder) => {
    assert.equal((await planDirectory(folder, { cliPath })).episodes[0].content, unicode.episodes[0].content);
  });
  for (const content of ["😀".repeat(200_001), "synthetic\ud800invalid"]) {
    const invalid = plan(); invalid.episodes = [{ ...episode, content }];
    await plannerFixture(invalid, async (cliPath, folder) => {
      await assert.rejects(planDirectory(folder, { cliPath }), /invalid directory plan/);
    });
  }
});

test("planner errors and invalid private destinations never disclose child output or publish records", async () => {
  const poisoned = plan(); poisoned.episodes[0] = { ...episode, context_id: otherId, content: "synthetic-private-canary" };
  await plannerFixture(poisoned, async (cliPath, folder) => {
    await assert.rejects(planDirectory(folder, { cliPath }), error => !error.message.includes("synthetic-private-canary") && /invalid directory plan/.test(error.message));
  });
  await plannerFixture(plan(), async (cliPath, folder) => {
    const f = client(path => path === "/v1/me" ? { ...identity, worlds: [{ ...world, owner_id: "bob" }] } : scope);
    await assert.rejects(importDirectory(f.api, selected, folder, { cliPath }), /your own private/);
    assert(!f.calls.some(call => call.url.endsWith("/imports")));
  });
});

test("native directory import submits source-only records with the selected pinned credential", async () => {
  await plannerFixture(plan(), async (cliPath, folder) => {
    const f = client(path => path === "/v1/me" ? identity : path.endsWith("/imports") ? { imported: 1, replayed: 0 } : scope);
    const result = await importDirectory(f.api, selected, folder, { cliPath, sourceOnly: true });
    assert.equal(result.imported, 1);
    const writes = f.calls.filter(call => call.url.endsWith("/imports"));
    assert.equal(writes.length, 1); assert.equal(writes[0].body.episodes[0].project, false);
    assert(writes.every(call => call.headers.Authorization === "Bearer synthetic-fixed-credential"));
  });
});

test("credential rotation between planning preflight and dispatch stops native imports", async () => {
  await plannerFixture(plan(), async (cliPath, folder) => {
    let reads = 0;
    const f = client((path, options) => path === "/v1/me" ? { ...identity, credential_id: options.headers.Authorization.endsWith("first") ? "first" : "second" } : scope, async () => ++reads === 1 ? "first" : "second");
    await assert.rejects(importDirectory(f.api, selected, folder, { cliPath }), /credential changed/);
    assert(!f.calls.some(call => call.url.endsWith("/imports")));
  });
});
