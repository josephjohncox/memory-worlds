import { readFile, stat } from "node:fs/promises";

export type WorldKind = "personal" | "private-work" | "team" | "company" | "ad-hoc";
export interface World {
  id: string; authority_id: string; name: string; kind: WorldKind; owner_id: string;
  created_at: string; generation?: string | null; projection_state: string;
}
export interface EpisodeInput {
  source_id: string; content: string; occurred_at: string;
  source_type?: "text" | "pi" | "publication";
  session_id?: string; source_entry_id?: string; parent_entry_id?: string | null;
  role?: string; metadata?: Record<string, unknown>; project?: boolean;
}
export interface Episode extends EpisodeInput {
  id: string; world_id: string; sequence: number; digest: string;
  received_at: string; actor_id: string; agent_id: string; projection_state: string;
}
export interface Receipt { episode: Episode; replayed: boolean }
export interface Mount { id: string; name: string; url: string; world_ids: string[]; authority_id: string; can_publish: boolean }
export interface Identity { credential_id: string; user_id: string; agent_id: string; admin: boolean; authority_id: string; worlds: World[] }
export interface SearchHit {
  mount_id?: string; authority_id: string; world_id: string; record_type: "source" | "fact";
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
  content: string; occurred_at: string; project: false;
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
  if (!["personal", "private-work", "team", "company", "ad-hoc"].includes(item.kind as string)) throw new Error("Invalid world kind");
  return item as unknown as World;
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

  private async request(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
    const deadline = AbortSignal.timeout(this.timeoutMs);
    const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
    combined.throwIfAborted();
    const token = await this.token();
    combined.throwIfAborted();
    const response = await this.transport(`${this.url}${path}`, {
      method: body === undefined ? "GET" : "POST", redirect: "error", signal: combined,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      await response.body?.cancel();
      throw new MemoryWorldsError(response.status, `http_${response.status}`);
    }
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

  async worlds(signal?: AbortSignal): Promise<World[]> {
    return array(await this.request("/v1/worlds", undefined, signal)).map(world);
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
  async capture(worldId: string, episode: EpisodeInput, signal?: AbortSignal): Promise<Receipt> {
    const value = object(await this.request(`/v1/worlds/${encodeURIComponent(worldId)}/episodes`, episode, signal));
    string(object(value.episode).id);
    return value as unknown as Receipt;
  }
  async search(worldIds: string[], mountIds: string[], query: string, mode: "source" | "graph", limit: number, signal?: AbortSignal): Promise<SearchResult> {
    if (!query.trim() || query.length > 2000 || !Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error("Invalid search query or limit");
    if (worldIds.length > 50 || mountIds.length > 20) throw new Error("Search selection exceeds world/mount limits");
    const tasks: Promise<SearchResult>[] = [];
    if (worldIds.length) tasks.push((async () => {
      const result = object(await this.request("/v1/search", { world_ids: worldIds, query, mode, limit }, signal));
      if (result.mode !== mode) throw new Error("Search response mode does not match the requested source/graph mode");
      const worlds = array(result.worlds).map(world);
      const results = array(result.results).map(item => {
        const hit = object(item);
        const worldId = string(hit.world_id);
        if (!worldIds.includes(worldId)) throw new Error("Search returned a world outside the selected scope");
        string(mode === "source" ? hit.content : hit.fact);
        const authority = worlds.find(item => item.id === worldId)?.authority_id;
        if (!authority) throw new Error("Search returned a world outside the selected scope");
        return { ...hit, authority_id: authority, record_type: mode === "source" ? "source" : "fact" } as SearchHit;
      });
      return { results, failures: [], complete: true };
    })());
    if (mountIds.length) tasks.push((async () => {
      const response = object(await this.request("/v1/router/search", { mount_ids: mountIds, query, mode, limit }, signal));
      const results = array(response.results).map(value => {
        const hit = object(value);
        string(hit.authority_id); string(hit.world_id);
        if (!mountIds.includes(string(hit.mount_id))) throw new Error("Unexpected remote mount in search response");
        if (hit.record_type !== (mode === "source" ? "source" : "fact")) throw new Error("Remote search response mode mismatch");
        string(mode === "source" ? hit.content : hit.fact);
        return hit as unknown as SearchHit;
      });
      const failures = array(response.failures).map(value => {
        const item = object(value);
        return { mount_id: string(item.mount_id), error: string(item.error) };
      });
      return { results, failures, complete: response.complete === true };
    })());
    const results = await Promise.allSettled(tasks);
    signal?.throwIfAborted();
    const combined: SearchResult = { results: [], failures: [], complete: true };
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        combined.failures.push({ mount_id: worldIds.length && index === 0 ? "local" : "router", error: "unavailable" });
        combined.complete = false;
      } else {
        combined.results.push(...result.value.results);
        combined.failures.push(...result.value.failures);
        combined.complete &&= result.value.complete;
      }
    });
    // Authorities retain their own ordering; scores are not comparable across services.
    combined.results = combined.results.slice(0, limit);
    return combined;
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
