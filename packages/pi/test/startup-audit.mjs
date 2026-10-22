// Measure fresh Pi processes against synthetic HTTP only; no user settings, sessions or models.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const options = { runs: 3, latencyMs: 100, extension: packageRoot, piCli: join(packageRoot, "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"), output: undefined };
for (let index = 2; index < process.argv.length; index++) {
  const flag = process.argv[index];
  if (flag === "--help") {
    console.log("Measure isolated Pi startup with synthetic memory HTTP, without model requests.\n\nUsage: mise run pi:startup:audit -- [--runs 3] [--latency-ms 100] [--output REPORT.json]\n       [--extension PACKAGE_DIRECTORY] [--pi-cli PI_CLI_JS]\n\nReports RPC readiness, request counts and endpoint timelines. Times include fresh-process Pi loading.\nOptional paths select packages, never user configuration or sessions.");
    process.exit(0);
  }
  const value = process.argv[++index];
  if (!value || !["--runs", "--latency-ms", "--extension", "--pi-cli", "--output"].includes(flag)) throw new Error("Unknown or incomplete audit option; use --help");
  if (flag === "--runs") options.runs = Number(value);
  else if (flag === "--latency-ms") options.latencyMs = Number(value);
  else if (flag === "--extension") options.extension = resolve(value);
  else if (flag === "--pi-cli") options.piCli = resolve(value);
  else options.output = resolve(value);
}
if (!Number.isInteger(options.runs) || options.runs < 1 || options.runs > 20 || !Number.isInteger(options.latencyMs) || options.latencyMs < 0 || options.latencyMs > 2000) throw new Error("Audit requires runs 1..20 and latency-ms 0..2000");

const privateScope = "00000000-0000-4000-8000-000000000002";
const sharedScope = "00000000-0000-4000-8000-000000000001";
const agentId = "00000000-0000-4000-8000-000000000004";
const contextId = "00000000-0000-4000-8000-000000000003";
const selected = { world_id: "private", scope_id: privateScope };
const timestamp = "2026-10-20T00:00:00Z";
const token = "synthetic-startup-audit-token";
const world = { scope_id: sharedScope, id: "private", name: "Private", alias: "personal", authority_id: "fixture-authority", kind: "personal", owner_id: "alice", created_at: timestamp, projection_state: "disabled" };
const namedAgent = { id: agentId, kind: "agent", name: "Audit specialist", alias: "architect", owner_id: "alice", created_at: timestamp };
const scope = { ...selected, id: privateScope, alias: "notes", kind: "agent-persistent", name: "Notes", created_at: timestamp };
const scenarios = [
  { name: "pi-only", enabled: false },
  { name: "scoped", enabled: true },
  { name: "named-agent", enabled: true, named: true },
  { name: "repeated-aliases", enabled: true, named: true, aliases: true },
  { name: "capture-on", enabled: true, named: true, capture: true },
];

async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exit = once(child, "exit");
  child.kill("SIGTERM");
  const timer = setTimeout(() => child.kill("SIGKILL"), 2000);
  try { await exit; } finally { clearTimeout(timer); }
}

async function trial(scenario) {
  const directory = await mkdtemp(join(tmpdir(), "memory-worlds-startup-audit-"));
  const requests = [], unexpected = [];
  let child, started;
  const daemon = createServer(async (request, response) => {
    const call = { path: request.url, start_ms: Math.round(performance.now() - started) };
    requests.push(call);
    try {
      assert.equal(request.headers.authorization, `Bearer ${token}`);
      let body = "";
      for await (const chunk of request) body += chunk;
      await new Promise(resolve => setTimeout(resolve, options.latencyMs));
      let value;
      if (request.url === "/v1/me") value = { credential_id: "fixture", user_id: "alice", agent_id: "pi", authority_id: world.authority_id, worlds: [world] };
      else if (request.url === "/v1/worlds") value = [world];
      else if (request.url === "/v1/router/deliveries" || request.url === "/v1/mounts") value = [];
      else if (request.url === "/v1/identities/agent/resolve?reference=%40architect") value = namedAgent;
      else if (request.url === "/v1/worlds/private/scopes") value = [scope];
      else if (request.url === `/v1/worlds/private/scopes/${privateScope}`) value = scope;
      else if (request.url === "/v1/contexts") value = { ...JSON.parse(body), id: contextId, credential_id: "fixture", invocation_id: contextId, created_at: timestamp };
      else if (request.url === "/v1/scopes/flow") value = { allowed: true };
      else throw new Error("Unexpected fixture endpoint");
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify(value));
      call.end_ms = Math.round(performance.now() - started);
    } catch {
      unexpected.push(request.url);
      response.writeHead(500, { "Content-Type": "application/json" }).end("{}");
    }
  });
  try {
    daemon.listen(0, "127.0.0.1");
    await once(daemon, "listening");
    const url = `http://127.0.0.1:${daemon.address().port}`;
    const tokenPath = join(directory, "agent.token"), configPath = join(directory, "memory-worlds.json");
    const configured = scenario.aliases ? { world_id: "@personal", scope_id: "@notes" } : selected;
    await writeFile(tokenPath, token, { mode: 0o600 });
    await writeFile(configPath, JSON.stringify({ url, tokenFile: tokenPath, readScopes: [configured], writeScope: configured, ...(scenario.named ? { agent: "@architect" } : {}), automaticCapture: scenario.capture === true }));
    await mkdir(join(directory, "agent"), { mode: 0o700 });
    started = performance.now();
    child = spawn(process.execPath, [options.piCli, "--mode", "rpc", "--no-session", "--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", ...(scenario.enabled ? ["--extension", options.extension] : [])], {
      cwd: directory,
      // Build an allowlist: ambient model credentials, tracing and Pi settings never enter the child.
      env: { PATH: process.env.PATH, HOME: directory, LANG: "C.UTF-8", ...(process.env.SYSTEMROOT ? { SYSTEMROOT: process.env.SYSTEMROOT } : {}), PI_CODING_AGENT_DIR: join(directory, "agent"), MEMORY_WORLDS_CONFIG: configPath, MEMORY_WORLDS_TOKEN_FILE: tokenPath, MEMORY_WORLDS_URL: url },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let output = "", stderr = "";
    const events = [];
    child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
    child.stderr.on("data", chunk => { stderr += chunk; });
    const reply = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Pi startup audit timed out")), Math.max(20000, options.latencyMs * 30 + 5000));
      const fail = error => { clearTimeout(timer); reject(error); };
      child.on("error", () => fail(new Error("Cannot launch the configured Pi CLI")));
      child.on("exit", () => fail(new Error("Pi exited before RPC readiness")));
      child.stdout.on("data", chunk => {
        output += chunk;
        if (output.length + stderr.length > 2 * 1024 * 1024) { fail(new Error("Pi audit output exceeds its limit")); return; }
        let newline;
        while ((newline = output.indexOf("\n")) !== -1) {
          const line = output.slice(0, newline); output = output.slice(newline + 1);
          if (!line.trim()) continue;
          let event;
          try { event = JSON.parse(line); } catch { fail(new Error("Pi emitted invalid RPC data")); return; }
          events.push(event);
          if (event.id === "startup-audit-ready" && event.type === "response") { clearTimeout(timer); resolve(event); }
        }
      });
      child.stdin.write(JSON.stringify({ id: "startup-audit-ready", type: "get_commands" }) + "\n");
    });
    const readyMs = Math.round(performance.now() - started);
    assert.equal(reply.success, true, "Pi get_commands failed");
    if (scenario.enabled) assert(reply.data.commands.some(command => command.name === "memory"), "Memory command failed to register");
    assert(!events.some(event => event.type === "extension_error" || event.type === "extension_ui_request" && event.method === "notify" && event.notifyType === "error"), "Extension startup failed");
    assert(!stderr.includes("Failed to load extension"), "Pi failed to load the selected extension");
    assert.deepEqual(unexpected, [], "Unexpected fixture requests");
    // Shutdown can revalidate capture policy. Freeze this timeline at readiness
    // so later shutdown requests cannot be counted as startup work.
    return { ready_ms: readyMs, request_count: requests.length, requests: structuredClone(requests) };
  } finally {
    await stop(child);
    daemon.closeAllConnections();
    await new Promise(resolve => daemon.close(resolve));
    await rm(directory, { recursive: true, force: true });
  }
}

async function packageVersion(path, expected) {
  for (let current = dirname(path); dirname(current) !== current; current = dirname(current)) {
    try { const info = JSON.parse(await readFile(join(current, "package.json"), "utf8")); if (info.name === expected) return info.version; } catch { /* Continue to the owning package manifest. */ }
  }
  return "unknown";
}
function median(values) { const sorted = [...values].sort((a, b) => a - b), middle = Math.floor(sorted.length / 2); return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2; }
const report = {
  generated_at: new Date().toISOString(),
  node_version: process.versions.node, platform: `${process.platform}-${process.arch}`,
  pi_version: await packageVersion(options.piCli, "@earendil-works/pi-coding-agent"),
  extension_version: JSON.parse(await readFile(join(options.extension, "package.json"), "utf8")).version,
  extension_entry_sha256: createHash("sha256").update(await readFile(join(options.extension, "dist/extension.js"))).digest("hex"),
  methodology: "Fresh process to first successful get_commands RPC response, after awaited session_start. Temporary HOME, agent directory and fixture token. No model calls or user sessions. HTTP response delay is synthetic; OS filesystem caches are not cleared. No timing thresholds.",
  latency_ms: options.latencyMs, runs_per_scenario: options.runs, scenarios: [],
};
for (const scenario of scenarios) {
  const trials = [];
  for (let run = 0; run < options.runs; run++) trials.push(await trial(scenario));
  const result = { scenario: scenario.name, median_ready_ms: median(trials.map(trial => trial.ready_ms)), median_request_count: median(trials.map(trial => trial.request_count)), trials };
  report.scenarios.push(result);
  console.log(`${scenario.name.padEnd(17)} ${String(result.median_ready_ms).padStart(5)} ms  ${result.median_request_count} HTTP requests`);
}
if (options.output) {
  await writeFile(options.output, JSON.stringify(report, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  console.log("Wrote startup audit report.");
}
