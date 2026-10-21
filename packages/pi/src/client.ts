import { readFile, stat } from "node:fs/promises";
export { DirectoryImportError, planDirectory, importDirectory, type DirectoryFormat, type DirectoryOptions, type DirectoryPlan, type DirectoryReport } from "./imports.js";

export type WorldKind = "personal" | "private-work" | "team" | "company" | "ad-hoc";
export interface World {
  id: string; authority_id: string; name: string; kind: WorldKind; owner_id: string;
  alias?: string | null;
  created_at: string; generation?: string | null; projection_state: string; scope_id: string;
}
export interface EpisodeInput {
  context_id?: string | null; scope_id?: string | null; source_id: string; content: string; occurred_at: string;
  source_type?: "text" | "chatgpt" | "claude" | "pi" | "publication";
  session_id?: string | null; source_entry_id?: string | null; parent_entry_id?: string | null;
  source_ref?: string | null; role?: string | null; metadata?: Record<string, unknown>; project?: boolean;
}
export interface Episode extends EpisodeInput {
  scope_id: string; id: string; world_id: string; sequence: number; digest: string;
  agent_uuid?: string; invocation_id?: string; parent_invocation_id?: string; received_at: string; actor_id: string; agent_id: string; projection_state: string;
}
export interface Receipt { episode: Episode; replayed: boolean }
export interface Mount { id: string; name: string; url: string; world_ids: string[]; authority_id: string; can_publish: boolean }
export interface Identity { credential_id: string; user_id: string; agent_id: string; admin: boolean; restricted?: boolean; agent_uuid?: string; invocation_id?: string; parent_invocation_id?: string; authority_id: string; worlds: World[] }
export interface SearchHit {
  mount_id?: string; authority_id: string; world_id: string; record_type: "source" | "fact" | "entity"; scope_id: string; generation?: string; entity_id?: string; name?: string; summary?: string;
  id?: string; source_id?: string; content?: string; fact?: string; fact_id?: string;
  occurred_at?: string; role?: string | null; projection_state?: string;
  claim_status?: string; graph_episode_ids?: string[];
}
export interface SearchResult {
  results: SearchHit[]; failures: { mount_id: string; error: string }[]; complete: boolean;
}
export interface Delivery {
  id: string; state: "pending" | "delivered" | "blocked";
  destination_mount_id: string; destination_authority_id: string; world_id: string; source_id: string;
  user_id: string; agent_id: string; attempts: number; created_at: string;
  delivered_at?: string | null; error?: string | null; remote_episode_id?: string | null;
}
export interface Publication {
  destination_mount_id: string; world_id: string; source_id: string;
  content: string; occurred_at: string; project: boolean;
}

export type ScopeKind = "world-shared" | "run-shared" | "agent-persistent" | "invocation-private";
export type ScopeOperation = "read" | "write" | "publish" | "delegate" | "manage";
export interface ScopeSelection { world_id: string; scope_id: string }
export interface Scope extends ScopeSelection {
  id: string; kind: ScopeKind; name: string; created_at: string;
  alias?: string | null;
  run_id?: string | null; agent_id?: string | null; invocation_id?: string | null;
  parent_invocation_id?: string | null; coordinator_credential_id?: string | null; generation?: string | null; projection_state: string;
}
export interface ScopeGrant extends ScopeSelection { operations: ScopeOperation[] }
export interface DiscoveryOptions { world?: string; includeScopes?: boolean }
export interface DiscoveryResult { worlds: World[]; scopes: Record<string, Scope[]>; mounts: Mount[] }
export type IdentityKind = "agent" | "run" | "invocation";
export interface NamedIdentity {
  id: string; kind: IdentityKind; owner_id: string; name: string; alias: string | null; created_at: string;
  parent_id?: string | null; agent_id?: string | null; run_id?: string | null;
}
export interface IdentityInput { id?: string; name: string; alias?: string | null; parent_id?: string; agent_id?: string; run_id?: string }
export interface ImportResult { imported: number; replayed: number }
export interface ContextInput { read_scopes: ScopeSelection[]; write_scope?: ScopeSelection | null; agent_id?: string; run_id?: string }
export interface AgentContext extends ContextInput {
  id: string; credential_id: string; invocation_id: string; parent_invocation_id?: string | null; created_at: string;
}
export interface DelegationInput {
  grants: ScopeGrant[]; private_world_id?: string; agent_id?: string; run_id?: string;
  expires_at?: string; allow_delegation?: boolean; parent_context_id?: string; coordinator_read?: boolean;
}
/** Sensitive. Pass straight to a closure; never serialize to tool results or session entries. */
export interface DelegationReceipt { token: string; credential_id: string; context: AgentContext; private_scope: Scope | null }
export interface ScopedPublication extends Omit<EpisodeInput, "source_type" | "session_id" | "source_entry_id" | "parent_entry_id" | "role" | "metadata" | "scope_id"> {
  source_scope?: ScopeSelection; source_episode_id?: string;
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ALIAS = /^[a-z][a-z0-9_-]{0,63}$/;
export class MemoryWorldsReferenceError extends Error { constructor(message: string) { super(message); this.name = "MemoryWorldsReferenceError"; } }
export function aliasReference(value: string): boolean { return value.startsWith("@"); }
function checkedAlias(value: string | null): string | null {
  if (value !== null && !ALIAS.test(value)) throw new MemoryWorldsReferenceError("Alias must be lowercase letters, digits, underscores or hyphens, starting with a letter");
  return value;
}
function aliasMatch<T extends { alias?: string | null }>(items: T[], reference: string): T {
  checkedAlias(reference.slice(1));
  const matches = items.filter(item => item.alias === reference.slice(1));
  if (matches.length !== 1) throw new MemoryWorldsReferenceError(matches.length ? "Alias is ambiguous; select a canonical ID" : "No visible object matches that alias");
  return matches[0]!;
}
function identityKind(value: string): IdentityKind {
  if (!["agent", "run", "invocation"].includes(value)) throw new Error("Invalid identity kind");
  return value as IdentityKind;
}
function namedIdentity(value: unknown, kind: IdentityKind): NamedIdentity {
  const item = object(value);
  if (!UUID.test(string(item.id)) || item.kind !== kind || !string(item.name).trim() || !string(item.owner_id) || !Number.isFinite(Date.parse(string(item.created_at)))) throw new Error("Invalid named identity response");
  if (item.alias !== undefined && item.alias !== null) checkedAlias(string(item.alias));
  for (const key of ["parent_id", "agent_id", "run_id"]) if (item[key] !== undefined && item[key] !== null && !UUID.test(string(item[key]))) throw new Error("Invalid linked identity");
  return item as unknown as NamedIdentity;
}
export function scopeSelection(value: unknown): ScopeSelection {
  const item = object(value);
  const worldId = string(item.world_id), scopeId = string(item.scope_id);
  if (!/^[a-zA-Z0-9_-]{1,100}$/.test(worldId) || !UUID.test(scopeId)) throw new Error("Invalid scope selection");
  return { world_id: worldId, scope_id: scopeId.toLowerCase() };
}
/** User references are resolved live before becoming persisted scope selections. */
export function scopeReferenceSelection(value: unknown): ScopeSelection {
  const item = object(value), worldId = string(item.world_id), scopeId = string(item.scope_id);
  if (aliasReference(worldId)) checkedAlias(worldId.slice(1));
  else if (!/^[a-zA-Z0-9_-]{1,100}$/.test(worldId)) throw new Error("Invalid world reference");
  if (aliasReference(scopeId)) checkedAlias(scopeId.slice(1));
  else if (!UUID.test(scopeId)) throw new Error("Invalid scope reference");
  return { world_id: worldId, scope_id: aliasReference(scopeId) ? scopeId : scopeId.toLowerCase() };
}
export function scopeKey(scope: ScopeSelection): string { return `${scope.world_id}/${scope.scope_id}`; }
function scope(value: unknown): Scope {
  const item = object(value); const id = string(item.id);
  scopeSelection({ world_id: item.world_id, scope_id: id });
  if (!["world-shared", "run-shared", "agent-persistent", "invocation-private"].includes(String(item.kind))) throw new Error("Invalid scope kind");
  if (item.alias !== undefined && item.alias !== null) checkedAlias(string(item.alias));
  return { ...item, scope_id: id } as unknown as Scope;
}
function context(value: unknown): AgentContext {
  const item = object(value);
  if (!UUID.test(string(item.id)) || !UUID.test(string(item.invocation_id))) throw new Error("Invalid context identity");
  const reads = array(item.read_scopes).map(scopeSelection);
  const write = item.write_scope ? scopeSelection(item.write_scope) : null;
  return { ...item, read_scopes: reads, write_scope: write } as unknown as AgentContext;
}

export class MemoryWorldsError extends Error {
  constructor(readonly status: number, readonly code: string) {
    // Do not return upstream bodies, URLs or headers: they may contain private input.
    super(`Memory Worlds request failed (${status}, ${code})`);
    this.name = "MemoryWorldsError";
  }
}

export function serviceURL(value: string): string {
  const url = new URL(value);
  if (url.username || url.password || url.search || url.hash) throw new Error("Service URL cannot contain credentials, query or fragment");
  if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) {
    throw new Error("Memory Worlds requires HTTPS or loopback HTTP (including SSH tunnels)");
  }
  return url.toString().replace(/\/$/, "");
}

export async function readTokenFile(path: string): Promise<string> {
  const info = await stat(path);
  if (!info.isFile() || info.size > 8192 || (process.platform !== "win32" && (info.mode & 0o077))) {
    throw new Error("Memory Worlds token must be a private regular file (chmod 600)");
  }
  const token = (await readFile(path, "utf8")).trim();
  if (!token || /\s/.test(token)) throw new Error("Memory Worlds token file must contain one bearer token");
  return token;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid Memory Worlds response");
  return value as Record<string, unknown>;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error("Invalid Memory Worlds response");
  return value;
}
function string(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid Memory Worlds response");
  return value;
}
function world(value: unknown): World {
  const item = object(value);
  for (const key of ["id", "authority_id", "name", "kind", "owner_id"]) string(item[key]);
  scopeSelection({ world_id: item.id, scope_id: item.scope_id });
  if (!["personal", "private-work", "team", "company", "ad-hoc"].includes(item.kind as string)) throw new Error("Invalid world kind");
  if (item.alias !== undefined && item.alias !== null) checkedAlias(string(item.alias));
  return item as unknown as World;
}
function metadata<T extends object>(value: T, keys: readonly (keyof T)[]): T {
  return Object.fromEntries(keys.filter(key => value[key] !== undefined).map(key => [key, value[key]])) as T;
}

/** Direct authenticated HTTP client. No MCP, graph ports, session files or storage ownership. */
export class MemoryWorldsClient {
  readonly url: string;
  constructor(
    url: string,
    private readonly token: () => Promise<string>,
    private readonly transport: typeof fetch = fetch,
    readonly timeoutMs = 5000,
  ) { this.url = serviceURL(url); }

  private async request(path: string, body?: unknown, signal?: AbortSignal, method?: "DELETE" | "PUT" | "PATCH", timeoutMs = this.timeoutMs): Promise<unknown> {
    const deadline = AbortSignal.timeout(timeoutMs);
    const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
    combined.throwIfAborted();
    const token = await this.token();
    combined.throwIfAborted();
    const response = await this.transport(`${this.url}${path}`, {
      method: method ?? (body === undefined ? "GET" : "POST"), redirect: "error", signal: combined,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      await response.body?.cancel();
      throw new MemoryWorldsError(response.status, `http_${response.status}`);
    }
    if (response.status === 204) return undefined;
    const reader = response.body?.getReader();
    if (!reader) throw new Error("Empty Memory Worlds response");
    let size = 0;
    const chunks: Uint8Array[] = [];
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 2_097_152) throw new Error("Memory Worlds response exceeds 2 MiB");
        chunks.push(value);
      }
    } finally { await reader.cancel(); }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  }

  /** Snapshot a rotating token for one operation; erase the closure when the operation ends. */
  async withCredentialSession<T>(work: (client: MemoryWorldsClient) => Promise<T>, signal?: AbortSignal): Promise<T> {
    signal?.throwIfAborted();
    let snapshot: string | undefined = await this.token();
    const pinned = new MemoryWorldsClient(this.url, async () => {
      if (!snapshot) throw new Error("Credential session has ended");
      return snapshot;
    }, this.transport, this.timeoutMs);
    try { signal?.throwIfAborted(); return await work(pinned); }
    finally { snapshot = undefined; }
  }
  async worlds(signal?: AbortSignal): Promise<World[]> {
    return array(await this.request("/v1/worlds", undefined, signal)).map(world);
  }
  /** Read visible metadata with one credential; selection and capture remain unchanged. */
  async discover(options: DiscoveryOptions = {}, signal?: AbortSignal): Promise<DiscoveryResult> {
    options = { ...options };
    if (options.includeScopes !== undefined && typeof options.includeScopes !== "boolean") throw new MemoryWorldsReferenceError("includeScopes must be a boolean");
    if (options.world !== undefined && (typeof options.world !== "string" || !options.world.length || options.world.length > 100)) throw new MemoryWorldsReferenceError("Invalid world reference");
    try { return await this.withCredentialSession(async client => {
      let worlds = (await client.worlds(signal)).map(item => metadata(item, ["id", "authority_id", "name", "kind", "owner_id", "alias", "created_at", "generation", "projection_state", "scope_id"]));
      if (options.world !== undefined) {
        const id = aliasReference(options.world) ? aliasMatch(worlds, options.world).id : await client.resolveWorld(options.world, signal);
        worlds = worlds.filter(item => item.id === id);
        if (!worlds.length) throw new MemoryWorldsReferenceError("No visible world matches that selection");
      }
      const mounts = (await client.mounts(signal)).map(item => metadata(item, ["id", "name", "url", "world_ids", "authority_id", "can_publish"])), scopes: [string, Scope[]][] = [];
      let bytes = Buffer.byteLength(JSON.stringify(worlds)) + Buffer.byteLength(JSON.stringify(mounts)) + 64;
      const bounded = () => { if (bytes > 2_097_152) throw new MemoryWorldsReferenceError("Discovery exceeds 2 MiB; select one world or omit scopes"); };
      bounded();
      if (options.includeScopes !== false) for (const world of worlds) {
        const visible = (await client.scopes(world.id, signal)).map(item => metadata(item, ["id", "world_id", "scope_id", "kind", "name", "alias", "created_at", "run_id", "agent_id", "invocation_id", "parent_invocation_id", "coordinator_credential_id", "generation", "projection_state"]));
        if (visible.some(scope => scope.world_id !== world.id) || new Set(visible.map(scope => scope.id.toLowerCase())).size !== visible.length) throw new MemoryWorldsError(502, "invalid_discovery_metadata");
        bytes += Buffer.byteLength(JSON.stringify(visible)) + Buffer.byteLength(JSON.stringify(world.id)) + 4;
        bounded(); scopes.push([world.id, visible]);
      }
      return { worlds, scopes: Object.fromEntries(scopes), mounts };
    }, signal); } catch (error) {
      if (signal?.aborted) throw new DOMException("Memory Worlds discovery was cancelled", "AbortError");
      if (error instanceof MemoryWorldsError || error instanceof MemoryWorldsReferenceError) throw error;
      // Token readers, fetch implementations and JSON parsers can include private
      // paths, credentials or response snippets in their diagnostics and causes.
      throw new Error("Memory Worlds discovery failed; verify service and current credential");
    }
  }
  async resolveWorld(reference: string, signal?: AbortSignal): Promise<string> {
    if (aliasReference(reference)) return aliasMatch(await this.worlds(signal), reference).id;
    if (!/^[a-zA-Z0-9_-]{1,100}$/.test(reference)) throw new MemoryWorldsReferenceError("Invalid world reference");
    return reference;
  }
  async resolveScope(worldId: string, reference: string, signal?: AbortSignal): Promise<string> {
    if (aliasReference(reference)) return aliasMatch(await this.scopes(worldId, signal), reference).id;
    if (!UUID.test(reference)) throw new MemoryWorldsReferenceError("Scope reference requires a UUID or @alias");
    return reference.toLowerCase();
  }
  async resolveSelection(worldReference: string, scopeReference?: string, signal?: AbortSignal): Promise<ScopeSelection> {
    return this.withCredentialSession(async client => {
      const worldId = await client.resolveWorld(worldReference, signal);
      const scopeId = scopeReference === undefined ? (await client.scopes(worldId, signal)).find(item => item.kind === "world-shared")?.id : await client.resolveScope(worldId, scopeReference, signal);
      if (!scopeId) throw new Error("No visible world-shared scope exists");
      const selected = scopeSelection({ world_id: worldId, scope_id: scopeId });
      await client.scope(selected, signal);
      return selected;
    }, signal);
  }
  async setWorldAlias(worldId: string, alias: string | null, signal?: AbortSignal): Promise<World> {
    const result = world(await this.request(`/v1/worlds/${encodeURIComponent(worldId)}/alias`, { alias: checkedAlias(alias) }, signal, "PUT"));
    if (result.id !== worldId) throw new Error("Alias response does not match world");
    return result;
  }
  async setScopeAlias(selection: ScopeSelection, alias: string | null, signal?: AbortSignal): Promise<Scope> {
    const result = scope(await this.request(`${this.scopePath(selection)}/alias`, { alias: checkedAlias(alias) }, signal, "PUT"));
    if (scopeKey(result) !== scopeKey(scopeSelection(selection))) throw new Error("Alias response does not match scope");
    return result;
  }
  async identities(kind: IdentityKind, signal?: AbortSignal): Promise<NamedIdentity[]> {
    return array(await this.request(`/v1/identities/${identityKind(kind)}`, undefined, signal)).map(item => namedIdentity(item, kind));
  }
  async namedIdentity(kind: IdentityKind, id: string, signal?: AbortSignal): Promise<NamedIdentity> {
    if (!UUID.test(id)) throw new Error("Identity requires a UUID");
    const result = namedIdentity(await this.request(`/v1/identities/${identityKind(kind)}/${id}`, undefined, signal), kind);
    if (result.id !== id.toLowerCase()) throw new Error("Identity response does not match selection");
    return result;
  }
  async resolveIdentity(kind: IdentityKind, reference: string, signal?: AbortSignal): Promise<NamedIdentity> {
    if (aliasReference(reference)) checkedAlias(reference.slice(1));
    else if (!UUID.test(reference)) throw new MemoryWorldsReferenceError("Identity reference requires a registered UUID or @alias");
    const result = namedIdentity(await this.request(`/v1/identities/${identityKind(kind)}/resolve?reference=${encodeURIComponent(reference)}`, undefined, signal), kind);
    if (aliasReference(reference) ? result.alias !== reference.slice(1) : result.id !== reference.toLowerCase()) throw new Error("Identity resolution did not match reference");
    return result;
  }
  async createIdentity(kind: IdentityKind, input: IdentityInput, signal?: AbortSignal): Promise<NamedIdentity> {
    return namedIdentity(await this.request(`/v1/identities/${identityKind(kind)}`, structuredClone(input), signal), kind);
  }
  async updateIdentity(kind: IdentityKind, id: string, input: { name?: string; alias?: string | null }, signal?: AbortSignal): Promise<NamedIdentity> {
    if (!UUID.test(id)) throw new Error("Identity requires a UUID");
    const result = namedIdentity(await this.request(`/v1/identities/${identityKind(kind)}/${id}`, structuredClone(input), signal, "PATCH"), kind);
    if (result.id !== id.toLowerCase()) throw new Error("Identity response does not match selection");
    return result;
  }
  async identity(signal?: AbortSignal): Promise<Identity> {
    const value = object(await this.request("/v1/me", undefined, signal));
    string(value.authority_id); string(value.user_id); string(value.agent_id);
    return { ...value, worlds: array(value.worlds).map(world) } as unknown as Identity;
  }
  async mounts(signal?: AbortSignal): Promise<Mount[]> {
    return array(await this.request("/v1/mounts", undefined, signal)).map(value => {
      const item = object(value);
      string(item.id); string(item.name); string(item.url); array(item.world_ids).map(string);
      return item as unknown as Mount;
    });
  }
  async searchMounts(mountIds: string[], query: string, mode: "source" | "graph", limit: number, signal?: AbortSignal): Promise<SearchResult> {
    if (!query.trim() || query.length > 2000 || !Number.isInteger(limit) || limit < 1 || limit > 100 || mountIds.length > 20) throw new Error("Invalid remote search selection, query or limit");
    if (!mountIds.length) return { results: [], failures: [], complete: true };
    try {
      const response = object(await this.request("/v1/router/search", { mount_ids: mountIds, query, mode, limit }, signal));
      const results = array(response.results).map(value => {
        const hit = object(value); scopeSelection(hit);
        string(hit.authority_id);
        if (!mountIds.includes(string(hit.mount_id))) throw new Error("Unexpected remote mount in search response");
        if (hit.record_type !== (mode === "source" ? "source" : "fact")) throw new Error("Remote search response mode mismatch");
        string(mode === "source" ? hit.content : hit.fact);
        return hit as unknown as SearchHit;
      });
      const failures = array(response.failures).map(value => {
        const item = object(value);
        return { mount_id: string(item.mount_id), error: string(item.error) };
      });
      return { results: results.slice(0, limit), failures, complete: response.complete === true };
    } catch {
      signal?.throwIfAborted();
      return { results: [], failures: [{ mount_id: "router", error: "unavailable" }], complete: false };
    }
  }
  async scopes(worldId: string, signal?: AbortSignal): Promise<Scope[]> {
    return array(await this.request(`/v1/worlds/${encodeURIComponent(worldId)}/scopes`, undefined, signal)).map(scope);
  }
  async scope(selection: ScopeSelection, signal?: AbortSignal): Promise<Scope> {
    const selected = scopeSelection(selection);
    const result = scope(await this.request(this.scopePath(selected), undefined, signal));
    if (scopeKey(result) !== scopeKey(selected)) throw new Error("Scope response does not match selection");
    return result;
  }
  private scopePath(selection: ScopeSelection): string {
    const selected = scopeSelection(selection);
    return `/v1/worlds/${encodeURIComponent(selected.world_id)}/scopes/${selected.scope_id}`;
  }
  async createScope(worldId: string, input: { kind: ScopeKind; name: string; alias?: string; run_id?: string; agent_id?: string; invocation_id?: string }, signal?: AbortSignal): Promise<Scope> {
    return scope(await this.request(`/v1/worlds/${encodeURIComponent(worldId)}/scopes`, input, signal));
  }
  async canFlow(readScopes: ScopeSelection[], writeScope: ScopeSelection, signal?: AbortSignal): Promise<boolean> {
    const response = object(await this.request("/v1/scopes/flow", { read_scopes: readScopes.map(scopeSelection), write_scope: scopeSelection(writeScope) }, signal));
    if (typeof response.allowed !== "boolean") throw new Error("Invalid scope flow response");
    return response.allowed;
  }
  async context(contextId: string, signal?: AbortSignal): Promise<AgentContext> {
    const result = context(await this.request(`/v1/contexts/${encodeURIComponent(contextId)}`, undefined, signal));
    if (result.id !== contextId) throw new Error("Context response identity mismatch");
    return result;
  }
  async bindContext(input: ContextInput, signal?: AbortSignal): Promise<AgentContext> {
    return context(await this.request("/v1/contexts", input, signal));
  }
  async delegate(input: DelegationInput, signal?: AbortSignal): Promise<DelegationReceipt> {
    const response = object(await this.request("/v1/credentials/delegate", input, signal));
    const credentialId = typeof response.credential_id === "string" ? response.credential_id : undefined;
    try {
      const token = string(response.token);
      if (!token || /\s/.test(token) || !credentialId) throw new Error("Invalid delegated credential");
      return { token, credential_id: credentialId, context: context(response.context), private_scope: response.private_scope ? scope(response.private_scope) : null };
    } catch {
      if (credentialId) await this.revokeCredential(credentialId);
      throw new Error("Invalid delegation response; issued credential revoked when identifiable");
    }
  }
  async revokeCredential(credentialId: string, signal?: AbortSignal): Promise<void> {
    await this.request(`/v1/credentials/${encodeURIComponent(credentialId)}`, undefined, signal, "DELETE");
  }
  async captureScope(selection: ScopeSelection, episode: EpisodeInput, signal?: AbortSignal): Promise<Receipt> {
    const response = object(await this.request(`${this.scopePath(selection)}/episodes`, episode, signal));
    const item = object(response.episode);
    if (item.world_id !== selection.world_id || item.scope_id !== selection.scope_id) throw new Error("Capture receipt does not match selected scope");
    string(item.id);
    return response as unknown as Receipt;
  }
  /** One atomic private-scope import. Inputs never select another destination or context. */
  async importEpisodes(selection: ScopeSelection, episodes: readonly EpisodeInput[], signal?: AbortSignal): Promise<ImportResult> {
    const selected = scopeSelection(selection);
    if (!episodes.length || episodes.length > 10_000) throw new Error("An import requires 1..10000 episodes");
    const inputs = structuredClone(episodes).map(item => {
      if (item.context_id || item.scope_id && item.scope_id !== selected.scope_id || item.source_type === "publication") throw new Error("Import source cannot select a context, publication or another scope");
      return { ...item, scope_id: selected.scope_id };
    });
    if (Buffer.byteLength(JSON.stringify({ episodes: inputs }), "utf8") > 32 * 1024 * 1024) throw new Error("Import request exceeds 32 MiB");
    const response = object(await this.request(`${this.scopePath(selected)}/imports`, { episodes: inputs }, signal, undefined, Math.max(this.timeoutMs, 30_000)));
    if (!Number.isInteger(response.imported) || !Number.isInteger(response.replayed) || Number(response.imported) < 0 || Number(response.replayed) < 0 || Number(response.imported) + Number(response.replayed) !== inputs.length) throw new Error("Invalid import receipt");
    return { imported: Number(response.imported), replayed: Number(response.replayed) };
  }
  async episodes(selection: ScopeSelection, limit = 5, signal?: AbortSignal): Promise<Episode[]> {
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("Invalid episode limit");
    return array(await this.request(`${this.scopePath(selection)}/episodes?limit=${limit}`, undefined, signal)).map(value => {
      const item = object(value);
      if (item.world_id !== selection.world_id || item.scope_id !== selection.scope_id) throw new Error("Episode outside selected scope");
      string(item.id); string(item.content);
      return item as unknown as Episode;
    });
  }
  /** Fail closed: an unauthorized member of a selected scope set rejects the entire request. */
  async searchScopes(selections: ScopeSelection[], query: string, mode: "source" | "graph", limit: number, signal?: AbortSignal, includeEntities = false): Promise<SearchResult> {
    if (!selections.length || selections.length > 50 || !query.trim() || query.length > 2000 || !Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("Invalid scoped search");
    const scopes = selections.map(scopeSelection), allowed = new Set(scopes.map(scopeKey));
    const result = object(await this.request("/v1/search", { scopes, query, mode, limit, include_entities: includeEntities }, signal));
    if (result.mode !== mode) throw new Error("Search response mode mismatch");
    const worlds = array(result.worlds).map(world);
    const results = array(result.results).map(value => {
      const hit = object(value);
      const selected = scopeSelection(hit);
      if (!allowed.has(scopeKey(selected))) throw new Error("Search returned an unselected scope");
      const authority = worlds.find(world => world.id === selected.world_id)?.authority_id;
      if (!authority) throw new Error("Missing world authority");
      const recordType = mode === "source" ? "source" : hit.record_type === "entity" ? "entity" : "fact";
      string(recordType === "source" ? hit.content : recordType === "entity" ? hit.name : hit.fact);
      return { ...hit, ...selected, authority_id: authority, record_type: recordType } as SearchHit;
    });
    return { results, failures: [], complete: true };
  }
  async publishScope(destination: ScopeSelection, publication: ScopedPublication, signal?: AbortSignal): Promise<Receipt> {
    const response = object(await this.request(`${this.scopePath(destination)}/publications`, publication, signal));
    const item = object(response.episode);
    if (item.world_id !== destination.world_id || item.scope_id !== destination.scope_id) throw new Error("Publication receipt does not match destination scope");
    string(item.id);
    return response as unknown as Receipt;
  }
  async publish(publication: Publication, signal?: AbortSignal): Promise<Delivery> {
    const value = object(await this.request("/v1/router/publications", publication, signal));
    string(value.id);
    return value as unknown as Delivery;
  }
  async deliveries(signal?: AbortSignal): Promise<Delivery[]> {
    return array(await this.request("/v1/router/deliveries", undefined, signal)) as Delivery[];
  }
}
