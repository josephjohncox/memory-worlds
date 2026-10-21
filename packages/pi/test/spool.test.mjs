import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, stat, chmod, rm, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { MemorySpool } from "memory-worlds-pi/spool";

const binding = { serviceUrl: "http://localhost:8787", authorityId: "authority", credentialId: "agent-credential" };
const target = { world_id: "personal", scope_id: "00000000-0000-4000-8000-000000000001" };
const episode = { source_id: "stable-session-entry", content: "private selected note", occurred_at: "2026-10-20T00:00:00Z", source_type: "pi", project: false };
function client(capture = async (_selected, source) => ({ episode: { ...source, ...target, id: "episode" }, replayed: false })) {
  const instance = { url: binding.serviceUrl, withCredentialSession: async work => work(instance), identity: async () => ({ authority_id: binding.authorityId, credential_id: binding.credentialId }), captureScope: capture };
  return instance;
}
async function fixture(fn) { const folder = await mkdtemp(join(tmpdir(), "mw-spool-test-")); try { await fn(new MemorySpool(join(folder, "private")), folder); } finally { await rm(folder, { recursive: true, force: true }); } }

test("offline envelopes survive a new spool instance with mode 0600 and immutable source identity", async () => fixture(async (spool, folder) => {
  await spool.enqueue(binding, target, episode); await spool.enqueue(binding, target, episode);
  const files = await readdir(spool.directory); assert.equal(files.length, 1);
  assert.equal((await stat(join(spool.directory, files[0]))).mode & 0o777, 0o600);
  assert.equal((await stat(spool.directory)).mode & 0o777, 0o700);
  const body = await readFile(join(spool.directory, files[0]), "utf8");
  assert(!body.includes("Bearer")); assert(!body.includes("tokenFile"));
  await assert.rejects(spool.enqueue(binding, target, { ...episode, content: "changed content under same id" }), /conflicts/);
  const reopened = new MemorySpool(join(folder, "private"));
  const result = await reopened.drain(client(), binding);
  assert.equal(result.delivered.length, 1); assert.equal((await reopened.status()).pending, 0);
}));

test("lost capture acknowledgments retain exact scope and source for replay", async () => fixture(async spool => {
  await spool.enqueue(binding, target, episode);
  const attempts = [];
  await assert.rejects(spool.drain(client(async (selected, source) => { attempts.push({ selected, source }); throw new Error("lost acknowledgment"); }), binding), /lost acknowledgment/);
  assert.equal((await spool.status()).pending, 1);
  await spool.drain(client(async (selected, source) => { attempts.push({ selected, source }); return { episode: { ...source, ...selected, id: "replayed" }, replayed: true }; }), binding);
  assert.deepEqual(attempts[0], attempts[1]); assert.equal((await spool.status()).pending, 0);
}));

test("credential rotation and authority substitution cannot dispatch queued private sources", async () => fixture(async spool => {
  await spool.enqueue(binding, target, episode); let captures = 0;
  const swapped = { ...client(async () => { captures++; }), identity: async () => ({ authority_id: "other-authority", credential_id: binding.credentialId }) };
  swapped.withCredentialSession = async work => work(swapped);
  await assert.rejects(spool.drain(swapped, binding), /credential or authority changed/);
  const rotated = { ...binding, credentialId: "new-credential" };
  const fresh = { ...client(async () => { captures++; }), identity: async () => ({ authority_id: binding.authorityId, credential_id: "new-credential" }) };
  fresh.withCredentialSession = async work => work(fresh);
  const result = await spool.drain(fresh, rotated);
  assert.equal(result.blocked, 1); assert.equal(captures, 0); assert.equal((await spool.status()).pending, 1);
}));

test("mismatched receipts preserve queued data and unsafe files fail closed", async () => fixture(async (spool, folder) => {
  await spool.enqueue(binding, target, episode);
  await assert.rejects(spool.drain(client(async (_selected, source) => ({ episode: { ...source, ...target, scope_id: "wrong", id: "wrong" } })), binding), /does not match/);
  assert.equal((await spool.status()).pending, 1);
  const name = (await readdir(spool.directory))[0]; await chmod(join(spool.directory, name), 0o644);
  await assert.rejects(spool.drain(client(), binding), /private regular file/);
  const path = join(folder, "unsafe"); await writeFile(path, "{}");
  const link = join(folder, "link"); await symlink(spool.directory, link);
  await assert.rejects(new MemorySpool(link).status(), /must be private/);
}));

test("spool capacity rejects new sources while identical retries remain valid and the queue stays drainable", async () => fixture(async (_spool, folder) => {
  const spool = new MemorySpool(join(folder, "limited"), 1);
  await spool.enqueue(binding, target, episode);
  await assert.rejects(spool.enqueue(binding, target, { ...episode, source_id: "new-source" }), /capacity/);
  await spool.enqueue(binding, target, episode);
  assert.equal((await spool.drain(client(), binding)).delivered.length, 1);
  await spool.enqueue(binding, target, { ...episode, source_id: "new-source" });
  assert.equal((await spool.status()).pending, 1);
}));

test("a changed source receipt cannot delete the immutable envelope", async () => fixture(async spool => {
  await spool.enqueue(binding, target, { ...episode, metadata: { classification: "private" } });
  await assert.rejects(spool.drain(client(async (_selected, source) => ({ episode: { ...source, ...target, content: "changed evidence", id: "wrong" } })), binding), /does not match/);
  assert.equal((await spool.status()).pending, 1);
}));


test("queued automatic capture rechecks the live audience before dispatch", async () => fixture(async spool => {
  const flow = { read_scopes: [target], write_scope: target };
  await spool.enqueue(binding, target, episode, flow);
  let captures = 0;
  const guarded = client(async (_selected, source) => { captures++; return { episode: { ...source, ...target, id: "saved" } }; });
  guarded.canFlow = async () => false;
  await assert.rejects(spool.drain(guarded, binding), /audience no longer permits/);
  assert.equal(captures, 0); assert.equal((await spool.status()).pending, 1);
  guarded.canFlow = async () => true;
  assert.equal((await spool.drain(guarded, binding)).delivered.length, 1);
}));


test("a token rotation between identity and dispatch cannot redirect queued sources", async () => fixture(async spool => {
  const { MemoryWorldsClient } = await import("memory-worlds-pi/client");
  await spool.enqueue(binding, target, episode); let token = "first", reads = 0;
  const authorizations = [];
  const actual = new MemoryWorldsClient(binding.serviceUrl, async () => { reads++; return token; }, async (url, options) => {
    authorizations.push(options.headers.Authorization);
    if (url.endsWith("/v1/me")) { token = "rotated"; return new Response(JSON.stringify({ authority_id: binding.authorityId, credential_id: binding.credentialId, user_id: "alice", agent_id: "agent", worlds: [] })); }
    const body = JSON.parse(options.body);
    return new Response(JSON.stringify({ episode: { ...body, ...target, id: "saved" }, replayed: true }));
  });
  assert.equal((await spool.drain(actual, binding)).delivered.length, 1);
  assert.equal(reads, 1); assert(authorizations.every(value => value === "Bearer first"));
}));
