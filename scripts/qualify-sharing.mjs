#!/usr/bin/env node
/** Exercise sharing controls using only labeled synthetic inputs and local providers. */
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { performance } from "node:perf_hooks";
import { SHARING_CASES, validateCases } from "./qualify-sharing-cases.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export function scoreSharing(item, decisions, threshold = 0.95) {
  const automatic = decisions.filter(value => value.visibility === "shareable" && value.confidence >= threshold);
  const violations = automatic.filter(value => item.expected === "private" || value.category !== item.category || !item.allowed.some(text => text.includes(value.excerpt)) || (item.requiredText ?? []).some(text => !value.excerpt.includes(text)));
  return {
    expected: item.expected,
    automatic: automatic.length,
    falsePositive: violations.length > 0,
    falseNegative: item.expected === "shareable" && !automatic.length,
    passed: !violations.length && (item.expected === "private" || automatic.length > 0),
  };
}
export function summarize(results) {
  const completed = results.filter(item => item.status === "complete"), failed = results.filter(item => item.status !== "complete");
  const published = completed.filter(item => item.score.automatic > 0);
  const correct = published.filter(item => !item.score.falsePositive);
  const positives = completed.filter(item => item.score.expected === "shareable");
  const detected = positives.filter(item => !item.score.falseNegative && !item.score.falsePositive);
  return {
    cases: results.length, completed: completed.length, errors: failed.length,
    unsafeAutoShareCases: completed.filter(item => item.score.falsePositive).length,
    missedShareableCases: positives.filter(item => item.score.falseNegative).length,
    precision: published.length ? correct.length / published.length : null,
    recall: positives.length ? detected.length / positives.length : null,
    passed: results.length > 0 && failed.length === 0 && completed.every(item => item.score.passed),
  };
}
async function modules() {
  const sdkRoot = resolve(root, "packages/pi/node_modules/@earendil-works/pi-coding-agent");
  const sdkManifest = JSON.parse(await readFile(resolve(sdkRoot, "package.json"), "utf8"));
  const sdk = await import(pathToFileURL(resolve(sdkRoot, sdkManifest.exports["."].import)).href);
  const aiRoot = resolve(root, "packages/pi/node_modules/@earendil-works/pi-ai");
  const aiManifest = JSON.parse(await readFile(resolve(aiRoot, "package.json"), "utf8"));
  const ai = await import(pathToFileURL(resolve(aiRoot, aiManifest.exports["."].import)).href);
  const manifest = JSON.parse(await readFile(resolve(root, "packages/pi/package.json"), "utf8"));
  const sharing = await import(pathToFileURL(resolve(root, "packages/pi", manifest.exports["./sharing"].import)).href);
  return { sdk, sharing, ai };
}
async function receipt(report, output) {
  if (output) await writeFile(output, JSON.stringify(report, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  console.log(JSON.stringify(report));
}
export async function qualify(options) {
  const cases = validateCases(SHARING_CASES).slice(0, options.limit ?? 100);
  const datasetDigest = createHash("sha256").update(JSON.stringify(SHARING_CASES)).digest("hex");
  const { sdk, sharing, ai } = await modules();
  if (options.mode === "offline") {
    const results = cases.map(item => {
      const settings = sharing.parseSharingSettings({ classifier: { provider: "synthetic", model: "guard-only" }, categories: item.categories, excludedTopics: item.excludedTopics });
      const blocked = item.entries.some(entry => sharing.sharingBlocked(entry.text, settings));
      return { id: item.id, topic: item.topic, expectedGuard: item.guard, blocked, passed: blocked === item.guard };
    });
    return { kind: "sharing-deterministic-guards", modelCalls: 0, datasetDigest, cases: results.length, passed: results.every(item => item.passed), results, limitation: "Deterministic guard coverage only; no model accuracy claim." };
  }
  const provider = "local-qualification", modelId = "scripted-classifier";
  const credentials = { read: async () => undefined, list: async () => [], modify: async () => { throw new Error("No credential writes"); }, delete: async () => { throw new Error("No credential writes"); } };
  const runtime = await sdk.ModelRuntime.create({ credentials, modelsPath: null, allowModelNetwork: false, refreshOnCreate: false });
  const registry = new sdk.ModelRegistry(runtime);
  const fixture = ai.fauxProvider({ provider, models: [{ id: modelId, name: "Scripted local qualification", input: ["text"] }] });
  registry.registerProvider({ ...fixture.provider, auth: { apiKey: { name: "Local synthetic authentication", check: async () => ({ type: "api_key", source: "local-emulator" }), resolve: async () => ({ auth: { apiKey: "local-synthetic-fixture" }, source: "local-emulator" }) } } });
  await runtime.refresh({ allowNetwork: false });
  const model = registry.find(provider, modelId);
  if (!model || !registry.hasConfiguredAuth(model)) throw new Error("Local scripted provider failed to initialize");
  let calls = 0;
  const usage = { input: 0, output: 0, cost: 0 };
  const dispatch = {
    find: registry.find.bind(registry), hasConfiguredAuth: registry.hasConfiguredAuth.bind(registry),
    streamSimple(selected, context, settings) {
      if (calls >= options.maxCalls) throw new Error("Qualification request budget exhausted");
      calls += 1;
      const stream = registry.streamSimple(selected, context, settings);
      return { result: async () => {
        const response = await stream.result();
        usage.input += response.usage?.input ?? 0; usage.output += response.usage?.output ?? 0;
        usage.cost += response.usage?.cost?.total ?? 0;
        return response;
      } };
    },
  };
  const results = [];
  for (const item of cases) {
    fixture.setResponses([(context, settings) => {
      if (context.tools?.length || settings.apiKey !== "local-synthetic-fixture") throw new Error("Local classifier boundary failed");
      const source = item.entries[0];
      const selected = options.emulator === "unsafe" || item.expected === "shareable";
      const decisions = selected ? [{ entryId: source.entryId, excerpt: options.emulator === "invalid-span" ? "Invented source text." : source.text, category: item.category ?? item.categories?.[0] ?? "team-discussion", visibility: "shareable", confidence: 0.99 }] : [];
      return ai.fauxAssistantMessage(JSON.stringify(decisions));
    }]);
    const settings = sharing.parseSharingSettings({ classifier: { provider, model: modelId }, categories: item.categories, excludedTopics: item.excludedTopics, timeoutMs: options.timeoutMs });
    const start = performance.now(), before = calls;
    try {
      const decisions = await sharing.classifySharingWithPi({ modelRegistry: dispatch }, item.entries, settings, AbortSignal.timeout(options.timeoutMs));
      results.push({ id: item.id, topic: item.topic, status: "complete", modelCalls: calls - before, elapsedMs: Math.round(performance.now() - start), score: scoreSharing(item, decisions, settings.minConfidence), decisions: decisions.map(value => ({ category: value.category, visibility: value.visibility, confidence: value.confidence, excerptSha256: createHash("sha256").update(value.excerpt).digest("hex") })) });
    } catch {
      results.push({ id: item.id, topic: item.topic, status: "classification_failed", modelCalls: calls - before, elapsedMs: Math.round(performance.now() - start) });
      if (calls >= options.maxCalls) break;
    }
  }
  return { kind: "sharing-local-emulation", provider, model: modelId, api: model.api, emulator: options.emulator, datasetDigest, ...summarize(results), totalCases: cases.length, modelCalls: calls, externalCalls: 0, usage, results, qualificationComplete: results.length === cases.length, limitation: "Scripted labels exercise native Pi dispatch, deterministic exclusions and validation. External model judgment and accuracy are assumed, not measured." };
}
async function main() {
  const { values } = parseArgs({ options: { mode: { type: "string", default: "offline" }, emulator: { type: "string", default: "labeled" }, output: { type: "string" }, limit: { type: "string", default: "100" }, "max-calls": { type: "string", default: "24" }, "timeout-ms": { type: "string", default: "10000" } } });
  if (!["offline", "emulated"].includes(values.mode) || !["labeled", "unsafe", "invalid-span"].includes(values.emulator)) throw new Error("Unknown local qualification mode");
  const options = { ...values, limit: Number(values.limit), maxCalls: Number(values["max-calls"]), timeoutMs: Number(values["timeout-ms"]) };
  if (![options.limit, options.maxCalls, options.timeoutMs].every(Number.isInteger) || options.limit < 1 || options.limit > 100 || options.maxCalls < 1 || options.maxCalls > 100 || options.timeoutMs < 100 || options.timeoutMs > 60000) throw new Error("Invalid qualification bounds");
  const report = await qualify(options);
  await receipt(report, options.output);
  if (!report.passed || report.qualificationComplete === false) process.exitCode = 1;
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(() => { console.error("Qualification failed; verify bounded arguments, built Pi package and exclusive output path."); process.exitCode = 1; });
