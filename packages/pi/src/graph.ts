import { isPrivateScope, scopeKey, type MemoryWorldsClient, type ScopeSelection, type SearchHit } from "./client.js";

/** Native graph actions; project/global aliases are explicit capability bindings. */
export interface GraphArguments {
  action: "add" | "search" | "episodes"; content?: string; name?: string;
  source?: "text" | "message" | "json"; query?: string; limit?: number;
  scope?: "project" | "global" | "both";
}
export interface GraphBindings { project?: ScopeSelection; global?: ScopeSelection; writeScope?: ScopeSelection; contextId?: string }
export interface GraphResponse {
  success: true; scope: "project" | "global" | "both"; message?: string;
  entities?: string[]; facts?: string[]; episodes?: string[]; records: SearchHit[];
  scopes: ScopeSelection[]; entityUuids?: { label: string; uuid: string }[];
  factUuids?: { label: string; uuid: string }[]; episodeUuids?: { label: string; uuid: string }[];
  episodeId?: string; replayed?: boolean;
}
export function graphLimit(value?: number): number {
  if (value !== undefined && !Number.isFinite(value)) throw new Error("Invalid graph limit");
  return Math.min(20, Math.max(1, Math.floor(value ?? 5)));
}
export function graphSelections(bindings: GraphBindings, selected: GraphArguments["scope"] = "project"): ScopeSelection[] {
  const names = selected === "both" ? ["project", "global"] as const : [selected] as const;
  const scopes = names.map(name => {
    const binding = bindings[name];
    if (!binding) throw new Error(`Graph ${name} alias is unbound; select an explicit world and scope`);
    return binding;
  });
  return [...new Map(scopes.map(scope => [scopeKey(scope), scope])).values()];
}
/** No cwd inference, raw group identifiers, ambient transcript or publication side effects. */
export async function executeGraph(
  client: MemoryWorldsClient, input: GraphArguments, bindings: GraphBindings,
  invocation: { sourceId: string; occurredAt: string; contextId?: string | null }, signal?: AbortSignal,
): Promise<GraphResponse> {
  const selected = input.scope ?? (input.action === "search" ? "both" : "project"), scopes = graphSelections(bindings, selected), limit = graphLimit(input.limit);
  if (input.action === "search") {
    if (!input.query?.trim()) throw new Error("Graph search requires query");
    const result = await client.searchScopes(scopes, input.query, "graph", limit, signal, true);
    const entities = result.results.filter(hit => hit.record_type === "entity"), facts = result.results.filter(hit => hit.record_type === "fact");
    return { success: true, scope: selected, scopes, records: result.results,
      entities: entities.map(hit => `${hit.name ?? ""}${hit.summary ? `: ${hit.summary}` : ""}`), facts: facts.map(hit => hit.fact ?? ""),
      entityUuids: entities.map(hit => ({ label: hit.name ?? "", uuid: hit.entity_id ?? hit.id ?? "" })),
      factUuids: facts.map(hit => ({ label: hit.fact ?? "", uuid: hit.fact_id ?? hit.id ?? "" })),
    };
  }
  if (input.action === "episodes") {
    const [identity, batches] = await Promise.all([client.identity(signal), Promise.all(scopes.map(scope => client.episodes(scope, limit, signal)))]);
    const episodes = batches.flat().slice(0, limit);
    return { success: true, scope: selected, scopes, records: episodes.map(episode => ({ ...episode, authority_id: identity.authority_id, record_type: "source" })),
      episodes: episodes.map(episode => episode.content), episodeUuids: episodes.map(episode => ({ label: episode.content.slice(0, 100), uuid: episode.id })),
    };
  }
  if (selected === "both" || scopes.length !== 1) throw new Error("Graph add requires exactly one destination");
  if (!bindings.writeScope || scopeKey(bindings.writeScope) !== scopeKey(scopes[0]!)) throw new Error("Graph add requires the configured private capture target; use explicit publication for shared scopes");
  const target = await client.scope(scopes[0]!, signal);
  if (!isPrivateScope(target.kind)) throw new Error("Graph add requires a private agent, thread or invocation scope");
  if (!input.content?.trim() || input.content.length > 200000) throw new Error("Graph add requires content of 1..200000 characters");
  if (input.source === "json") { try { JSON.parse(input.content); } catch { throw new Error("Graph JSON source must contain valid JSON"); } }
  const receipt = await client.captureScope(scopes[0]!, {
    context_id: "contextId" in invocation ? invocation.contextId ?? undefined : bindings.contextId, source_id: invocation.sourceId, occurred_at: invocation.occurredAt, content: input.content,
    source_type: input.source === "message" ? "pi" : "text", project: true,
    metadata: { ...(input.name ? { name: input.name.slice(0, 200) } : {}), graphiti_source: input.source ?? "text" },
  }, signal);
  return { success: true, scope: selected, scopes, records: [], message: "Stored private source; graph extraction follows the selected scope's projection policy", episodeId: receipt.episode.id, replayed: receipt.replayed };
}
