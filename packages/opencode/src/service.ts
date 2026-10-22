import { object, readToken, scopeKey, selection, type MemoryWorldsConfig, type ScopeSelection } from "./config.js";

const MAX_BYTES = 2_097_152;
export class MemoryWorldsError extends Error {
  constructor(message: string) { super(message); this.name = "MemoryWorldsError"; }
}
function text(value: unknown): string {
  if (typeof value !== "string") throw new MemoryWorldsError("Invalid Memory Worlds response");
  return value;
}
function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new MemoryWorldsError("Invalid Memory Worlds response");
  return value;
}
function pick(value: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  return Object.fromEntries(keys.filter(key => value[key] !== undefined).map(key => [key, value[key]]));
}
function world(value: unknown): Record<string, unknown> {
  const item = object(value);
  selection({ world_id: item.id, scope_id: item.scope_id });
  text(item.authority_id); text(item.name);
  if (!["personal", "private-work", "team", "company", "ad-hoc"].includes(text(item.kind))) throw new Error();
  return pick(item, ["id", "authority_id", "name", "kind", "alias", "scope_id", "projection_state"]);
}
function scope(value: unknown, expected: ScopeSelection): Record<string, unknown> {
  const item = object(value);
  const actual = selection({ world_id: item.world_id, scope_id: item.id });
  if (scopeKey(actual) !== scopeKey(expected)) throw new Error();
  if (!["world-shared", "run-shared", "agent-persistent", "thread-private", "invocation-private"].includes(text(item.kind))) throw new Error();
  text(item.name);
  return pick(item, ["id", "world_id", "kind", "name", "alias", "agent_id", "run_id", "thread_id", "invocation_id", "projection_state"]);
}

export class ServiceSession {
  constructor(
    private readonly config: MemoryWorldsConfig,
    private readonly credential: string,
    private readonly transport: typeof fetch,
    private readonly signal: AbortSignal,
  ) {}

  async request(path: string, body?: unknown): Promise<unknown> {
    this.signal.throwIfAborted();
    const response = await this.transport(this.config.url + path, {
      method: body === undefined ? "GET" : "POST", redirect: "error", signal: this.signal,
      headers: { Accept: "application/json", Authorization: `Bearer ${this.credential}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      await response.body?.cancel();
      throw new MemoryWorldsError(`Memory Worlds request failed (HTTP ${response.status})`);
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error();
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) throw new Error();
        chunks.push(value);
      }
    } finally { await reader.cancel(); }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  }

  async identity(): Promise<Record<string, unknown>> {
    const item = object(await this.request("/v1/me"));
    if (item.admin !== false) throw new MemoryWorldsError("Use a scoped agent credential; administrator credentials are rejected");
    for (const key of ["credential_id", "user_id", "agent_id", "authority_id"]) text(item[key]);
    return pick(item, ["credential_id", "user_id", "agent_id", "authority_id", "agent_uuid", "invocation_id", "restricted"]);
  }

  async discover(worldId?: string, includeScopes = false): Promise<unknown> {
    const visible = list(await this.request("/v1/worlds")).map(world);
    if (new Set(visible.map(item => item.id)).size !== visible.length) throw new Error();
    const worlds = worldId === undefined ? visible : visible.filter(item => item.id === worldId);
    if (worldId !== undefined && !worlds.length) throw new MemoryWorldsError("The selected world is unavailable to this credential");
    const scopes: Record<string, unknown[]> = {};
    const mounts = list(await this.request("/v1/mounts")).map(value => {
      const item = object(value);
      for (const key of ["id", "name", "authority_id"]) text(item[key]);
      list(item.world_ids).forEach(text);
      return pick(item, ["id", "name", "authority_id", "world_ids", "can_publish"]);
    });
    if (includeScopes) for (const item of worlds) {
      const id = text(item.id);
      scopes[id] = list(await this.request(`/v1/worlds/${encodeURIComponent(id)}/scopes`)).map(value => {
        const raw = object(value);
        return scope(raw, selection({ world_id: id, scope_id: raw.id }));
      });
      if (Buffer.byteLength(JSON.stringify({ worlds, scopes, mounts })) > MAX_BYTES) throw new MemoryWorldsError("Discovery is too large; select one world");
    }
    return { worlds, scopes, mounts };
  }

  async search(query: string, mode: "graph" | "source", limit: number): Promise<unknown> {
    if (!this.config.readScopes.length) throw new MemoryWorldsError("Configure readScopes before searching");
    const result = object(await this.request("/v1/search", { scopes: this.config.readScopes, query, mode, limit, include_entities: false }));
    if (result.mode !== mode) throw new Error();
    const worlds = list(result.worlds).map(world);
    const allowed = new Set(this.config.readScopes.map(scopeKey));
    const results = list(result.results).map(value => {
      const item = object(value), selected = selection(item);
      if (!allowed.has(scopeKey(selected))) throw new MemoryWorldsError("Memory Worlds returned an unselected scope");
      const authority = worlds.find(item => item.id === selected.world_id)?.authority_id;
      if (!authority) throw new Error();
      text(mode === "source" ? item.content : item.fact);
      return { ...pick(item, ["id", "source_id", "content", "fact", "fact_id", "occurred_at", "source_ref", "role", "claim_status", "graph_episode_ids", "projection_state"]), ...selected, authority_id: authority, record_type: mode === "source" ? "source" : "fact" };
    });
    return { results: results.slice(0, limit), complete: true };
  }

  async capture(payload: Record<string, unknown>, prepare: (request: Record<string, unknown>, target: ScopeSelection) => Promise<Record<string, unknown>>): Promise<unknown> {
    const selected = this.config.writeScope;
    if (!selected) throw new MemoryWorldsError("Configure a private writeScope before capture");
    const path = `/v1/worlds/${encodeURIComponent(selected.world_id)}/scopes/${selected.scope_id}`;
    const target = scope(await this.request(path), selected);
    if (!["agent-persistent", "thread-private", "invocation-private"].includes(text(target.kind))) throw new MemoryWorldsError("Private capture requires an agent-persistent, thread-private or invocation-private scope");
    const request = await prepare({ ...payload, scope_id: selected.scope_id }, selected);
    const receipt = object(await this.request(`${path}/episodes`, request));
    const episode = object(receipt.episode);
    if (scopeKey(selection(episode)) !== scopeKey(selected) || typeof receipt.replayed !== "boolean") throw new Error();
    text(episode.id); text(episode.source_id);
    return { episode: pick(episode, ["id", "world_id", "scope_id", "source_id", "occurred_at", "received_at", "agent_id", "agent_uuid", "invocation_id", "projection_state"]), replayed: receipt.replayed };
  }
}

export async function withSession<T>(config: MemoryWorldsConfig, transport: typeof fetch, signal: AbortSignal, work: (session: ServiceSession, identity: Record<string, unknown>) => Promise<T>): Promise<T> {
  try {
    const deadline = AbortSignal.any([signal, AbortSignal.timeout(10_000)]);
    deadline.throwIfAborted();
    const token = await readToken(config.tokenFile);
    deadline.throwIfAborted();
    const session = new ServiceSession(config, token, transport, deadline);
    const identity = await session.identity();
    return await work(session, identity);
  } catch (error) {
    if (signal.aborted) throw new MemoryWorldsError("Memory Worlds operation was cancelled");
    if (error instanceof MemoryWorldsError) throw error;
    throw new MemoryWorldsError("Memory Worlds operation failed; check the service, profile and private credential");
  }
}
