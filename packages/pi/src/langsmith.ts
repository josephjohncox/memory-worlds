import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Correlation identifiers only. Names, prompts, tokens and URLs do not belong here. */
export interface MemoryTraceCorrelation {
  authority_id?: string; world_id?: string; scope_id?: string; context_id?: string;
  agent_id?: string; run_id?: string; invocation_id?: string; episode_id?: string;
  execution_id?: string; parent_invocation_id?: string;
  source_scope_id?: string; destination_scope_id?: string;
  source_world_id?: string; destination_world_id?: string;
}
export interface LangSmithPiTracingOptions {
  endpoint: string;
  apiKey: string;
  project?: string;
  workspaceId?: string;
  mode?: "ids-only" | "selected-content";
  correlation?: MemoryTraceCorrelation;
  /** Explicitly selected excerpts, never the session transcript or raw tool result. */
  selectedContent?: Readonly<Record<string, string>>;
  /** The `langsmith-trace` header only. Baggage and replica destinations are excluded. */
  parentTrace?: string;
  fetchImplementation?: typeof fetch;
}
export interface PiTracing {
  extensionFactory(pi: ExtensionAPI): Promise<void>;
  flush(): Promise<void>;
}

const uuid = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const uuidValue = new RegExp(`^${uuid}$`);
const uuidFields = new Set(["scope_id", "context_id", "agent_id", "run_id", "invocation_id", "execution_id", "parent_invocation_id", "source_scope_id", "destination_scope_id"]);
const identifierFields = new Set(["authority_id", "world_id", "episode_id", "source_world_id", "destination_world_id"]);
const traceHeader = new RegExp(`^\\d{8}T\\d{9,12}Z${uuid}(?:\\.\\d{8}T\\d{9,12}Z${uuid}){0,31}$`);

/**
 * Explicitly load the official tracing extension behind a client that strips
 * every upstream prompt, model payload, tool argument/result, event and error.
 * Normal Pi sessions and ambient LangSmith configuration are unaffected.
 */
export async function createLangSmithPiTracing(options: LangSmithPiTracingOptions): Promise<PiTracing> {
  const endpoint = new URL(options.endpoint);
  if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash || (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(endpoint.hostname)))) throw new Error("LangSmith requires an explicit HTTPS or loopback endpoint");
  if (typeof options.apiKey !== "string" || !options.apiKey.trim() || options.apiKey.length > 4096) throw new Error("Supply an explicit LangSmith API key");
  const apiKey = options.apiKey, parentTrace = options.parentTrace, fetchImplementation = options.fetchImplementation;
  const project = options.project ?? "memory-worlds";
  if (!/^[A-Za-z0-9_.-]{1,100}$/.test(project)) throw new Error("Invalid LangSmith project name");
  const workspaceId = options.workspaceId ?? "";
  if (workspaceId && !/^[A-Za-z0-9_-]{1,128}$/.test(workspaceId)) throw new Error("Invalid LangSmith workspace identifier");
  const mode = options.mode ?? "ids-only";
  if (!["ids-only", "selected-content"].includes(mode)) throw new Error("Invalid Pi tracing policy");
  const correlation: Record<string, string> = {};
  for (const [key, value] of Object.entries(options.correlation ?? {})) {
    if (value === undefined) continue;
    if (typeof value !== "string" || (uuidFields.has(key) ? !uuidValue.test(value) : !identifierFields.has(key) || !/^[A-Za-z0-9_-]{1,100}$/.test(value))) throw new Error("Trace correlation accepts canonical UUIDs and bounded identifiers only");
    correlation[key] = uuidFields.has(key) ? value.toLowerCase() : value;
  }
  if (options.selectedContent !== undefined && (!options.selectedContent || typeof options.selectedContent !== "object" || Array.isArray(options.selectedContent))) throw new Error("Trace excerpts require a record of selected strings");
  const selected = { ...options.selectedContent };
  if (Object.keys(selected).length > 16 || Object.entries(selected).some(([key, value]) => !/^[A-Za-z0-9_-]{1,64}$/.test(key) || typeof value !== "string" || value.length > 8000) || JSON.stringify(selected).length > 16000) throw new Error("Trace excerpts must be explicitly selected and bounded");
  if ((mode === "ids-only" && Object.keys(selected).length) || (mode === "selected-content" && !Object.keys(selected).length)) throw new Error("Selected trace excerpts require the selected-content policy");
  if (parentTrace !== undefined && (typeof parentTrace !== "string" || parentTrace.length > 8192 || !traceHeader.test(parentTrace))) throw new Error("Invalid LangSmith parent trace header");
  const [{ Client, RunTree }, { default: langsmith }] = await Promise.all([import("langsmith"), import("@langchain/langsmith-pi-extension")]);
  type CreateRun = Parameters<InstanceType<typeof Client>["createRun"]>[0];
  type UpdateRun = Parameters<InstanceType<typeof Client>["updateRun"]>[1];
  const metadata = { ...correlation, memory_worlds_trace_policy: mode };
  class ScopedTraceClient extends Client {
    override async createRun(run: CreateRun): Promise<void> {
      // Construct a fresh allowlist rather than deleting known sensitive fields.
      await super.createRun({
        id: run.id, name: run.run_type === "llm" ? "Pi model" : run.run_type === "tool" ? "Pi tool" : "Pi execution",
        run_type: run.run_type, start_time: run.start_time, end_time: run.end_time,
        parent_run_id: run.parent_run_id, trace_id: run.trace_id, dotted_order: run.dotted_order,
        project_name: project,
        inputs: mode === "selected-content" && run.name === "Pi agent run" ? { selected: { ...selected } } : {},
        outputs: {}, extra: { metadata: { ...metadata } },
        ...(run.error ? { error: "Pi execution failed" } : {}),
      });
    }
    override async updateRun(id: string, run: UpdateRun): Promise<void> {
      await super.updateRun(id, {
        end_time: run.end_time, trace_id: run.trace_id, dotted_order: run.dotted_order,
        outputs: {}, extra: { metadata: { ...metadata } },
        ...(run.error ? { error: "Pi execution failed" } : {}),
      });
    }
  }
  const client = new ScopedTraceClient({ apiUrl: endpoint.href, apiKey, workspaceId, timeout_ms: 30000, autoBatchTracing: false, omitTracedRuntimeInfo: true, hideInputs: false, hideOutputs: false, hideMetadata: false, tracingSamplingRate: 1, tracingMode: "langsmith", debug: false, fetchImplementation });
  const parent = parentTrace === undefined ? undefined : RunTree.fromHeaders({ "langsmith-trace": parentTrace }, { client, project_name: project, replicas: [], metadata: {} });
  return {
    extensionFactory: pi => langsmith(pi, { client, config: { enabled: true, api_key: apiKey, api_url: endpoint.href, project, metadata, replicas: [] }, getCurrentRunTree: () => parent }),
    flush: () => client.awaitPendingTraceBatches(),
  };
}
