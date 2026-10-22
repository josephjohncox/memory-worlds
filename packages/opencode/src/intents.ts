import { constants } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { link, lstat, mkdir, open, opendir, unlink } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join, parse, resolve } from "node:path";
import { object, readOwnedFile, scopeKey, selection, type ScopeSelection } from "./config.js";
import { MemoryWorldsError } from "./service.js";

const MAX_BYTES = 1_048_576;
const MAX_ENTRIES = 4096;
export interface CaptureArguments { text: string; sourceId: string; occurredAt: string }
interface IntentKey {
  url: string;
  credential_id: string;
  world_id: string;
  scope_id: string;
  source_id: string;
}

function fields(value: Record<string, unknown>, expected: readonly string[]): void {
  if (Object.keys(value).length !== expected.length || expected.some(key => !(key in value))) throw new Error();
}
function originalRequest(value: unknown, key: IntentKey, args: CaptureArguments): Record<string, unknown> {
  const request = object(value);
  fields(request, ["source_id", "content", "occurred_at", "source_type", "session_id", "source_entry_id", "metadata", "project", "scope_id"]);
  if (request.source_id !== args.sourceId || request.content !== args.text || request.occurred_at !== args.occurredAt || request.scope_id !== key.scope_id || request.source_type !== "text" || request.project !== true) throw new Error();
  for (const name of ["session_id", "source_entry_id"]) {
    if (typeof request[name] !== "string" || !request[name].length || request[name].length > 500) throw new Error();
  }
  const metadata = object(request.metadata);
  fields(metadata, ["harness", "opencode_session_id", "opencode_message_id", "opencode_agent"]);
  if (metadata.harness !== "opencode" || metadata.opencode_session_id !== request.session_id || metadata.opencode_message_id !== request.source_entry_id || typeof metadata.opencode_agent !== "string" || !metadata.opencode_agent.length || metadata.opencode_agent.length > 100) throw new Error();
  return request;
}

async function privateDirectory(path: string): Promise<void> {
  const root = parse(path).root;
  let current = root;
  for (const part of path.slice(root.length).split(/[\\/]/u).filter(Boolean)) {
    current = join(current, part);
    try { await mkdir(current, { mode: 0o700 }); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
    const info = await lstat(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error();
    if (process.platform !== "win32" && (info.mode & 0o022) && !(info.mode & 0o1000)) throw new Error();
    if (current === path && (process.getuid && info.uid !== process.getuid() || process.platform !== "win32" && (info.mode & 0o077))) throw new Error();
  }
}

async function capacity(directory: string): Promise<void> {
  let entries = 0;
  for await (const _entry of await opendir(directory)) {
    if (++entries >= MAX_ENTRIES) throw new MemoryWorldsError("The private capture intent journal is full; review its retained records before creating more captures");
  }
}

async function syncDirectory(directory: string): Promise<void> {
  const handle = await open(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try { await handle.sync(); }
  finally { await handle.close(); }
}

/** Durable explicit write intents. No upload worker, automatic capture or token storage. */
export class CaptureIntents {
  private readonly directory: string;
  constructor(directory?: string) {
    const base = process.env.XDG_STATE_HOME ?? join(homedir(), ".local/state");
    const selected = directory ?? join(base, "memory-worlds/opencode/captures");
    if (!isAbsolute(selected) || selected.length > 8192 || selected.split(/[\\/]/u).some(part => part === "." || part === "..") || /[\x00-\x1f\x7f]/u.test(selected)) throw new MemoryWorldsError("The private capture intent journal requires an absolute state directory");
    this.directory = resolve(selected);
    if (!this.directory || this.directory === parse(this.directory).root) throw new MemoryWorldsError("The private capture intent journal requires a dedicated state directory");
  }

  async request(url: string, credentialId: string, target: ScopeSelection, args: CaptureArguments, candidate: Record<string, unknown>): Promise<Record<string, unknown>> {
    try {
      if (url.length > 4096 || !/^[A-Za-z0-9_-]{1,100}$/u.test(credentialId) || !args.sourceId.length || args.sourceId.length > 500 || !args.text.trim() || args.text.length > 200_000 || !args.occurredAt.length || args.occurredAt.length > 100) throw new Error();
      const selected = selection(target);
      const key: IntentKey = { url, credential_id: credentialId, ...selected, source_id: args.sourceId };
      const identity = JSON.stringify(key);
      const filename = createHash("sha256").update(identity).digest("hex") + ".json";
      await privateDirectory(this.directory);
      const path = join(this.directory, filename);
      const existing = async (): Promise<Record<string, unknown> | undefined> => {
        let bytes: Buffer;
        try { bytes = await readOwnedFile(path, MAX_BYTES, true); }
        catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined; throw error; }
        const document = object(JSON.parse(bytes.toString("utf8")));
        fields(document, ["key", "request"]);
        if (JSON.stringify(document.key) !== identity) throw new Error();
        const saved = object(document.request);
        if (saved.content !== args.text || saved.source_id !== args.sourceId || saved.occurred_at !== args.occurredAt) throw new MemoryWorldsError("This capture sourceId already records different text or a different timestamp; retry the original arguments or use a new sourceId");
        return originalRequest(document.request, key, args);
      };
      const saved = await existing();
      if (saved) { await syncDirectory(this.directory); return saved; }
      await capacity(this.directory);
      const request = originalRequest(candidate, key, args);
      const bytes = Buffer.from(JSON.stringify({ key, request }));
      if (bytes.byteLength > MAX_BYTES) throw new MemoryWorldsError("The explicit capture intent exceeds the 1 MiB private journal limit");
      const temporary = join(this.directory, `.intent-${randomUUID()}.tmp`);
      const handle = await open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW | constants.O_NONBLOCK, 0o600);
      try {
        try { await handle.writeFile(bytes); await handle.sync(); }
        finally { await handle.close(); }
        try { await link(temporary, path); }
        catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
        // Persist the winning immutable filename before any external capture POST.
        await syncDirectory(this.directory);
        const result = await existing();
        if (!result || scopeKey(selection({ world_id: key.world_id, scope_id: result.scope_id })) !== scopeKey(selected)) throw new Error();
        return result;
      } finally { await unlink(temporary); }
    } catch (error) {
      if (error instanceof MemoryWorldsError) throw error;
      throw new MemoryWorldsError("Cannot access the private capture intent journal; check its owned regular files and private directory permissions");
    }
  }
}
