import { execFile } from "node:child_process";
import { isAbsolute, resolve } from "node:path";
import { promisify } from "node:util";
import type { EpisodeInput, ImportResult, MemoryWorldsClient, ScopeSelection } from "./client.js";

export class DirectoryImportError extends Error { constructor(message: string) { super(message); this.name = "MemoryWorldsImportError"; } }

export type DirectoryFormat = "auto" | "chatgpt" | "claude" | "richlow-md" | "documents";
export interface DirectoryOptions {
  cliPath?: string; format?: DirectoryFormat; allBranches?: boolean; sourceTimezone?: string;
  fallbackTimestamp?: string; sourceOnly?: boolean; timeoutMs?: number; signal?: AbortSignal;
}
export interface DirectoryReport {
  files: { path: string; format: string; bytes: number; sha256: string; episodes: number }[];
  omitted_files: number; scanned_files: number; skipped_files: number; selected_files: number;
  episodes: number; deduplicated: number; formats: Record<string, number>; conversations: number;
  unsupported_parts: number; references_only: number; skipped: number; warnings: string[]; payload_bytes: number;
}
export interface DirectoryPlan { report: DirectoryReport; episodes: EpisodeInput[] }
const execute = promisify(execFile);
const FORMATS = ["auto", "chatgpt", "claude", "richlow-md", "documents"];
const EPISODE_FIELDS = new Set(["scope_id", "context_id", "source_id", "content", "occurred_at", "source_type", "session_id", "source_entry_id", "parent_entry_id", "role", "source_ref", "metadata", "project"]);
function record(value: unknown): value is Record<string, unknown> { return Boolean(value && typeof value === "object" && !Array.isArray(value)); }
function count(value: unknown): value is number { return Number.isSafeInteger(value) && Number(value) >= 0; }
function text(value: unknown, maximum: number): value is string {
  if (typeof value !== "string" || !value.length || value.length > maximum * 2) return false;
  let length = 0;
  for (const point of value) { const scalar = point.codePointAt(0)!; if (scalar >= 0xd800 && scalar <= 0xdfff || ++length > maximum) return false; }
  return true;
}
function parsedPlan(value: unknown): DirectoryPlan {
  if (!record(value) || Object.keys(value).some(key => !["report", "episodes"].includes(key)) || !record(value.report) || !Array.isArray(value.episodes) || !value.episodes.length || value.episodes.length > 10_000) throw new DirectoryImportError("Invalid directory planner response");
  const report = value.report;
  const fields = ["omitted_files", "scanned_files", "skipped_files", "selected_files", "episodes", "deduplicated", "conversations", "unsupported_parts", "references_only", "skipped", "payload_bytes"];
  if (fields.some(key => !count(report[key])) || Object.keys(report).some(key => ![...fields, "files", "formats", "warnings"].includes(key)) || !Array.isArray(report.files) || report.files.length > 200 || !record(report.formats) || Object.entries(report.formats).some(([key, value]) => !FORMATS.includes(key) || !count(value)) || !Array.isArray(report.warnings) || report.warnings.length > 100 || report.warnings.some(warning => !text(warning, 4000))) throw new DirectoryImportError("Invalid directory planner report");
  for (const file of report.files) {
    if (!record(file) || Object.keys(file).some(key => !["path", "format", "bytes", "sha256", "episodes"].includes(key)) || !text(file.path, 8192) || isAbsolute(file.path) || file.path.split(/[\\/]/).includes("..") || !FORMATS.includes(String(file.format)) || !count(file.bytes) || !count(file.episodes) || !/^[0-9a-f]{64}$/.test(String(file.sha256))) throw new DirectoryImportError("Invalid directory planner file report");
  }
  const episodes = value.episodes.map(value => {
    if (!record(value) || Object.keys(value).some(key => !EPISODE_FIELDS.has(key)) || value.scope_id != null || value.context_id != null || !text(value.source_id, 500) || !text(value.content, 200_000) || !value.content.trim() || !text(value.occurred_at, 100) || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value.occurred_at) || !Number.isFinite(Date.parse(value.occurred_at)) || !["text", "chatgpt", "claude", "pi"].includes(String(value.source_type)) || value.project !== true || !record(value.metadata)) throw new DirectoryImportError("Directory planner returned an invalid or scoped episode");
    for (const [key, maximum] of [["session_id", 500], ["source_entry_id", 500], ["parent_entry_id", 500], ["role", 100], ["source_ref", 2000]] as const) if (value[key] != null && value[key] !== "" && !text(value[key], maximum)) throw new DirectoryImportError("Directory planner returned invalid source attribution");
    const { scope_id: _scope, context_id: _context, ...episode } = value;
    return episode as unknown as EpisodeInput;
  });
  if (report.episodes !== episodes.length || new Set(episodes.map(episode => episode.source_id)).size !== episodes.length) throw new DirectoryImportError("Directory planner count or identity mismatch");
  return { report: report as unknown as DirectoryReport, episodes };
}
/** Pure local planning through the installed current Memory Worlds CLI; credentials never enter it. */
export async function planDirectory(directory: string, options: DirectoryOptions = {}): Promise<DirectoryPlan> {
  options = { ...options };
  const cliPath = options.cliPath ?? "memory-worlds", format = options.format ?? "auto", timeout = options.timeoutMs ?? 60_000;
  if (!text(directory, 8192) || directory.includes("\0") || !text(cliPath, 8192) || cliPath.includes("\0") || !FORMATS.includes(format) || !Number.isSafeInteger(timeout) || timeout < 100 || timeout > 300_000) throw new DirectoryImportError("Invalid directory import options");
  options.signal?.throwIfAborted();
  const args = ["plan-directory", resolve(directory), "--format", format, "--emit-records"];
  if (options.allBranches) args.push("--all-branches");
  for (const [flag, value] of [["--source-timezone", options.sourceTimezone], ["--fallback-timestamp", options.fallbackTimestamp]]) {
    if (value !== undefined) { if (!text(value, 200) || value.includes("\0")) throw new DirectoryImportError("Invalid directory import date options"); args.push(flag!, value); }
  }
  const env: NodeJS.ProcessEnv = { PYTHONUTF8: "1", PYTHONNOUSERSITE: "1" };
  for (const key of ["PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TMP", "TEMP"]) if (process.env[key] !== undefined) env[key] = process.env[key];
  let output: Buffer;
  try {
    const result = await execute(cliPath, args, { env, shell: false, encoding: "buffer", maxBuffer: 40 * 1024 * 1024, timeout, signal: options.signal, windowsHide: true });
    output = result.stdout;
  } catch {
    options.signal?.throwIfAborted();
    throw new DirectoryImportError("Directory planning failed; verify the installed Memory Worlds CLI, input format, dates and file limits");
  }
  let plan: DirectoryPlan;
  try { plan = parsedPlan(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(output))); }
  catch { throw new DirectoryImportError("The installed Memory Worlds CLI returned an invalid directory plan"); }
  if (options.sourceOnly) plan.episodes = plan.episodes.map(episode => ({ ...episode, project: false }));
  return plan;
}

/** Import selected external files without changing chat reads, capture target or exposure. */
export async function importDirectory(client: MemoryWorldsClient, destination: ScopeSelection, directory: string, options: DirectoryOptions = {}): Promise<{ report: DirectoryReport } & ImportResult> {
  destination = structuredClone(destination); options = { ...options };
  const plan = await planDirectory(directory, options);
  return client.withCredentialSession(async pinned => {
    const identity = await pinned.identity(options.signal), selected = await pinned.scope(destination, options.signal);
    const world = identity.worlds.find(item => item.id === destination.world_id);
    if (!world || !["personal", "private-work"].includes(world.kind) || world.owner_id !== identity.user_id || !["agent-persistent", "invocation-private"].includes(selected.kind)) throw new DirectoryImportError("Directory import requires your own private world and private scope");
    const current = await client.identity(options.signal);
    if (current.authority_id !== identity.authority_id || current.credential_id !== identity.credential_id) throw new DirectoryImportError("Memory credential changed during directory import");
    return { report: plan.report, ...await pinned.importEpisodes(destination, plan.episodes, options.signal) };
  }, options.signal);
}
