import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, chmod, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { MemoryWorldsClient, MemoryWorldsError, readTokenFile, serviceURL } from "../dist/client.js";
import { parseConfig } from "../dist/config.js";

const world = { id: "private", name: "Private", authority_id: "a", kind: "personal", owner_id: "alice" };
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

test("federation retains authority provenance and reports partial failure without fallback", async () => {
  const seen = [];
  const client = new MemoryWorldsClient("http://localhost:8787", async () => "secret", async (url, options) => {
    seen.push(JSON.parse(options.body));
    return url.endsWith("/router/search")
      ? reply({ results: [{ mount_id: "team", authority_id: "b", world_id: "shared", record_type: "fact", fact: "shared fact" }], failures: [{ mount_id: "offline", error: "unavailable" }], complete: false })
      : reply({ mode: "graph", worlds: [world], results: [{ world_id: "private", fact_id: "entry", fact: "private evidence" }] });
  });
  const result = await client.search(["private"], ["team", "offline"], "bearing", "graph", 5);
  assert.equal(result.complete, false);
  assert.deepEqual(result.results.map(hit => hit.authority_id), ["a", "b"]);
  assert.deepEqual(result.results.map(hit => hit.record_type), ["fact", "fact"]);
  assert.equal(result.failures[0].mount_id, "offline");
  assert.deepEqual(seen.map(item => item.mode), ["graph", "graph"]);
});

test("an upstream failure never echoes source or credential content", async () => {
  const client = new MemoryWorldsClient("http://localhost:8787", async () => "secret", async () => new Response("secret private transcript", { status: 403 }));
  await assert.rejects(client.worlds(), error => error instanceof MemoryWorldsError && !error.message.includes("secret"));
  const result = await client.search(["private"], [], "words", "graph", 5);
  assert.equal(result.complete, false);
  assert.deepEqual(result.results, []);
  assert.equal(result.failures[0].mount_id, "local");
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
  const result = await client.search([], ["selected"], "word", "source", 5);
  assert.equal(result.complete, false);
  assert.equal(result.results.length, 0);
});

test("source fallback cannot be mislabeled as graph retrieval", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply({ mode: "source", worlds: [world], results: [{ world_id: "private", content: "lexical evidence" }] }));
  const result = await client.search(["private"], [], "word", "graph", 5);
  assert.equal(result.complete, false);
  assert.deepEqual(result.results, []);
});

test("local results outside the explicit selected worlds are rejected", async () => {
  const client = new MemoryWorldsClient("http://localhost", async () => "secret", async () => reply({ mode: "source", worlds: [world, { ...world, id: "other" }], results: [{ world_id: "other", content: "out-of-scope" }] }));
  const result = await client.search(["private"], [], "word", "source", 5);
  assert.equal(result.complete, false);
  assert.deepEqual(result.results, []);
});

test("configuration defaults to no capture/retrieval and rejects plaintext credentials", () => {
  const config = parseConfig({}, {}, "/users/alice");
  assert.equal(config.automaticCapture, false);
  assert.equal(config.retrieval.enabled, false);
  assert.equal(config.tokenFile, "/users/alice/.config/memory-worlds/pi.token");
  assert.equal(parseConfig({ automaticCapture: true }, {}, "/users/alice").automaticCapture, false);
  assert.equal(parseConfig({ automaticCapture: true, writeWorldId: "private" }, {}, "/users/alice").automaticCapture, true);
  assert.throws(() => parseConfig({ token: "secret" }, {}), /Unknown/);
  assert.throws(() => parseConfig({ retrieval: { limit: 21 } }, {}), /Retrieval/);
  assert.throws(() => parseConfig({ retrieval: { enabled: "yes" } }, {}), /boolean/);
  assert.throws(() => parseConfig({ readWorldIds: ["../../escape"] }, {}), /identifiers/);
});
