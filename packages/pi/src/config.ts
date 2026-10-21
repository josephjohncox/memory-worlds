import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";
import { scopeReferenceSelection, serviceURL, type ScopeSelection } from "./client.js";
import { parseSharingSettings, type SharingSettings } from "./sharing.js";

export interface MemoryConfig {
  url: string; tokenFile: string; readMountIds: string[];
  agent?: string; cliPath: string;
  automaticCapture: boolean; readScopes: ScopeSelection[]; writeScope?: ScopeSelection;
  graphAliases: { project?: ScopeSelection; global?: ScopeSelection }; projectCaptured: boolean; correctionHints: boolean; reviewCorrections: boolean; spoolDirectory?: string;
  retrieval: { enabled: boolean; mode: "source" | "graph"; limit: number; maxChars: number };
  sharing?: SharingSettings;
}
function ids(value: unknown): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 50 || value.some(item => typeof item !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(item))) {
    throw new Error("Mount selections require up to 20 identifiers");
  }
  return [...new Set(value)];
}
export function parseConfig(value: unknown, env: NodeJS.ProcessEnv = process.env, home = homedir()): MemoryConfig {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid Memory Worlds config");
  const config = value as Record<string, unknown>;
  const allowed = ["url", "tokenFile", "readMountIds", "automaticCapture", "retrieval", "readScopes", "writeScope", "graphAliases", "projectCaptured", "correctionHints", "reviewCorrections", "spoolDirectory", "sharing", "agent", "cliPath"];
  if (Object.keys(config).some(key => !allowed.includes(key))) throw new Error("Unknown Memory Worlds config key; use tokenFile for credentials");
  const rawTokenFile = env.MEMORY_WORLDS_TOKEN_FILE ?? config.tokenFile ?? join(home, ".config/memory-worlds/pi.token");
  if (typeof rawTokenFile !== "string") throw new Error("tokenFile must be an absolute path");
  const tokenFile = rawTokenFile.startsWith("~/") ? join(home, rawTokenFile.slice(2)) : rawTokenFile;
  if (!isAbsolute(tokenFile)) throw new Error("tokenFile must be an absolute path");
  const url = env.MEMORY_WORLDS_URL ?? config.url ?? "http://127.0.0.1:8787";
  if (typeof url !== "string") throw new Error("url must be a string");
  const agent = config.agent;
  if (agent !== undefined && (typeof agent !== "string" || !(agent.startsWith("@") ? /^@[a-z][a-z0-9_-]{0,63}$/.test(agent) : /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(agent)))) throw new Error("agent requires a registered UUID or @alias");
  const cliPath = config.cliPath ?? "memory-worlds";
  if (typeof cliPath !== "string" || !cliPath.trim() || cliPath.length > 8192 || cliPath.includes("\0")) throw new Error("cliPath must name the installed Memory Worlds executable");
  const readMountIds = ids(config.readMountIds);
  if (readMountIds.length > 20) throw new Error("At most 20 remote mounts can be selected");
  if (config.automaticCapture !== undefined && typeof config.automaticCapture !== "boolean") throw new Error("automaticCapture must be boolean");
  const retrieval = config.retrieval === undefined ? {} : config.retrieval as Record<string, unknown>;
  if (!retrieval || typeof retrieval !== "object" || Array.isArray(retrieval)) throw new Error("Invalid retrieval config");
  if (Object.keys(retrieval).some(key => !["enabled", "mode", "limit", "maxChars"].includes(key))) throw new Error("Unknown retrieval config key");
  if (retrieval.enabled !== undefined && typeof retrieval.enabled !== "boolean") throw new Error("retrieval.enabled must be boolean");
  const mode = retrieval.mode ?? "graph";
  const limit = retrieval.limit ?? 5;
  const maxChars = retrieval.maxChars ?? 8000;
  if (!["source", "graph"].includes(String(mode)) || !Number.isInteger(limit) || Number(limit) < 1 || Number(limit) > 20 || !Number.isInteger(maxChars) || Number(maxChars) < 256 || Number(maxChars) > 32000) {
    throw new Error("Retrieval requires source/graph mode, limit 1..20 and maxChars 256..32000");
  }
  if (config.readScopes !== undefined && (!Array.isArray(config.readScopes) || config.readScopes.length > 50)) throw new Error("readScopes requires up to 50 selections");
  const readScopes = (config.readScopes as unknown[] | undefined ?? []).map(scopeReferenceSelection);
  const writeScope = config.writeScope === undefined ? undefined : scopeReferenceSelection(config.writeScope);
  const aliases = config.graphAliases === undefined ? {} : config.graphAliases as Record<string, unknown>;
  if (!aliases || typeof aliases !== "object" || Array.isArray(aliases) || Object.keys(aliases).some(key => !["project", "global"].includes(key))) throw new Error("graphAliases permits explicit project/global scope selections only");
  const graphAliases = { project: aliases.project === undefined ? undefined : scopeReferenceSelection(aliases.project), global: aliases.global === undefined ? undefined : scopeReferenceSelection(aliases.global) };
  if (config.projectCaptured !== undefined && typeof config.projectCaptured !== "boolean") throw new Error("projectCaptured must be boolean");
  if (config.correctionHints !== undefined && typeof config.correctionHints !== "boolean") throw new Error("correctionHints must be boolean");
  if (config.reviewCorrections !== undefined && typeof config.reviewCorrections !== "boolean") throw new Error("reviewCorrections must be boolean");
  const spoolDirectory = config.spoolDirectory === undefined ? undefined : typeof config.spoolDirectory === "string" && config.spoolDirectory.startsWith("~/") ? join(home, config.spoolDirectory.slice(2)) : config.spoolDirectory;
  if (spoolDirectory !== undefined && (typeof spoolDirectory !== "string" || !isAbsolute(spoolDirectory))) throw new Error("spoolDirectory must be an absolute private directory");
  return {
    url: serviceURL(url), tokenFile, readMountIds, agent, cliPath,
    readScopes, writeScope, graphAliases, spoolDirectory, reviewCorrections: config.reviewCorrections === true, projectCaptured: config.projectCaptured !== false, correctionHints: config.correctionHints !== false,
    automaticCapture: Boolean(writeScope && config.automaticCapture === true),
    sharing: parseSharingSettings(config.sharing),
    retrieval: { enabled: retrieval.enabled === true, mode: mode as "source" | "graph", limit: Number(limit), maxChars: Number(maxChars) },
  };
}
export async function loadConfig(path = process.env.MEMORY_WORLDS_CONFIG ?? join(homedir(), ".pi/agent/memory-worlds.json")): Promise<MemoryConfig> {
  let content: string;
  try { content = await readFile(path, "utf8"); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return parseConfig({});
    throw new Error("Cannot read Memory Worlds config");
  }
  if (content.length > 65536) throw new Error("Memory Worlds config exceeds 64 KiB");
  return parseConfig(JSON.parse(content));
}
