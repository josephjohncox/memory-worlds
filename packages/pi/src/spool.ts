import { createHash, randomUUID } from "node:crypto";
import { mkdir, lstat, open, readFile, readdir, unlink, link } from "node:fs/promises";
import { isAbsolute, join } from "node:path";
import { scopeSelection, serviceURL, type EpisodeInput, type MemoryWorldsClient, type Receipt, type ScopeSelection } from "./client.js";

export interface SpoolBinding { serviceUrl: string; authorityId: string; credentialId: string }
export interface SpoolTarget { world_id: string; scope_id?: string }
export interface SpoolFlow { read_scopes: ScopeSelection[]; write_scope: ScopeSelection }
interface Envelope { version: 1; binding: SpoolBinding; target: SpoolTarget; episode: EpisodeInput; flow?: SpoolFlow }
export interface SpoolDelivery { target: SpoolTarget; sourceId: string; receipt: Receipt }
function encoded(value: unknown): string { return JSON.stringify(value); }
function validate(value: unknown): Envelope {
  if (!value || typeof value !== "object") throw new Error("Invalid memory spool envelope");
  const item = value as Envelope;
  if (item.version !== 1 || !item.binding || typeof item.binding.authorityId !== "string" || !item.binding.authorityId || typeof item.binding.credentialId !== "string" || !item.binding.credentialId) throw new Error("Invalid memory spool binding");
  serviceURL(item.binding.serviceUrl);
  if (!item.target || !/^[a-zA-Z0-9_-]{1,100}$/.test(item.target.world_id)) throw new Error("Invalid memory spool target");
  if (item.target.scope_id) scopeSelection(item.target);
  if (!item.episode || typeof item.episode.source_id !== "string" || !item.episode.source_id || typeof item.episode.content !== "string" || !item.episode.content.trim() || item.episode.content.length > 200000 || typeof item.episode.occurred_at !== "string" || !Number.isFinite(Date.parse(item.episode.occurred_at))) throw new Error("Invalid memory spool source");
  if (item.episode.scope_id && item.episode.scope_id !== item.target.scope_id) throw new Error("Memory spool scope mismatch");
  if (item.flow) {
    if (!Array.isArray(item.flow.read_scopes) || item.flow.read_scopes.length > 50) throw new Error("Invalid memory spool flow guard");
    item.flow.read_scopes.forEach(scopeSelection); scopeSelection(item.flow.write_scope);
    if (item.flow.write_scope.world_id !== item.target.world_id || item.target.scope_id && item.flow.write_scope.scope_id !== item.target.scope_id) throw new Error("Memory spool flow target mismatch");
  }
  return item;
}
/** Private immutable source envelopes; credentials and daemon bootstrap material are never stored. */
export class MemorySpool {
  constructor(readonly directory: string, readonly maxEnvelopes = 10000, readonly maxBytes = 67_108_864) {
    if (!isAbsolute(directory)) throw new Error("Memory spool directory must be absolute");
    if (!Number.isInteger(maxEnvelopes) || maxEnvelopes < 1 || maxEnvelopes > 10000 || !Number.isInteger(maxBytes) || maxBytes < 1 || maxBytes > 67_108_864) throw new Error("Invalid bounded memory spool capacity");
  }
  private sameSource(received: EpisodeInput, expected: EpisodeInput): boolean {
    const canonical = (value: unknown): string => JSON.stringify(value && typeof value === "object" ? Array.isArray(value) ? value.map(item => JSON.parse(canonical(item))) : Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, item === undefined ? null : JSON.parse(canonical(item))])) : value);
    return Object.entries(expected).every(([key, value]) => value === undefined || (key === "occurred_at" ? Date.parse(String(value)) === Date.parse(received.occurred_at) : canonical(value) === canonical(received[key as keyof EpisodeInput])));
  }
  private async prepare(): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const info = await lstat(this.directory);
    if (!info.isDirectory() || info.isSymbolicLink() || (process.platform !== "win32" && (info.mode & 0o077))) throw new Error("Memory spool directory must be private (chmod 700)");
  }
  private key(envelope: Envelope): string {
    return createHash("sha256").update(encoded([envelope.binding, envelope.target, envelope.episode.source_id])).digest("hex");
  }
  async enqueue(binding: SpoolBinding, target: SpoolTarget, episode: EpisodeInput, flow?: SpoolFlow): Promise<void> {
    const envelope = validate({ version: 1, binding: { serviceUrl: serviceURL(binding.serviceUrl), authorityId: binding.authorityId, credentialId: binding.credentialId }, target: { world_id: target.world_id, ...(target.scope_id ? { scope_id: target.scope_id } : {}) }, episode, ...(flow ? { flow } : {}) });
    const body = encoded(envelope);
    if (Buffer.byteLength(body) > 1_048_576) throw new Error("Memory spool envelope exceeds 1 MiB");
    await this.prepare();
    const file = join(this.directory, `${this.key(envelope)}.json`);
    try {
      await this.read(file);
      if (await readFile(file, "utf8") !== body) throw new Error("Memory spool source identity conflicts with stored content");
      return;
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    const names = (await readdir(this.directory)).filter(name => /^[0-9a-f]{64}\.json$/.test(name));
    if (names.length >= this.maxEnvelopes) throw new Error("Memory spool capacity reached; drain existing sources first");
    let total = Buffer.byteLength(body);
    for (const name of names) { try { total += (await lstat(join(this.directory, name))).size; } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; } }
    if (total > this.maxBytes) throw new Error("Memory spool byte capacity reached; drain existing sources first");
    const temporary = join(this.directory, `.${randomUUID()}.tmp`);
    const handle = await open(temporary, "wx", 0o600);
    try {
      await handle.writeFile(body, "utf8"); await handle.sync(); await handle.close();
      try { await link(temporary, file); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        await this.read(file);
        if (await readFile(file, "utf8") !== body) throw new Error("Memory spool source identity conflicts with stored content");
      }
      if (process.platform !== "win32") {
        const directory = await open(this.directory, "r");
        try { await directory.sync(); } finally { await directory.close(); }
      }
    } finally { await handle.close(); await unlink(temporary).catch(() => {}); }
  }
  private async read(file: string): Promise<Envelope> {
    const info = await lstat(file);
    if (!info.isFile() || info.isSymbolicLink() || info.size > 1_048_576 || (process.platform !== "win32" && (info.mode & 0o077))) throw new Error("Memory spool file must be a bounded private regular file");
    return validate(JSON.parse(await readFile(file, "utf8")));
  }
  async status(): Promise<{ pending: number }> {
    await this.prepare();
    return { pending: (await readdir(this.directory)).filter(name => /^[0-9a-f]{64}\.json$/.test(name)).length };
  }
  async drain(client: MemoryWorldsClient, binding: SpoolBinding, signal?: AbortSignal, limit = 100): Promise<{ delivered: SpoolDelivery[]; blocked: number }> {
    return client.withCredentialSession(pinned => this.drainPinned(pinned, binding, signal, limit), signal);
  }
  private async drainPinned(client: MemoryWorldsClient, binding: SpoolBinding, signal?: AbortSignal, limit = 100): Promise<{ delivered: SpoolDelivery[]; blocked: number }> {
    await this.prepare();
    const delivered: SpoolDelivery[] = []; let blocked = 0;
    const expected = { serviceUrl: serviceURL(binding.serviceUrl), authorityId: binding.authorityId, credentialId: binding.credentialId };
    if (client.url !== expected.serviceUrl) throw new Error("Spool client authority URL mismatch");
    // Verify the live credential and daemon before reading or dispatching any queued source.
    const identity = await client.identity(signal);
    if (identity.authority_id !== expected.authorityId || identity.credential_id !== expected.credentialId) throw new Error("Spool credential or authority changed; queued sources remain pinned");
    const names = (await readdir(this.directory)).filter(name => /^[0-9a-f]{64}\.json$/.test(name)).sort();
    for (const name of names) {
      if (delivered.length >= Math.min(100, Math.max(1, limit))) break;
      signal?.throwIfAborted();
      const file = join(this.directory, name), envelope = await this.read(file);
      if (`${this.key(envelope)}.json` !== name) throw new Error("Memory spool identity mismatch");
      if (encoded(envelope.binding) !== encoded(expected)) { blocked++; continue; }
      if (envelope.flow && !await client.canFlow(envelope.flow.read_scopes, envelope.flow.write_scope, signal)) throw new Error("Queued source audience no longer permits automatic capture");
      const receipt = envelope.target.scope_id ? await client.captureScope(envelope.target as ScopeSelection, envelope.episode, signal) : await client.capture(envelope.target.world_id, envelope.episode, signal);
      if (receipt.episode.world_id !== envelope.target.world_id || (envelope.target.scope_id && receipt.episode.scope_id !== envelope.target.scope_id) || receipt.episode.source_id !== envelope.episode.source_id || !this.sameSource(receipt.episode, envelope.episode)) throw new Error("Spool receipt does not match queued source");
      await unlink(file);
      delivered.push({ target: envelope.target, sourceId: envelope.episode.source_id, receipt });
    }
    return { delivered, blocked };
  }
}
