import { constants } from "node:fs";
import { open, type FileHandle } from "node:fs/promises";
import { homedir } from "node:os";
import { isIP } from "node:net";
import { isAbsolute, join } from "node:path";

export interface ScopeSelection { world_id: string; scope_id: string }
export interface MemoryWorldsConfig {
  readonly url: string;
  readonly tokenFile: string;
  readonly readScopes: readonly ScopeSelection[];
  readonly writeScope?: ScopeSelection;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const WORLD_ID = /^[a-zA-Z0-9_-]{1,100}$/;
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid Memory Worlds response");
  return value as Record<string, unknown>;
}
export function selection(value: unknown): ScopeSelection {
  const item = object(value);
  if (typeof item.world_id !== "string" || !WORLD_ID.test(item.world_id) || typeof item.scope_id !== "string" || !UUID.test(item.scope_id)) throw new Error("A scope selection requires canonical world and scope IDs");
  return { world_id: item.world_id, scope_id: item.scope_id.toLowerCase() };
}
export function scopeKey(value: ScopeSelection): string { return `${value.world_id}/${value.scope_id}`; }
function absolutePath(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 8192 || /[\x00-\x1f\x7f]/u.test(value)) throw new Error("Memory Worlds files require absolute paths");
  const path = value.startsWith("~/") ? join(homedir(), value.slice(2)) : value;
  if (!isAbsolute(path)) throw new Error("Memory Worlds files require absolute paths");
  return path;
}
function serviceURL(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096 || /\s/u.test(value)) throw new Error("Invalid Memory Worlds service URL");
  try {
    const url = new URL(value);
    const loopback = url.hostname === "localhost" || url.hostname === "[::1]" || isIP(url.hostname) === 4 && url.hostname.startsWith("127.");
    if (url.username || url.password || url.search || url.hash || url.pathname !== "/" || !(url.protocol === "https:" || url.protocol === "http:" && loopback)) throw new Error();
    return url.origin;
  } catch { throw new Error("Memory Worlds requires an HTTPS service origin, or loopback HTTP"); }
}

export function parseConfig(value: unknown): MemoryWorldsConfig {
  const item = object(value);
  if (Object.keys(item).some(key => !["url", "tokenFile", "readScopes", "writeScope"].includes(key))) throw new Error("Unknown OpenCode memory profile setting");
  const reads = item.readScopes ?? [];
  if (!Array.isArray(reads) || reads.length > 50) throw new Error("Select up to 50 read scopes");
  const readScopes = reads.map(value => {
    if (Object.keys(object(value)).some(key => !["world_id", "scope_id"].includes(key))) throw new Error("Invalid scope selection fields");
    return Object.freeze(selection(value));
  });
  if (new Set(readScopes.map(scopeKey)).size !== readScopes.length) throw new Error("Read scope selections must be unique");
  const writeScope = item.writeScope === undefined ? undefined : selection(item.writeScope);
  if (item.writeScope !== undefined && Object.keys(object(item.writeScope)).some(key => !["world_id", "scope_id"].includes(key))) throw new Error("Invalid write scope fields");
  return Object.freeze({
    url: serviceURL(item.url), tokenFile: absolutePath(item.tokenFile),
    readScopes: Object.freeze(readScopes), writeScope: writeScope && Object.freeze(writeScope),
  });
}

export async function loadConfig(path?: string): Promise<MemoryWorldsConfig> {
  try {
    const selected = absolutePath(path ?? process.env.MEMORY_WORLDS_CONFIG ?? join(homedir(), ".config/memory-worlds/opencode.json"));
    const content = await readOwnedFile(selected, 65_536, false);
    return parseConfig(JSON.parse(content.toString("utf8")));
  } catch { throw new Error("Cannot load OpenCode memory profile; check MEMORY_WORLDS_CONFIG and its current configuration"); }
}

/** Reading at most maximum + 1 bytes also bounds files that grow after fstat. */
export async function boundedRead(handle: FileHandle, maximum: number): Promise<Buffer> {
  const buffer = Buffer.alloc(maximum + 1);
  let offset = 0;
  while (offset < buffer.byteLength) {
    const { bytesRead } = await handle.read(buffer, offset, buffer.byteLength - offset, null);
    if (!bytesRead) break;
    offset += bytesRead;
  }
  if (offset > maximum) throw new Error("Memory Worlds file exceeds its size limit");
  return buffer.subarray(0, offset);
}

export async function readOwnedFile(path: string, maximum: number, privateFile: boolean): Promise<Buffer> {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > maximum ||
        process.getuid && info.uid !== process.getuid() ||
        process.platform !== "win32" && (info.mode & (privateFile ? 0o077 : 0o022))) throw new Error("Memory Worlds files require owned regular files with safe permissions");
    return await boundedRead(handle, maximum);
  } finally { await handle.close(); }
}

/** Read one private descriptor so permission checks and bytes refer to the same file. */
export async function readToken(path: string): Promise<string> {
  try {
    const token = (await readOwnedFile(path, 16_386, true)).toString("utf8").trim();
    if (!token || token.length > 16_384 || /[^\x21-\x7e]/u.test(token)) throw new Error();
    return token;
  } catch { throw new Error("Memory Worlds requires a readable private regular token file with mode 0600"); }
}
