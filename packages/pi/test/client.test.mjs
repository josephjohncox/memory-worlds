import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, chmod, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MemoryWorldsClient, MemoryWorldsError, readTokenFile, serviceURL } from "memory-worlds-pi/client";
import { parseConfig } from "../dist/config.js";

const selection = { world_id: "private", scope_id: "00000000-0000-4000-8000-000000000002" };
const world = { scope_id: "00000000-0000-4000-8000-000000000001", id: "private", name: "Private", authority_id: "a", kind: "personal", owner_id: "alice" };
const reply = value => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });

test("credentials stay in the authorization header and redirects are refused", async () => {
  let request;
  const client = new MemoryWorldsClient("http://127.0.0.1:8787", async () => "secret", async (url, options) => {
    request = { url, options }; return reply([world]);
  });
  assert.equal((await client.worlds())[0].id, "private");
  assert.equal(request.options.headers.Authorization, "Bearer secret");
  assert.equal(request.options.redirect, "error");
  assert.equal(request.url, "http://127.0.0.1:8787/v1/worlds");
  assert.equal(request.options.body, undefined);
  assert.throws(() => serviceURL("http://example.com"), /HTTPS/);
  assert.throws(() => serviceURL("https://alice:secret@example.com"), /credentials/);
  assert.throws(() => serviceURL("https://example.com?token=secret"), /credentials/);
});

test("token files require private permissions and token rotation is read each time", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-token-"));
  try {
    const path = join(dir, "token");
    await writeFile(path, "first\n", { mode: 0o600 });
    assert.equal(await readTokenFile(path), "first");
    await writeFile(path, "second\n");
    assert.equal(await readTokenFile(path), "second");
    if (process.platform !== "win32") {
      await chmod(path, 0o644);
      await assert.rejects(readTokenFile(path), /private/);
    }
  } finally { await rm(dir, { recursive: true }); }
});

test("token files accept the API maximum ASCII bearer with bounded LF or CRLF endings", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-token-boundary-"));
  try {
    const path = join(dir, "token"), token = "synthetic-" + "a".repeat(16_384 - "synthetic-".length);
    for (const ending of ["", "\n", "\r\n"]) {
      await writeFile(path, token + ending, { mode: 0o600 });
      assert.equal(await readTokenFile(path), token);
    }
  } finally { await rm(dir, { recursive: true }); }
});

test("token files reject overlong, non-ASCII, empty or embedded-whitespace bearers", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-token-invalid-"));
  try {
    const path = join(dir, "token");
    for (const text of ["a".repeat(16_385), "a".repeat(16_385) + "\n", "", "\r\n", "synthetic token", "synthetic\ttoken", "synthetic\ntoken", "synthetic\rtoken", "synthetic-é", "synthetic-\u007f"]) {
      await writeFile(path, text, { mode: 0o600 });
      await assert.rejects(readTokenFile(path), /ASCII bearer token/);
    }
    await writeFile(path, "a".repeat(16_385) + "\r\n");
    await assert.rejects(readTokenFile(path), /private regular file/);
    await assert.rejects(readTokenFile(dir), /private regular file/);
  } finally { await rm(dir, { recursive: true }); }
});

test("discovery forwards a private token larger than 8 KiB unchanged only in authenticated headers", async () => {
  const dir = await mkdtemp(join(tmpdir(), "mw-token-discovery-"));
  try {
    const path = join(dir, "token"), token = "synthetic-" + "b".repeat(12_000), requests = [];
    await writeFile(path, token + "\n", { mode: 0o600 });
    const client = new MemoryWorldsClient("http://127.0.0.1:8787", () => readTokenFile(path), async (url, options) => {
      requests.push({ url, options });
      assert.equal(options.headers.Authorization, `Bearer ${token}`);
      assert.equal(options.method, "GET"); assert.equal(options.body, undefined);
      return reply(url.endsWith("/v1/worlds") ? [world] : []);
    });
    const result = await client.discover();
    assert.deepEqual(result, { worlds: [world], scopes: { private: [] }, mounts: [] });
    assert.equal(requests.length, 3);
    assert(!JSON.stringify(result).includes(token));
    assert(!JSON.stringify(result).includes("Bearer "));
    assert(requests.every(request => !request.url.includes(token)));
  } finally { await rm(dir, { recursive: true }); }
});

test("federation retains explicit scope provenance and reports partial failure", async () => {
  const seen = [];
  const client = new MemoryWorldsClient("http://localhost:8787", async () => "secret", async (_url, options) => {
    seen.push(JSON.parse(options.body));
    return reply({ results: [{ mount_id: "team", authority_id: "b", world_id: "shared", scope_id: selection.scope_id, record_type: "fact", fact: "shared fact" }], failures: [{ mount_id: "offline", error: "unavailable" }], complete: false });
  });
  const result = await client.searchMounts(["team", "offline"], "bearing", "graph", 5);
  assert.equal(result.complete, false);
  assert.deepEqual(result.results.map(hit => hit.authority_id), ["b"]);
  assert.deepEqual(result.results.map(hit => hit.scope_id), [selection.scope_id]);
  assert.equal(result.failures[0].mount_id, "offline");
  assert.deepEqual(seen[0].mount_ids, ["team", "offline"]);
});

test("an upstream failure never echoes source or credential content", async () => {
  const client = new MemoryWorldsClient("http://localhost:8787", async () => "secret", async () => new Response("secret private transcript", { status: 403 }));
  await assert.rejects(client.worlds(), error => error instanceof MemoryWorldsError && !error.message.includes("secret"));
  const result = await client.searchMounts(["team"], "words", "graph", 5);
  assert.equal(result.complete, false);
  assert.deepEqual(result.results, []);
  assert.equal(result.failures[0].mount_id, "router");
});

test("abort and response bounds apply to authenticated HTTP calls", async () => {
  let fetched = false;
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => { fetched = true; return reply([world]); });
  await assert.rejects(client.worlds(AbortSignal.abort()));
  assert.equal(fetched, false);
  const huge = new MemoryWorldsClient("http://localhost", async () => "secret", async () => new Response("x".repeat(2_097_153)));
  await assert.rejects(huge.worlds(), /2 MiB/);
});

test("search rejects unexpected remote mounts", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply({ results: [{ mount_id: "other", authority_id: "b", world_id: "s" }], failures: [], complete: true }));
  const result = await client.searchMounts(["selected"], "word", "source", 5);
  assert.equal(result.complete, false);
  assert.equal(result.results.length, 0);
});

test("source evidence cannot be mislabeled as graph retrieval", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply({ mode: "source", worlds: [world], results: [{ ...selection, content: "lexical evidence" }] }));
  await assert.rejects(client.searchScopes([selection], "word", "graph", 5), /mode mismatch/);
});

test("local results outside the explicit selected world and scope are rejected", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply({ mode: "source", worlds: [world, { ...world, id: "other" }], results: [{ ...selection, world_id: "other", content: "out-of-scope" }] }));
  await assert.rejects(client.searchScopes([selection], "word", "source", 5), /unselected scope/);
});

test("configuration defaults to no capture/retrieval and rejects plaintext credentials", () => {
  const config = parseConfig({}, {}, "/users/alice");
  assert.equal(config.automaticCapture, false);
  assert.equal(config.retrieval.enabled, false);
  assert.equal(config.tokenFile, "/users/alice/.config/memory-worlds/pi.token");
  assert.equal(parseConfig({ automaticCapture: true }, {}, "/users/alice").automaticCapture, false);
  assert.equal(parseConfig({ automaticCapture: true, writeScope: selection }, {}, "/users/alice").automaticCapture, true);
  assert.throws(() => parseConfig({ token: "secret" }, {}), /Unknown/);
  assert.throws(() => parseConfig({ retrieval: { limit: 21 } }, {}), /Retrieval/);
  assert.throws(() => parseConfig({ retrieval: { enabled: "yes" } }, {}), /boolean/);
  assert.throws(() => parseConfig({ readMountIds: ["../../escape"] }, {}), /identifiers/);
});

test("a credential session snapshots rotation and erases its token after completion", async () => {
  let token = "first", reads = 0, retained;
  const authorizations = [];
  const client = new MemoryWorldsClient("http://localhost", async () => { reads++; return token; }, async (_url, options) => { authorizations.push(options.headers.Authorization); return reply([world]); });
  await client.withCredentialSession(async pinned => { retained = pinned; await pinned.worlds(); token = "second"; await pinned.worlds(); });
  assert.deepEqual(authorizations, ["Bearer first", "Bearer first"]); assert.equal(reads, 1);
  await assert.rejects(retained.worlds(), /session has ended/);
});


test("world discovery requires an actual scope UUID", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply([{ ...world, scope_id: "invalid" }]));
  await assert.rejects(client.worlds(), /Invalid scope/);
});
