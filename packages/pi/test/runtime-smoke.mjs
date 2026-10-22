// Exercise the published entry point in the actual Pi 1.3.0 RPC process, without model credentials.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { once } from "node:events";
import { mkdtemp, writeFile, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dir = await mkdtemp(join(tmpdir(), "memory-worlds-pi-smoke-"));
const token = "smoke-fixture-agent-token";
const captured = [];
const published = [];
const requests = [];
const privateScope = "00000000-0000-4000-8000-000000000002", contextId = "00000000-0000-4000-8000-000000000003";
const selected = { world_id: "private", scope_id: privateScope };
const world = { scope_id: "00000000-0000-4000-8000-000000000001", id: "private", name: "Private", authority_id: "fixture-authority", kind: "personal", owner_id: "alice", created_at: "2026-10-20T00:00:00Z", projection_state: "disabled" };
const daemon = createServer(async (request, response) => {
  assert.equal(request.headers.authorization, `Bearer ${token}`);
  requests.push(request.url);
  let body = "";
  for await (const chunk of request) body += chunk;
  response.setHeader("Content-Type", "application/json");
  if (request.url === "/v1/worlds") response.end(JSON.stringify([world]));
  else if (request.url === "/v1/me") response.end(JSON.stringify({ credential_id: "fixture", user_id: "alice", agent_id: "pi", authority_id: world.authority_id, worlds: [world] }));
  else if (request.url === `/v1/worlds/private/scopes/${privateScope}`) response.end(JSON.stringify({ ...selected, id: privateScope, name: "Invocation", kind: "invocation-private", created_at: world.created_at, projection_state: "disabled" }));
  else if (request.url === "/v1/contexts") response.end(JSON.stringify({ ...JSON.parse(body), id: contextId, credential_id: "fixture", invocation_id: contextId, created_at: world.created_at }));
  else if (request.url === "/v1/router/deliveries") response.end("[]");
  else if (request.url === "/v1/mounts") response.end(JSON.stringify([{ id: "team", name: "Team", url: "https://example.test", authority_id: "team-authority", world_ids: ["shared"], can_publish: true }]));
  else if (request.url === "/v1/scopes/flow") response.end(JSON.stringify({ allowed: true }));
  else if (request.url === "/v1/search") response.end(JSON.stringify({ mode: "source", worlds: [world], results: [{ world_id: "private", scope_id: privateScope, id: "fixture-source", content: "fixture evidence" }] }));
  else if (request.url === "/v1/router/publications") {
    published.push(JSON.parse(body));
    response.statusCode = 202;
    response.end(JSON.stringify({ id: "fixture-delivery", state: "pending" }));
  }
  else if (request.url === `/v1/worlds/private/scopes/${privateScope}/episodes`) {
    captured.push(JSON.parse(body));
    response.end(JSON.stringify({ episode: { ...captured.at(-1), ...selected, id: "fixture-episode" }, replayed: false }));
  } else { response.statusCode = 404; response.end("{}"); }
});
let child;
try {
  daemon.listen(0, "127.0.0.1");
  await once(daemon, "listening");
  const tokenPath = join(dir, "agent.token");
  await writeFile(tokenPath, token, { mode: 0o600 });
  const configPath = join(dir, "memory-worlds.json");
  await writeFile(configPath, JSON.stringify({ url: `http://127.0.0.1:${daemon.address().port}`, tokenFile: tokenPath, writeScope: selected, readScopes: [selected] }));
  await mkdir(join(dir, "agent"));
  child = spawn(process.execPath, [join(root, "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"), "--mode", "rpc", "--no-session", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--extension", root], {
    cwd: dir,
    env: { ...process.env, PI_CODING_AGENT_DIR: join(dir, "agent"), MEMORY_WORLDS_CONFIG: configPath, MEMORY_WORLDS_URL: `http://127.0.0.1:${daemon.address().port}`, MEMORY_WORLDS_TOKEN_FILE: tokenPath },
    stdio: ["pipe", "pipe", "pipe"],
  });
  let output = "", stderr = "", sequence = 0;
  const messages = [];
  const waiters = new Map();
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", chunk => { stderr += chunk; });
  child.stdout.on("data", chunk => {
    output += chunk;
    let newline;
    while ((newline = output.indexOf("\n")) !== -1) {
      const line = output.slice(0, newline); output = output.slice(newline + 1);
      if (!line.trim()) continue;
      const message = JSON.parse(line); messages.push(message);
      if (message.id && message.type === "response") waiters.get(message.id)?.(message);
    }
  });
  const command = input => new Promise((resolve, reject) => {
    const id = `smoke-${++sequence}`;
    const timer = setTimeout(() => reject(new Error(`Pi RPC timeout: ${stderr}`)), 15000);
    waiters.set(id, response => { clearTimeout(timer); waiters.delete(id); resolve(response); });
    child.stdin.write(JSON.stringify({ id, ...input }) + "\n");
  });
  const commands = await command({ type: "get_commands" });
  assert.equal(commands.success, true, JSON.stringify(commands));
  assert(commands.data.commands.some(command => command.name === "memory"), "Native /memory command must load in Pi");
  assert(messages.some(message => message.type === "extension_ui_request" && message.method === "setStatus" && message.statusKey === "memory-worlds" && message.statusText?.includes("memory: online")), "Memory indicator must use Pi's bottom status bar");
  assert(!messages.some(message => message.type === "extension_ui_request" && message.method === "setWidget" && message.widgetLines?.length), "Memory must not render a widget above the input");
  assert.equal((await command({ type: "prompt", message: "/memory status" })).success, true);
  assert.equal((await command({ type: "prompt", message: "/memory capture native-runtime-fixture" })).success, true);
  assert.equal(captured.length, 1);
  assert.equal(captured[0].content, "native-runtime-fixture");
  assert.equal(captured[0].project, true);
  assert.equal((await command({ type: "prompt", message: "/memory source fixture" })).success, true);
  assert.equal((await command({ type: "prompt", message: "/memory publish team shared selected-runtime-publication" })).success, true);
  assert.equal((await command({ type: "prompt", message: "/memory publish team shared selected-runtime-publication" })).success, true);
  assert.equal(published.length, 2);
  assert.deepEqual(published[0], published[1]);
  assert.equal(published[0].content, "selected-runtime-publication");
  const entries = await command({ type: "get_entries" });
  assert.equal(entries.success, true);
  assert(!JSON.stringify(entries).includes(token));
  assert(requests.includes("/v1/worlds"));
  assert(!messages.some(message => message.type === "extension_error"), JSON.stringify(messages));
  assert(!stderr.includes("Failed to load extension"), stderr);
  assert(!JSON.stringify(messages).includes(token), "Credential must not appear in Pi session/RPC events");
  child.kill("SIGTERM");
  await once(child, "exit"); child = undefined;
  console.log("Pi 1.3.0 runtime smoke passed: package loading, commands, bottom status bar, authenticated capture/search/publication, immutable publication retry, session metadata, shutdown.");
} finally {
  child?.kill("SIGKILL");
  daemon.close();
  await rm(dir, { recursive: true, force: true });
}
