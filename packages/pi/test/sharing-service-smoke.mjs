// Actual Pi RPC, native provider authentication/streaming, and the authenticated Python API.
// All model responses are synthetic; no model endpoint or paid request is used.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { MemoryWorldsClient } from "memory-worlds-pi/client";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const python = resolve(process.env.MEMORY_WORLDS_SMOKE_PYTHON ?? join(root, "../../.venv/bin/python"));
const directory = await mkdtemp(join(tmpdir(), "memory-worlds-sharing-service-"));
const agentDirectory = join(directory, "agent"), tokenPath = join(directory, "agent.token");
const sessionDirectory = join(directory, "sessions");
const modelLog = join(directory, "model-calls.jsonl"), publicationLog = join(directory, "publications.jsonl");
const scenarioFile = join(directory, "scenario.json"), dropAcknowledgment = join(directory, "drop-ack");
const provider = "sharing-smoke", classifierModel = "haiku-synthetic";
const providerCredential = "synthetic-isolated-pi-credential";
const fixtureEnvironment = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith("MEMORY_WORLDS_")));
let service, pi;

const serviceCode = `
import json, os, socket, sys
from pathlib import Path
from uuid import uuid4
import uvicorn
from memory_worlds.api import create_app
from memory_worlds.models import CreateScope, CreateToken, CreateWorld, Role, ScopeKind, WorldKind
from memory_worlds.mounts import MountConfig
from memory_worlds.store import MemoryStore
folder = Path(sys.argv[1])
store = MemoryStore(folder / "data")
personal, operator_token = store.bootstrap("fixture")
operator = store.authenticate(operator_token)
team = store.create_world(operator, CreateWorld(name="Synthetic team space", kind=WorldKind.TEAM))
token = store.issue_token(operator, CreateToken(user_id="fixture", agent_id="pi-sharing", grants={personal.id: Role.WRITER, team.id: Role.WRITER}))
agent = store.authenticate(token)
private = store.create_scope(agent, personal.id, CreateScope(kind=ScopeKind.AGENT_PERSISTENT, name="Synthetic private chat", agent_id=uuid4()))
fd = os.open(folder / "agent.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    stream.write(token)
app = create_app(folder / "data", mount_config=MountConfig())
@app.middleware("http")
async def inspect_publications(request, call_next):
    if request.method == "POST" and request.url.path.endswith("/publications"):
        body = json.loads(await request.body())
        with (folder / "publications.jsonl").open("a") as stream:
            stream.write(json.dumps({"path": request.url.path, "body": body}) + "\\n")
    return await call_next(request)
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
sock.listen(128)
print(json.dumps({"port": sock.getsockname()[1], "personal": personal.model_dump(mode="json"), "team": team.model_dump(mode="json"), "private": private.model_dump(mode="json"), "credential_id": agent.credential_id, "operator_token": operator_token}), flush=True)
uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).run(sockets=[sock])
`;

async function eventually(probe, description, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (true) {
    const result = await probe();
    if (result) return result;
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for ${description}`);
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}
async function records(path) {
  return (await readFile(path, "utf8")).split("\n").filter(Boolean).map(line => JSON.parse(line));
}
async function terminate(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([once(child, "exit"), new Promise(resolve => setTimeout(resolve, 3000).unref())]);
  if (child.exitCode === null && child.signalCode === null) { child.kill("SIGKILL"); await once(child, "exit"); }
}

try {
  await mkdir(agentDirectory, { mode: 0o700 });
  await mkdir(sessionDirectory, { mode: 0o700 });
  await writeFile(modelLog, "", { mode: 0o600 });
  await writeFile(publicationLog, "", { mode: 0o600 });
  await writeFile(join(agentDirectory, "auth.json"), JSON.stringify({ [provider]: { type: "api_key", key: providerCredential } }), { mode: 0o600 });
  service = spawn(python, ["-c", serviceCode, directory], { cwd: resolve(root, "../.."), env: fixtureEnvironment, stdio: ["ignore", "pipe", "pipe"] });
  let serviceOutput = "", serviceError = "";
  service.stderr.setEncoding("utf8"); service.stderr.on("data", chunk => { serviceError += chunk; });
  const metadata = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Python sharing fixture startup timeout: ${serviceError}`)), 15000);
    service.once("error", error => { clearTimeout(timer); reject(error); });
    service.once("exit", code => { clearTimeout(timer); reject(new Error(`Python sharing fixture exited ${code}: ${serviceError}`)); });
    service.stdout.setEncoding("utf8"); service.stdout.on("data", chunk => {
      serviceOutput += chunk;
      const newline = serviceOutput.indexOf("\n");
      if (newline !== -1) { clearTimeout(timer); resolve(JSON.parse(serviceOutput.slice(0, newline))); }
    });
  });
  const url = `http://127.0.0.1:${metadata.port}`;
  const memoryCredential = (await readFile(tokenPath, "utf8")).trim();
  assert.notEqual(memoryCredential, providerCredential);
  await eventually(async () => { try { return (await fetch(`${url}/readyz`)).ok; } catch { return false; } }, "Python API readiness");
  const source = { world_id: metadata.personal.id, scope_id: metadata.private.id };
  const destination = { world_id: metadata.team.id, scope_id: metadata.team.scope_id };
  const operator = new MemoryWorldsClient(url, async () => metadata.operator_token);
  const destinationEpisodes = () => operator.episodes(destination, 100);
  const configFile = join(directory, "memory-worlds.json");
  await writeFile(configFile, JSON.stringify({
    url, tokenFile: tokenPath, writeScope: source, readScopes: [source], projectCaptured: false,
    sharing: { classifier: { provider, model: classifierModel }, excludedTopics: ["acquisition planning"], minConfidence: 0.95 },
  }), { mode: 0o600 });

  // This complete native provider uses Pi's isolated stored credential and public streaming API.
  // A fetch wrapper injects one lost acknowledgment after a real accepted HTTP publication.
  const providerFile = join(directory, "synthetic-provider.mjs");
  const aiModule = import.meta.resolve("@earendil-works/pi-ai");
  await writeFile(providerFile, `
import assert from "node:assert/strict";
import { appendFileSync, existsSync, readFileSync, unlinkSync } from "node:fs";
import { fauxProvider, fauxAssistantMessage, getCurrentTools } from ${JSON.stringify(aiModule)};
const scenarioFile = ${JSON.stringify(scenarioFile)}, modelLog = ${JSON.stringify(modelLog)};
const memoryCredential = ${JSON.stringify(memoryCredential)}, operatorCredential = ${JSON.stringify(metadata.operator_token)};
const modelCredential = ${JSON.stringify(providerCredential)}, classifierModel = ${JSON.stringify(classifierModel)};
const originalFetch = globalThis.fetch;
globalThis.fetch = async (...arguments_) => {
  const response = await originalFetch(...arguments_);
  const address = String(arguments_[0]);
  if (address.startsWith(${JSON.stringify(url)}) && address.endsWith("/publications") && response.ok && existsSync(${JSON.stringify(dropAcknowledgment)})) {
    unlinkSync(${JSON.stringify(dropAcknowledgment)});
    await response.body?.cancel();
    throw new TypeError("Synthetic lost publication acknowledgment");
  }
  return response;
};
export default function(pi) {
  const fixture = fauxProvider({ provider: ${JSON.stringify(provider)}, models: [
    { id: "synthetic-main", name: "Synthetic chat model", input: ["text"] },
    { id: classifierModel, name: "Synthetic physical Haiku fixture", input: ["text"] },
  ] });
  const respond = (context, options, _state, model) => {
    assert(options.apiKey === modelCredential, "Model request must use its isolated Pi credential");
    assert(!JSON.stringify(context).includes(memoryCredential), "Memory credential reached model context");
    assert(!JSON.stringify(context).includes(operatorCredential), "Operator credential reached model context");
    const scenario = JSON.parse(readFileSync(scenarioFile, "utf8"));
    const tools = getCurrentTools(context.messages);
    if (model.id !== classifierModel) {
      assert(!tools.some(tool => /sharing|publish/.test(tool.name)), "An assistant tool must not enable chat sharing");
      appendFileSync(modelLog, JSON.stringify({ kind: "main", toolNames: tools.map(tool => tool.name) }) + "\\n");
      return fauxAssistantMessage(scenario.reply);
    }
    assert.equal(tools.length, 0, "Sharing classifier must receive no tools");
    const lastUser = context.messages.filter(message => message.role === "user").at(-1);
    assert.equal(typeof lastUser.content, "string");
    const payload = JSON.parse(lastUser.content);
    assert(Array.isArray(payload.entries));
    appendFileSync(modelLog, JSON.stringify({ kind: "classifier", toolCount: tools.length, entries: payload.entries }) + "\\n");
    const selected = payload.entries.find(entry => entry.role === "assistant" && entry.text === scenario.reply);
    return fauxAssistantMessage(JSON.stringify(selected ? [{
      entryId: selected.entryId, excerpt: scenario.excerpt ?? scenario.reply,
      category: scenario.category ?? "technical-finding", visibility: scenario.visibility ?? "shareable", confidence: scenario.confidence ?? 0.99,
    }] : []));
  };
  fixture.setResponses(Array.from({ length: 200 }, () => respond));
  pi.registerProvider({ ...fixture.provider, auth: { apiKey: {
    name: "Isolated synthetic Pi credential",
    check: async ({ credential }) => credential?.key === modelCredential ? { type: "api_key", source: "isolated-fixture" } : undefined,
    resolve: async ({ credential }) => {
      assert(credential?.key === modelCredential, "Native provider must resolve its stored Pi credential");
      return { auth: { apiKey: credential.key }, source: "isolated-fixture" };
    },
  } } });
}
`, { mode: 0o600 });

  let output = "", errors = "", sequence = 0;
  const messages = [], waiters = new Map();
  const launchPi = sessionFile => {
    output = "";
    const child = spawn(process.execPath, [join(root, "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"), "--mode", "rpc", "--session-dir", sessionDirectory, ...(sessionFile ? ["--session", sessionFile] : []), "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--provider", provider, "--model", "synthetic-main", "--extension", providerFile, "--extension", root], {
      cwd: directory, env: { ...fixtureEnvironment, PI_CODING_AGENT_DIR: agentDirectory, MEMORY_WORLDS_CONFIG: configFile, MEMORY_WORLDS_TOKEN_FILE: tokenPath, MEMORY_WORLDS_URL: url }, stdio: ["pipe", "pipe", "pipe"],
    });
    child.stderr.setEncoding("utf8"); child.stderr.on("data", chunk => { errors += chunk; });
    child.stdout.setEncoding("utf8"); child.stdout.on("data", chunk => {
      output += chunk;
      let newline;
      while ((newline = output.indexOf("\n")) !== -1) {
        const line = output.slice(0, newline); output = output.slice(newline + 1);
        if (!line.trim()) continue;
        const message = JSON.parse(line); messages.push(message);
        if (message.type === "response" && message.id) waiters.get(message.id)?.(message);
      }
    });
    return child;
  };
  pi = launchPi();
  const command = payload => new Promise((resolve, reject) => {
    const id = `sharing-${++sequence}`;
    const timer = setTimeout(() => { waiters.delete(id); reject(new Error(`Pi sharing RPC timeout: ${errors}`)); }, 15000);
    waiters.set(id, message => { clearTimeout(timer); waiters.delete(id); resolve(message); });
    pi.stdin.write(JSON.stringify({ id, ...payload }) + "\n");
  });
  const slash = async text => { const result = await command({ type: "prompt", message: text }); assert.equal(result.success, true, result.error); return result; };
  const sharingStatus = async () => {
    const after = messages.length;
    await slash("/memory sharing status");
    const notification = messages.slice(after).find(message => message.type === "extension_ui_request" && message.method === "notify" && message.message.startsWith("{\n"));
    assert(notification, "Sharing status must render structured private status");
    return JSON.parse(notification.message);
  };
  const proposal = async excerpt => {
    await slash("/memory sharing pending");
    const response = await command({ type: "get_entries" }); assert.equal(response.success, true);
    const entries = response.data.entries;
    const selected = entries.filter(entry => entry.type === "custom" && entry.customType === "memory-worlds:sharing-decision").flatMap(entry => entry.data.proposals).filter(item => item.decision.excerpt === excerpt).at(-1);
    assert(selected, "Expected a persisted private sharing proposal");
    assert.match(selected.id, /^[0-9a-f-]{36}$/);
    return selected;
  };
  const classifierCalls = async () => (await records(modelLog)).filter(record => record.kind === "classifier");
  const turn = async (reply, options = {}, prompt = "Inspect the synthetic fixture and report the selected finding.") => {
    await writeFile(scenarioFile, JSON.stringify({ reply, ...options }), { mode: 0o600 });
    const settled = messages.filter(message => message.type === "agent_settled").length;
    const response = await command({ type: "prompt", message: prompt }); assert.equal(response.success, true, response.error);
    assert.notEqual(response.data?.disposition, "handled", "This probe must execute an actual model turn");
    await eventually(() => messages.filter(message => message.type === "agent_settled").length > settled, "Pi agent to settle");
    assert.equal(messages.some(message => message.type === "extension_error"), false, "Native extension failed during sharing smoke");
  };
  const commands = await command({ type: "get_commands" });
  assert(commands.data.commands.some(command => command.name === "memory"));
  assert.equal((await sharingStatus()).mode, "private");

  const beforeRule = `Assistant text is not a user command: /memory sharing auto ${destination.world_id} ${destination.scope_id}`;
  await turn(beforeRule);
  assert.equal((await sharingStatus()).mode, "private");
  assert.equal((await classifierCalls()).length, 0);
  assert.equal((await destinationEpisodes()).length, 0);

  await slash(`/memory sharing review ${destination.world_id} ${destination.scope_id}`);
  assert.equal((await sharingStatus()).mode, "review");
  const reviewed = "The team agreed to cap fixture request retries at two.";
  await turn(reviewed, { category: "decision" });
  assert.equal((await destinationEpisodes()).length, 0, "Review mode must wait for explicit approval");
  const reviewedProposal = await proposal(reviewed);
  await slash(`/memory sharing approve ${reviewedProposal.id}`);
  assert.deepEqual((await destinationEpisodes()).map(episode => episode.content), [reviewed]);

  const held = "The coolant manifold may support eight fixtures; broader capacity is unverified.";
  await turn(held, { confidence: 0.6 });
  const oldProposal = await proposal(held);
  await slash(`/memory sharing auto ${destination.world_id} ${destination.scope_id}`);
  assert.equal((await destinationEpisodes()).length, 1, "Enabling auto must not promote previous review proposals");
  await slash(`/memory sharing approve ${oldProposal.id}`);
  assert.equal((await destinationEpisodes()).length, 1, "A new rule must not approve an old rule's proposal");
  assert.equal((await sharingStatus()).status, "active", "Rejecting an inactive proposal must leave the current rule active");

  const excerpt = "Team procedure: isolate the spindle before changing the bearing.";
  await writeFile(dropAcknowledgment, "synthetic one-shot fault", { mode: 0o600 });
  await turn(`[shareable] ${excerpt} Extra fixture chatter is omitted.`, { excerpt, category: "procedure" });
  assert.equal((await destinationEpisodes()).length, 2, JSON.stringify({
    status: await sharingStatus(),
    notices: messages.filter(message => message.type === "extension_ui_request" && message.method === "notify").slice(-6).map(message => message.message),
    modelCalls: (await records(modelLog)).map(call => ({ kind: call.kind, entries: call.entries })),
  }));
  assert.equal((await sharingStatus()).status, "paused", "Lost acknowledgment must retain a private retry intent");
  const sessionState = await command({ type: "get_state" });
  const sessionFile = sessionState.data.sessionFile;
  assert.equal(typeof sessionFile, "string");
  assert(sessionFile.startsWith(sessionDirectory), "Session evidence must stay in the owned private directory");
  const callsBeforeResume = (await classifierCalls()).length;
  pi.stdin.end();
  await eventually(() => pi.exitCode !== null || pi.signalCode !== null, "graceful Pi shutdown");
  assert.equal(pi.exitCode, 0);
  pi = undefined;
  const persistedSession = await readFile(sessionFile, "utf8");
  assert(persistedSession.split("\n").filter(Boolean).map(line => JSON.parse(line)).some(entry => entry.type === "custom" && entry.customType === "memory-worlds:sharing-intent"), "The immutable retry intent must reach the actual Pi session file");
  assert(!persistedSession.includes(memoryCredential));
  assert(!persistedSession.includes(providerCredential));
  pi = launchPi(sessionFile);
  assert.equal((await command({ type: "get_commands" })).success, true);
  const resumed = await sharingStatus();
  assert.equal(resumed.mode, "auto", "A graceful restart must retain a valid policy for the same chat");
  assert.equal(resumed.status, "paused", "Unacknowledged writes require explicit retry after restart");
  assert.equal((await classifierCalls()).length, callsBeforeResume, "Resuming a saved intent must not reclassify source text");
  await slash("/memory sharing retry");
  assert.equal((await sharingStatus()).status, "active");
  assert.equal((await destinationEpisodes()).length, 2, "A lost acknowledgment retry must replay the accepted publication");
  const duplicateWrites = (await records(publicationLog)).filter(item => item.body.content === excerpt);
  assert.equal(duplicateWrites.length, 2);
  assert.deepEqual(duplicateWrites[0], duplicateWrites[1], "Retries must preserve destination, ID, time, text, and projection request");

  for (const excluded of [
    "[shareable] HR compensation and salary planning remain unresolved.",
    "[shareable] Quarterly rewards and bonus decisions remain unresolved.",
    "[shareable] Alice is brilliant.", "[shareable] Alice is incompetent.",
    "[shareable] The project is wonderful.", "[shareable] The project is terrible.",
    "[shareable] Acquisition planning adopted a new fixture label.",
    "[shareable] Off the record: replace the fixture label.",
  ]) {
    const before = (await classifierCalls()).length;
    await turn(excluded, { category: "explicit-share", confidence: 1 }, excluded);
    assert.equal((await classifierCalls()).length, before, "Excluded user and assistant text must be filtered before model submission");
    assert.equal((await destinationEpisodes()).length, 2, "A shareable marker cannot override exclusions");
  }

  for (const sensitivePrompt of ["HR performance review", "/tmp/fixture HR performance review", "HR performance review \ud800"]) {
    const beforeSensitiveTurn = (await classifierCalls()).length;
    await turn("The next step needs more work.", { confidence: 1 }, sensitivePrompt);
    assert.equal((await classifierCalls()).length, beforeSensitiveTurn, "An innocent-looking assistant reply to an excluded user prompt must remain private with its whole turn, including path-prefixed or malformed Unicode prompts");
    assert.equal((await destinationEpisodes()).length, 2);
  }
  const cleanAfterSensitive = "The fixture procedure uses a dedicated alignment pin.";
  await turn(cleanAfterSensitive, { category: "procedure" });
  assert.equal((await destinationEpisodes()).length, 3, "A new clean user turn must continue the active auto rule");

  await slash("/memory sharing private");
  const privateCalls = (await classifierCalls()).length;
  const privateReply = "The fixture uses a labeled coolant return line.";
  await turn(privateReply);
  assert.equal((await classifierCalls()).length, privateCalls);
  assert.equal((await destinationEpisodes()).length, 3);
  await slash(`/memory sharing auto ${destination.world_id} ${destination.scope_id}`);
  const future = "The team procedure requires a hard stop before cleaning the fixture.";
  await turn(future, { category: "procedure" });
  const published = await destinationEpisodes();
  assert.equal(published.length, 4);
  assert(published.some(episode => episode.content === future));
  assert(!published.some(episode => [beforeRule, held, privateReply].includes(episode.content)), "Rule activation must classify only future text");

  await slash(`/memory sharing review ${destination.world_id} ${destination.scope_id}`);
  const rejected = "The fixture uses revision three of the adapter.";
  await turn(rejected);
  const rejectedProposal = await proposal(rejected);
  const revoked = await fetch(`${url}/v1/credentials/${metadata.credential_id}`, { method: "DELETE", headers: { Authorization: `Bearer ${metadata.operator_token}` } });
  assert.equal(revoked.status, 204);
  await slash(`/memory sharing approve ${rejectedProposal.id}`);
  assert.equal((await sharingStatus()).mode, "private", "Revocation must stop the active rule");
  assert.equal((await destinationEpisodes()).length, 4);

  const calls = await classifierCalls();
  assert(calls.length >= 6, "The actual Pi model registry must execute classifier requests");
  assert(calls.every(call => call.toolCount === 0));
  assert(calls.every(call => !JSON.stringify(call.entries).includes(beforeRule)), "Pre-rule history must never enter classifier requests");
  for (const request of await records(publicationLog)) {
    assert.deepEqual(Object.keys(request.body).sort(), ["content", "occurred_at", "project", "source_id"]);
    assert.equal(request.body.project, true);
    assert(!JSON.stringify(request.body).includes(source.scope_id), "A publication must omit private source pointers");
  }
  for (const episode of await destinationEpisodes()) {
    assert.equal(episode.source_type, "publication");
    assert.equal(episode.project, true);
    assert.equal(episode.context_id == null, true);
    assert.equal(episode.session_id == null, true);
    assert.equal(episode.source_entry_id == null, true);
    assert.deepEqual(episode.metadata, {});
  }
  const entries = await command({ type: "get_entries" });
  assert(!JSON.stringify(entries).includes(memoryCredential));
  assert(!JSON.stringify(entries).includes(providerCredential));
  assert(!JSON.stringify(entries).includes(metadata.operator_token));
  await terminate(pi); pi = undefined;
  await terminate(service); service = undefined;
  console.log("Sharing service smoke passed: actual Pi credential/native classifier stream, private default and future windows, review approval, exact excerpt auto publication, exclusion precedence, durable Pi-session restart and immutable lost-ACK replay, user-only modes, private reset, live revocation, and selected-text-only authenticated Python publications.");
} finally {
  await terminate(pi); await terminate(service);
  await rm(directory, { recursive: true, force: true });
}
