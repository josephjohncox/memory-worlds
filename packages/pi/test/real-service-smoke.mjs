// Actual Pi 1.3.0 process -> actual Python HTTP API -> durable SQLite source storage.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, writeFile, mkdir, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { MemoryWorldsClient } from "memory-worlds-pi/client";
import { withSubagent } from "memory-worlds-pi/orchestrator";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const python = resolve(process.env.MEMORY_WORLDS_SMOKE_PYTHON ?? join(root, "../../.venv/bin/python"));
const dir = await mkdtemp(join(tmpdir(), "memory-worlds-pi-service-"));
const tokenPath = join(dir, "agent.token");
const serviceCode = `
import json, os, socket, sys
from pathlib import Path
import uvicorn
from memory_worlds.api import create_app
from memory_worlds.models import CreateToken, Role, CreateScope, ScopeKind
from uuid import uuid4
from memory_worlds.mounts import MountConfig
from memory_worlds.store import MemoryStore
folder = Path(sys.argv[1])
store = MemoryStore(folder / "data")
world, operator_token = store.bootstrap("fixture")
operator = store.authenticate(operator_token)
token = store.issue_token(operator, CreateToken(user_id="fixture", agent_id="pi", grants={world.id: Role.WRITER}))
shared_scope = next(scope for scope in store.list_scopes(operator, world.id) if scope.kind == ScopeKind.WORLD_SHARED)
run_scope = store.create_scope(store.authenticate(token), world.id, CreateScope(kind=ScopeKind.RUN_SHARED, name="Pi run"))
agent_scope = store.create_scope(store.authenticate(token), world.id, CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Pi specialist", agent_id=uuid4()))
fd = os.open(folder / "agent.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(token)
app = create_app(folder / "data", mount_config=MountConfig())
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen(128)
print(json.dumps({"port": sock.getsockname()[1], "world": world.model_dump(mode="json"), "scope": agent_scope.model_dump(mode="json"), "shared_scope_id": str(shared_scope.id), "run_scope_id": str(run_scope.id)}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])
`;
const fixtureEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("MEMORY_WORLDS_")));
let service, pi;
try {
  service = spawn(python, ["-c", serviceCode, dir], { cwd: resolve(root, "../.."), env: fixtureEnv, stdio: ["ignore", "pipe", "pipe"] });
  let serviceOutput = "", serviceError = "";
  service.stderr.setEncoding("utf8");
  service.stderr.on("data", chunk => { serviceError += chunk; });
  const metadata = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Python service startup timeout: ${serviceError}`)), 15000);
    service.once("error", error => { clearTimeout(timeout); reject(error); });
    service.once("exit", code => { clearTimeout(timeout); reject(new Error(`Python service exited ${code}: ${serviceError}`)); });
    service.stdout.setEncoding("utf8");
    service.stdout.on("data", chunk => {
      serviceOutput += chunk;
      const newline = serviceOutput.indexOf("\n");
      if (newline !== -1) { clearTimeout(timeout); resolve(JSON.parse(serviceOutput.slice(0, newline))); }
    });
  });
  const url = `http://127.0.0.1:${metadata.port}`;
  const token = (await readFile(tokenPath, "utf8")).trim();
  const authHeaders = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  for (let attempt = 0; ; attempt++) {
    try { if ((await fetch(`${url}/readyz`)).ok) break; } catch {}
    if (attempt > 100) throw new Error(`Python API did not become ready: ${serviceError}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal((await fetch(`${url}/v1/me`)).status, 401);
  const identity = await (await fetch(`${url}/v1/me`, { headers: authHeaders })).json();
  assert.equal(identity.agent_id, "pi");
  assert.equal(identity.admin, false);
  assert.equal(identity.authority_id, metadata.world.authority_id);
  const configPath = join(dir, "memory-worlds.json");
  const selected = { world_id: metadata.world.id, scope_id: metadata.scope.id };
  await writeFile(configPath, JSON.stringify({ url, tokenFile: tokenPath, writeScope: selected, readScopes: [selected], graphAliases: { project: selected, global: { world_id: metadata.world.id, scope_id: metadata.shared_scope_id } }, spoolDirectory: join(dir, "private-spool") }));
  await mkdir(join(dir, "agent"));
  pi = spawn(process.execPath, [join(root, "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"), "--mode", "rpc", "--no-session", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--extension", root], {
    cwd: dir, env: { ...fixtureEnv, PI_CODING_AGENT_DIR: join(dir, "agent"), MEMORY_WORLDS_CONFIG: configPath, MEMORY_WORLDS_TOKEN_FILE: tokenPath, MEMORY_WORLDS_URL: url }, stdio: ["pipe", "pipe", "pipe"],
  });
  let output = "", errors = "", sequence = 0;
  const messages = [], waiters = new Map();
  pi.stderr.setEncoding("utf8"); pi.stderr.on("data", chunk => { errors += chunk; });
  pi.stdout.setEncoding("utf8");
  pi.stdout.on("data", chunk => {
    output += chunk;
    let newline;
    while ((newline = output.indexOf("\n")) !== -1) {
      const line = output.slice(0, newline); output = output.slice(newline + 1);
      if (!line.trim()) continue;
      const message = JSON.parse(line); messages.push(message);
      if (message.type === "response" && message.id) waiters.get(message.id)?.(message);
    }
  });
  const command = input => new Promise((resolve, reject) => {
    const id = `service-${++sequence}`;
    const timeout = setTimeout(() => reject(new Error(`Pi RPC timeout: ${errors}`)), 15000);
    waiters.set(id, message => { clearTimeout(timeout); waiters.delete(id); resolve(message); });
    pi.stdin.write(JSON.stringify({ id, ...input }) + "\n");
  });
  const commands = await command({ type: "get_commands" });
  assert(commands.data.commands.some(command => command.name === "memory"));
  assert(commands.data.commands.some(command => command.name === "graph"));
  for (const text of ["/memory status", "/memory capture real-service-bearing-evidence", "/memory search real-service-bearing-evidence"]) {
    assert.equal((await command({ type: "prompt", message: text })).success, true);
  }
  const searchResponse = await fetch(`${url}/v1/search`, { method: "POST", headers: authHeaders, body: JSON.stringify({ scopes: [selected], query: "real-service-bearing-evidence", mode: "source", limit: 5 }) });
  assert.equal(searchResponse.status, 200);
  const search = await searchResponse.json();
  assert.equal(search.mode, "source");
  assert.equal(search.results.length, 1);
  assert.equal(search.results[0].content, "real-service-bearing-evidence");
  assert.equal(search.results[0].source_type, "pi");
  assert.equal(search.results[0].scope_id, selected.scope_id);
  assert.match(search.results[0].invocation_id, /^[0-9a-f-]{36}$/);
  assert.match(search.results[0].agent_uuid, /^[0-9a-f-]{36}$/);
  assert.equal(search.results[0].actor_id, "fixture");
  assert.equal(search.results[0].agent_id, "pi");
  assert.equal(search.results[0].projection_state, "disabled");
  for (const text of ["/graph add selected-graphiti-evidence", "/graph episodes"]) {
    assert.equal((await command({ type: "prompt", message: text })).success, true);
  }
  const client = new MemoryWorldsClient(url, async () => token);
  const graphSource = (await client.searchScopes([selected], "selected-graphiti-evidence", "source", 5)).results;
  assert.equal(graphSource.length, 1); assert.equal(graphSource[0].scope_id, selected.scope_id);
  const globalScope = { world_id: metadata.world.id, scope_id: metadata.shared_scope_id };
  assert.match(globalScope.scope_id, /^[0-9a-f-]{36}$/);
  const sharedIsolation = await client.searchScopes([globalScope], "real-service-bearing-evidence", "source", 5);
  assert.equal(sharedIsolation.results.length, 0, "World-shared memory must not expose private notes");
  assert.equal((await command({ type: "prompt", message: `/memory share ${metadata.world.id} ${globalScope.scope_id} selected-world-shared-finding` })).success, true);
  const sharedSources = await client.searchScopes([globalScope], "selected-world-shared-finding", "source", 5);
  assert.equal(sharedSources.results.length, 1); assert.equal(sharedSources.results[0].scope_id, globalScope.scope_id);
  const sharedEpisodes = await client.episodes(globalScope, 5);
  assert.equal(sharedEpisodes.length, 1); assert.equal(sharedEpisodes[0].content, "selected-world-shared-finding");
  for (const text of ["/graph global", "/graph episodes", "/graph project"]) assert.equal((await command({ type: "prompt", message: text })).success, true);
  const runScope = { world_id: metadata.world.id, scope_id: metadata.run_scope_id };
  await client.publishScope(runScope, { source_id: "fixture-run-brief", occurred_at: new Date().toISOString(), content: "shared-run-brief-for-private-working-notes", project: false });
  assert.equal(await client.canFlow([runScope], selected), true);
  for (const text of [`/memory read ${metadata.world.id} ${runScope.scope_id}`, "/memory capture on", "/memory search shared-run-brief-for-private-working-notes", "/memory status"]) assert.equal((await command({ type: "prompt", message: text })).success, true);
  const lastStatus = messages.filter(message => message.type === "extension_ui_request" && message.method === "notify" && message.message?.startsWith("{\n") && message.message.includes('"automaticCapture"')).at(-1);
  assert.equal(JSON.parse(lastStatus.message).automaticCapture, true, "Live run-shared readers must flow into the narrower private capture target");
  assert.equal((await command({ type: "prompt", message: "/memory capture private-note-after-shared-run-read" })).success, true);
  const privateRunNote = await client.searchScopes([selected], "private-note-after-shared-run-read", "source", 5);
  assert.equal(privateRunNote.results.length, 1); assert.equal(privateRunNote.results[0].scope_id, selected.scope_id);
  let childClient, childContext;
  const child = await withSubagent(client, { task: "Review the explicitly selected specialist note", privateWorldId: metadata.world.id, grants: [{ ...selected, operations: ["read"] }], evidence: search.results.map(hit => ({ ...hit, authority_id: metadata.world.authority_id, record_type: "source" })) }, async runtime => {
    childClient = runtime.client; childContext = runtime.context;
    await runtime.client.captureScope({ world_id: runtime.privateScope.world_id, scope_id: runtime.privateScope.id }, { source_id: "child-private-scratch", content: "unreturned private scratch", occurred_at: new Date().toISOString(), context_id: runtime.context.id });
    return "selected child finding";
  });
  assert.equal(child.result, "selected child finding"); assert.match(childContext.invocation_id, /^[0-9a-f-]{36}$/);
  await assert.rejects(client.episodes({ world_id: child.privateScope.world_id, scope_id: child.privateScope.id }), error => error.status === 403);
  await assert.rejects(childClient.identity(), /runtime has ended/);
  assert(!JSON.stringify(child).includes("unreturned private scratch"));
  const entries = await command({ type: "get_entries" });
  assert.equal(entries.success, true);
  assert(!JSON.stringify(entries).includes(token));
  assert(messages.some(message => message.type === "extension_ui_request" && message.method === "notify" && message.message?.includes("real-service-bearing-evidence")), "Pi must render the source evidence returned by the actual API");
  assert(!messages.some(message => message.type === "extension_error"), JSON.stringify(messages));
  assert(!errors.includes("Failed to load extension"), errors);
  pi.kill("SIGTERM"); await once(pi, "exit"); pi = undefined;
  service.kill("SIGTERM"); await once(service, "exit"); service = undefined;
  console.log("Real-service Pi smoke passed: actual Pi -> authenticated scoped Python API -> SQLite, bound UUID attribution, private native graph capture/episodes, world-shared exclusion, delegated child scratch isolation, selected result, credential revocation, provenance and credential exclusion.");
} finally {
  pi?.kill("SIGKILL"); service?.kill("SIGKILL");
  await rm(dir, { recursive: true, force: true });
}
