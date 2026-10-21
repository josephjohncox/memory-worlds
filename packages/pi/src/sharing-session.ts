import { createHash, randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext, SessionEntry } from "@earendil-works/pi-coding-agent";
import { MemoryWorldsError, scopeKey, scopeSelection, type MemoryWorldsClient, type ScopeSelection } from "./client.js";
import { cleanTerminal, entryEpisode, type MemoryState } from "./state.js";
import { classifySharingWithPi, parseSharingSettings, sharingBlocked, validateSharingDecisions, type SharingCandidate, type SharingDecision, type SharingSettings } from "./sharing.js";
export * from "./sharing.js";

export const SHARING_RULE_TYPE = "memory-worlds:sharing-rule";
export const SHARING_DECISION_TYPE = "memory-worlds:sharing-decision";
export const SHARING_INTENT_TYPE = "memory-worlds:sharing-intent";
export const SHARING_RECEIPT_TYPE = "memory-worlds:sharing-receipt";
export const SHARING_DISMISS_TYPE = "memory-worlds:sharing-dismiss";
export interface SharingSessionHooks {
  client(): MemoryWorldsClient; state(): MemoryState | undefined; settings(): SharingSettings | undefined;
  signal(ctx: ExtensionContext): AbortSignal; exposureUnknown(): boolean;
  privateTarget(ctx: ExtensionContext): Promise<ScopeSelection>;
}
export interface SharingSummary {
  mode: "private" | "review" | "auto"; status: "private" | "active" | "paused";
  destination?: Destination; classifier?: { provider: string; model: string };
  pending: number; failed: number; accepted: number;
}
type Destination = { kind: "local"; scope: ScopeSelection } | { kind: "mount"; mountId: string; worldId: string; authorityId: string; url: string };
interface Binding { sessionId: string; contextId: string; source: ScopeSelection; serviceURL: string; authorityId: string; credentialId: string; settingsDigest: string }
interface Rule { id: string; mode: "review" | "auto"; binding: Binding; destination: Destination; cursor: string | null }
interface Source { entryId: string; sourceId: string; hash: string; occurredAt: string; role: "user" | "assistant" }
interface Proposal { id: string; ruleId: string; source: Source; decision: SharingDecision }
interface Payload { source_id: string; content: string; occurred_at: string; project: true }
interface Intent { sessionId: string; ruleId: string; proposalId: string; destination: Destination; payload: Payload }
interface Accepted { id: string; kind: "local" | "mount"; state: string }
interface ProposalState { proposal: Proposal; intent?: Intent; receipt?: Accepted; dismissed: boolean }
interface ClassifierDependencies { classify?: (ctx: ExtensionContext, candidates: readonly SharingCandidate[], settings: SharingSettings, signal: AbortSignal) => Promise<SharingDecision[]> }
class SharingError extends Error {
  constructor(message: string, readonly invalidates = false) { super(message); this.name = "MemoryWorldsSharingError"; }
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH = /^[0-9a-f]{64}$/;
function text(value: unknown, maximum = 2000): value is string { return typeof value === "string" && value.length > 0 && value.length <= maximum; }
function object(value: unknown): value is Record<string, unknown> { return Boolean(value && typeof value === "object" && !Array.isArray(value)); }
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (object(value)) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}
function digest(value: unknown): string { return createHash("sha256").update(canonical(value)).digest("hex"); }
function sameScope(left: ScopeSelection | null | undefined, right: ScopeSelection): boolean { return Boolean(left && scopeKey(left) === scopeKey(right)); }
function freezeSettings(settings: SharingSettings): SharingSettings {
  const copy = structuredClone(settings);
  Object.freeze(copy.classifier); Object.freeze(copy.categories); Object.freeze(copy.excludedTopics); return Object.freeze(copy);
}
function sourceRecord(sessionId: string, candidate: SharingCandidate): Source { return { entryId: candidate.entryId, sourceId: `pi:${sessionId}:${candidate.entryId}`, hash: digest(candidate), occurredAt: candidate.occurredAt, role: candidate.role }; }
/** Bound text work before constructing or hashing an episode, including huge messages. */
function candidateCharacters(entry: SessionEntry, maximum: number): number {
  if (entry.type !== "message" || entry.message.role !== "user" && entry.message.role !== "assistant") return 0;
  const content = entry.message.content;
  if (typeof content === "string") return content.length;
  if (!Array.isArray(content)) return 0;
  if (content.length > 100) return maximum + 1;
  let characters = 0, parts = 0;
  for (const part of content) {
    if (!part || typeof part !== "object") return maximum + 1;
    if (part.type === "text") {
      if (!("text" in part) || typeof part.text !== "string") return maximum + 1;
      characters += (parts++ ? 1 : 0) + part.text.length;
      if (characters > maximum) return characters;
    }
  }
  return characters;
}
function candidateFor(entry: SessionEntry, sessionId: string): SharingCandidate | undefined {
  try {
    const episode = entryEpisode(sessionId, entry);
    if (!episode || !Number.isFinite(Date.parse(episode.occurred_at))) return undefined;
    if (Buffer.from(episode.content, "utf8").toString("utf8") !== episode.content) return undefined;
    return { entryId: entry.id, text: episode.content, occurredAt: episode.occurred_at, role: episode.role as "user" | "assistant" };
  } catch { return undefined; }
}
function raceAbort<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => reject(new SharingError("Sharing operation was interrupted."));
    signal.addEventListener("abort", abort, { once: true });
    work.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}
function validateDestination(value: unknown): Destination {
  if (!object(value)) throw new SharingError("Sharing destination is invalid.", true);
  if (value.kind === "local") return { kind: "local", scope: scopeSelection(value.scope) };
  if (value.kind === "mount" && text(value.mountId, 100) && /^[a-zA-Z0-9_-]+$/.test(value.mountId) && text(value.worldId, 100) && /^[a-zA-Z0-9_-]+$/.test(value.worldId) && text(value.authorityId) && text(value.url, 4096)) return { kind: "mount", mountId: value.mountId, worldId: value.worldId, authorityId: value.authorityId, url: value.url };
  throw new SharingError("Sharing destination is invalid.", true);
}
function validateRule(value: unknown, sessionId: string): Rule {
  if (!object(value) || !text(value.id) || !UUID.test(value.id) || !["review", "auto"].includes(String(value.mode)) || !object(value.binding)) throw new SharingError("Saved sharing policy is invalid.", true);
  const binding = value.binding;
  if (binding.sessionId !== sessionId || !text(binding.contextId) || !UUID.test(binding.contextId) || !text(binding.serviceURL, 4096) || !text(binding.authorityId) || !text(binding.credentialId) || !text(binding.settingsDigest) || !HASH.test(binding.settingsDigest) || !(value.cursor === null || text(value.cursor))) throw new SharingError("Saved sharing binding is invalid.", true);
  return { id: value.id, mode: value.mode as Rule["mode"], binding: { sessionId, contextId: binding.contextId, source: scopeSelection(binding.source), serviceURL: binding.serviceURL, authorityId: binding.authorityId, credentialId: binding.credentialId, settingsDigest: binding.settingsDigest }, destination: validateDestination(value.destination), cursor: value.cursor as string | null };
}

/** Per-chat user policy. Classifiers propose excerpts; the API authorizes each publication. */
export class ChatSharing {
  private rule?: Rule; private settings?: SharingSettings; private paused = false;
  private generation = 0; private abort = new AbortController(); private serial: Promise<unknown> = Promise.resolve();
  private proposals = new Map<string, ProposalState>();
  private readonly classify: NonNullable<ClassifierDependencies["classify"]>;
  constructor(private readonly pi: ExtensionAPI, private readonly hooks: SharingSessionHooks, dependencies: ClassifierDependencies = {}) { this.classify = dependencies.classify ?? classifySharingWithPi; }
  summary(): SharingSummary {
    const states = [...this.proposals.values()];
    return { mode: this.rule?.mode ?? "private", status: !this.rule ? "private" : this.paused ? "paused" : "active", destination: this.rule && structuredClone(this.rule.destination), classifier: this.settings && { ...this.settings.classifier }, pending: states.filter(state => !state.receipt && !state.dismissed && state.proposal.decision.visibility !== "private").length, failed: states.filter(state => state.intent && !state.receipt && !state.dismissed).length, accepted: states.filter(state => state.receipt).length };
  }
  private nextGeneration(): number { this.abort.abort(); this.abort = new AbortController(); return ++this.generation; }
  private append(type: string, data: unknown): void { this.pi.appendEntry(type, structuredClone(data)); }
  private save(ctx: ExtensionContext): void { this.append(SHARING_RULE_TYPE, { sessionId: ctx.sessionManager.getSessionId(), rule: this.rule ?? null, paused: this.paused }); }
  stop(ctx: ExtensionContext): void { this.nextGeneration(); this.rule = undefined; this.settings = undefined; this.paused = false; this.save(ctx); }
  /** Interrupt runtime work without changing the saved, explicitly enabled chat policy. */
  suspend(): void { this.nextGeneration(); this.paused = true; }
  private assertEpoch(epoch: number): void { if (epoch !== this.generation || this.abort.signal.aborted) throw new SharingError("Sharing mode changed; the previous operation was discarded."); }
  private serialize<T>(work: () => Promise<T>): Promise<T> { const result = this.serial.then(work, work); this.serial = result.catch(() => undefined); return result; }
  private signal(ctx: ExtensionContext, settings: SharingSettings): AbortSignal { return AbortSignal.any([this.abort.signal, this.hooks.signal(ctx), AbortSignal.timeout(settings.timeoutMs)]); }
  private configured(): SharingSettings {
    const settings = parseSharingSettings(this.hooks.settings());
    if (!settings) throw new SharingError("Configure an explicit sharing classifier before enabling chat sharing.");
    return freezeSettings(settings);
  }
  private sourceCandidate(ctx: ExtensionContext, source: Source, sessionId: string): SharingCandidate {
    const entry = ctx.sessionManager.getBranch().find(item => item.id === source.entryId), candidate = entry && candidateFor(entry, sessionId);
    if (!candidate || digest(candidate) !== source.hash || source.sourceId !== `pi:${sessionId}:${candidate.entryId}` || source.occurredAt !== candidate.occurredAt || source.role !== candidate.role) throw new SharingError("The selected sharing source changed or left the active branch.", true);
    return candidate;
  }
  private checkBinding(ctx: ExtensionContext, rule: Rule): void {
    const state = this.hooks.state(), settings = this.configured();
    if (this.hooks.exposureUnknown() || ctx.sessionManager.getSessionId() !== rule.binding.sessionId || this.hooks.client().url !== rule.binding.serviceURL || digest(settings) !== rule.binding.settingsDigest || !state?.boundContext || state.boundContext.id !== rule.binding.contextId || state.boundContext.credential_id !== rule.binding.credentialId || !sameScope(state.writeScope, rule.binding.source) || !sameScope(state.boundContext.write_scope, rule.binding.source)) throw new SharingError("Sharing binding or exposure changed; enable a policy in a clean context.", true);
    if (state.exposedAuthorities.some(authority => authority !== rule.binding.authorityId)) throw new SharingError("Foreign memory exposure prevents automatic sharing.", true);
  }
  private async preflight(ctx: ExtensionContext, client: MemoryWorldsClient, rule: Rule, epoch: number, signal: AbortSignal): Promise<void> {
    this.assertEpoch(epoch); this.checkBinding(ctx, rule);
    const exposure = digest({ authorities: [...(this.hooks.state()?.exposedAuthorities ?? [])].sort(), scopes: [...(this.hooks.state()?.exposedScopes ?? [])].sort() });
    // This read-only probe detects token-file rotation; the operation keeps its pinned credential.
    const current = await this.hooks.client().identity(signal), identity = await client.identity(signal);
    if (current.credential_id !== rule.binding.credentialId || current.authority_id !== rule.binding.authorityId || identity.credential_id !== rule.binding.credentialId || identity.authority_id !== rule.binding.authorityId) throw new SharingError("Memory credential changed; chat sharing is private.", true);
    const context = await client.context(rule.binding.contextId, signal);
    if (context.credential_id !== rule.binding.credentialId || !sameScope(context.write_scope, rule.binding.source)) throw new SharingError("Sharing context changed.", true);
    const source = await client.scope(rule.binding.source, signal);
    if (!["invocation-private", "agent-persistent"].includes(source.kind)) throw new SharingError("Chat sharing requires an invocation-private or agent-persistent source.", true);
    if (rule.destination.kind === "local") {
      const destination = rule.destination, world = (await client.worlds(signal)).find(item => item.id === destination.scope.world_id);
      const scope = await client.scope(destination.scope, signal);
      if (!world || world.authority_id !== rule.binding.authorityId || !["team", "company", "ad-hoc"].includes(world.kind) || !["world-shared", "run-shared"].includes(scope.kind)) throw new SharingError("Select an authorized shared world and shared scope as the destination.", true);
    } else {
      const destination = rule.destination, mount = (await client.mounts(signal)).find(item => item.id === destination.mountId);
      if (!mount?.can_publish || !mount.world_ids.includes(destination.worldId) || mount.authority_id !== destination.authorityId || mount.url !== destination.url) throw new SharingError("The remote sharing destination changed or publication is unauthorized.", true);
    }
    const reads = new Map<string, ScopeSelection>();
    for (const label of this.hooks.state()?.exposedScopes ?? []) {
      const prefix = `${rule.binding.authorityId}/`;
      if (!label.startsWith(prefix)) throw new SharingError("Foreign or unknown memory exposure prevents sharing.", true);
      const fields = label.slice(prefix.length).split("/");
      if (fields.length !== 2) throw new SharingError("Memory exposure is invalid.", true);
      let selected: ScopeSelection;
      try { selected = scopeSelection({ world_id: fields[0], scope_id: fields[1] }); } catch { throw new SharingError("Memory exposure is invalid.", true); }
      if (!sameScope(selected, rule.binding.source)) reads.set(scopeKey(selected), selected);
    }
    if (reads.size > 50 || rule.destination.kind === "mount" && reads.size) throw new SharingError("Remote sharing requires exposure only to the selected private source.", true);
    for (const selected of reads.values()) if (!["world-shared", "run-shared"].includes((await client.scope(selected, signal)).kind)) throw new SharingError("Other private-scope exposure prevents sharing.", true);
    if (rule.destination.kind === "local" && reads.size && !await client.canFlow([...reads.values()], rule.destination.scope, signal)) throw new SharingError("The destination cannot read every exposed memory scope.", true);
    this.assertEpoch(epoch); this.checkBinding(ctx, rule);
    if (exposure !== digest({ authorities: [...(this.hooks.state()?.exposedAuthorities ?? [])].sort(), scopes: [...(this.hooks.state()?.exposedScopes ?? [])].sort() })) throw new SharingError("Memory exposure changed during sharing validation.", true);
    signal.throwIfAborted();
  }
  async restore(ctx: ExtensionContext, fork = false): Promise<void> {
    const epoch = this.nextGeneration(); this.rule = undefined; this.settings = undefined; this.paused = false; this.proposals.clear();
    if (fork) { this.save(ctx); return; }
    const sessionId = ctx.sessionManager.getSessionId();
    try {
      for (const entry of ctx.sessionManager.getEntries()) {
        if (entry.type !== "custom" || !entry.customType.startsWith("memory-worlds:sharing-") || !object(entry.data) || entry.data.sessionId !== sessionId) continue;
        const data = entry.data;
        if (entry.customType === SHARING_RULE_TYPE) {
          if (typeof data.paused !== "boolean") throw new SharingError("Saved sharing state is invalid.", true);
          this.rule = data.rule === null ? undefined : validateRule(data.rule, sessionId); this.paused = data.paused;
        } else if (entry.customType === SHARING_DECISION_TYPE) {
          if (!Array.isArray(data.proposals) || data.proposals.length > 5 || !(data.cursor === null || text(data.cursor))) throw new SharingError("Saved sharing decisions are invalid.", true);
          for (const raw of data.proposals) {
            if (!object(raw) || !text(raw.id) || !UUID.test(raw.id) || !text(raw.ruleId) || !UUID.test(raw.ruleId) || !object(raw.source) || !text(raw.source.hash) || !HASH.test(raw.source.hash) || !text(raw.source.entryId) || !text(raw.source.sourceId, 4500) || !text(raw.source.occurredAt) || !["user", "assistant"].includes(String(raw.source.role)) || !object(raw.decision) || !text(raw.decision.entryId, 200) || raw.decision.entryId !== raw.source.entryId || !text(raw.decision.excerpt, 4000) || !["decision", "procedure", "technical-finding", "explicit-share", "team-discussion", "team-preference"].includes(String(raw.decision.category)) || !["shareable", "review", "private"].includes(String(raw.decision.visibility)) || typeof raw.decision.confidence !== "number" || !Number.isFinite(raw.decision.confidence) || raw.decision.confidence < 0 || raw.decision.confidence > 1) throw new SharingError("Saved sharing proposal is invalid.", true);
            this.proposals.set(raw.id, { proposal: structuredClone(raw) as unknown as Proposal, dismissed: false });
          }
          if (this.rule && this.rule.id === data.ruleId) this.rule.cursor = data.cursor as string | null;
        } else if (entry.customType === SHARING_INTENT_TYPE) {
          const state = typeof data.proposalId === "string" && this.proposals.get(data.proposalId);
          if (!state || data.ruleId !== state.proposal.ruleId || !object(data.payload)) throw new SharingError("Saved sharing intent is invalid.", true);
          state.intent = structuredClone(data) as unknown as Intent;
        } else if (entry.customType === SHARING_RECEIPT_TYPE) {
          const state = typeof data.proposalId === "string" && this.proposals.get(data.proposalId);
          if (!state || !state.intent || data.ruleId !== state.proposal.ruleId || !object(data.receipt) || !text(data.receipt.id) || !["local", "mount"].includes(String(data.receipt.kind)) || !["stored", "pending", "delivered", "blocked"].includes(String(data.receipt.state))) throw new SharingError("Saved sharing receipt is invalid.", true);
          state.receipt = data.receipt as unknown as Accepted;
        } else if (entry.customType === SHARING_DISMISS_TYPE) {
          const state = typeof data.proposalId === "string" && this.proposals.get(data.proposalId); if (state) state.dismissed = true;
        }
      }
      if (!this.rule) { this.paused = false; return; }
      const rule = this.rule, settings = this.configured(); this.settings = settings;
      await this.hooks.client().withCredentialSession(client => this.preflight(ctx, client, rule, epoch, this.signal(ctx, settings)));
      this.assertEpoch(epoch);
      if ([...this.proposals.values()].some(state => state.proposal.ruleId === rule.id && !state.receipt && !state.dismissed && (state.intent || rule.mode === "auto" && state.proposal.decision.visibility === "shareable" && state.proposal.decision.confidence >= settings.minConfidence))) this.paused = true;
    } catch { if (epoch === this.generation) { this.proposals.clear(); this.stop(ctx); ctx.ui.notify("Saved sharing policy could not be verified; this chat is private.", "warning"); } }
  }
  async command(args: string, ctx: ExtensionContext): Promise<void> {
    const parts = args.trim().split(/\s+/).filter(Boolean), command = parts[0] ?? "status";
    if (command === "status") { ctx.ui.notify(JSON.stringify(this.summary(), null, 2), "info"); return; }
    if (command === "private") { this.stop(ctx); ctx.ui.notify("Chat sharing is private. Publications already accepted by a remote outbox remain queued.", "info"); return; }
    if (command === "pending") {
      const pending = [...this.proposals.values()].filter(state => !state.dismissed && !state.receipt && state.proposal.decision.visibility !== "private");
      ctx.ui.notify(pending.length ? pending.map(({ proposal }) => `${proposal.id} ${proposal.decision.category} ${proposal.decision.confidence.toFixed(2)}${proposal.ruleId !== this.rule?.id ? " [inactive policy]" : ""}\n${cleanTerminal(proposal.decision.excerpt)}`).join("\n\n").slice(0, 32000) : "No sharing proposals are pending.", "info"); return;
    }
    if (command === "dismiss" && parts.length === 2) {
      const state = this.proposals.get(parts[1]!); if (!state || state.receipt) throw new SharingError("Select an unsubmitted sharing proposal.");
      state.dismissed = true; this.append(SHARING_DISMISS_TYPE, { sessionId: ctx.sessionManager.getSessionId(), proposalId: state.proposal.id }); return;
    }
    if (["review", "auto"].includes(command)) {
      const epoch = this.nextGeneration(); this.rule = undefined; this.settings = undefined; this.paused = false; this.save(ctx);
      return this.serialize(async () => {
        try {
          const settings = this.configured(), signal = this.signal(ctx, settings), source = scopeSelection(await this.hooks.privateTarget(ctx)); this.assertEpoch(epoch);
          await this.hooks.client().withCredentialSession(async client => {
            const identity = await client.identity(signal), context = this.hooks.state()?.boundContext; this.assertEpoch(epoch);
            if (!context || !sameScope(context.write_scope, source) || context.credential_id !== identity.credential_id) throw new SharingError("Bind the current private source context before enabling sharing.");
            let destination: Destination;
            if (parts[1] === "mount" && parts.length === 4) {
              const mount = (await client.mounts(signal)).find(item => item.id === parts[2]);
              if (!mount?.can_publish || !mount.world_ids.includes(parts[3]!)) throw new SharingError("Select an authorized publication mount and one of its world IDs.");
              destination = { kind: "mount", mountId: mount.id, worldId: parts[3]!, authorityId: mount.authority_id, url: mount.url };
            } else if (parts.length === 3) destination = { kind: "local", scope: scopeSelection({ world_id: parts[1], scope_id: parts[2] }) };
            else throw new SharingError("Use review|auto WORLD SCOPE, or review|auto mount MOUNT WORLD.");
            const rule: Rule = { id: randomUUID(), mode: command as Rule["mode"], destination, cursor: ctx.sessionManager.getLeafId(), binding: { sessionId: ctx.sessionManager.getSessionId(), contextId: context.id, source, serviceURL: client.url, authorityId: identity.authority_id, credentialId: identity.credential_id, settingsDigest: digest(settings) } };
            await this.preflight(ctx, client, rule, epoch, signal); this.assertEpoch(epoch);
            this.rule = rule; this.settings = settings; this.save(ctx); ctx.ui.notify(`Chat sharing is ${command} for future conversation text. The private source remains private.`, "info");
          }, signal);
        } catch (error) { if (epoch === this.generation) this.stop(ctx); throw error instanceof SharingError ? error : new SharingError("Chat sharing could not be enabled; verify classifier, context, destination and credentials."); }
      });
    }
    if ((command === "approve" && parts.length === 2) || command === "retry" && parts.length === 1) {
      const epoch = this.generation;
      return this.serialize(async () => {
        if (!this.rule || !this.settings) throw new SharingError("Enable the matching chat sharing policy before approving or retrying.");
        const rule = this.rule, signal = this.signal(ctx, this.settings);
        // Invalid command input is not an operational failure of an active rule.
        const approval = command === "approve" ? this.proposals.get(parts[1]!) : undefined;
        if (command === "approve" && (!approval || approval.proposal.ruleId !== rule.id || approval.dismissed || approval.receipt || approval.proposal.decision.visibility === "private")) throw new SharingError("Select a pending proposal from the active policy.");
        try {
          await this.hooks.client().withCredentialSession(async client => {
            await this.preflight(ctx, client, rule, epoch, signal);
            if (approval) {
              await this.send(ctx, client, rule, approval, epoch, signal);
            } else for (const state of this.proposals.values()) if (state.proposal.ruleId === rule.id && !state.receipt && !state.dismissed && (state.intent || rule.mode === "auto" && state.proposal.decision.visibility === "shareable" && state.proposal.decision.confidence >= this.settings!.minConfidence)) await this.send(ctx, client, rule, state, epoch, signal);
            this.assertEpoch(epoch); this.paused = false; this.save(ctx);
          }, signal);
        } catch (error) { this.failed(ctx, error, epoch); throw error instanceof SharingError ? error : new SharingError("Sharing submission failed; the immutable intent remains private for retry."); }
      });
    }
    throw new SharingError("Use sharing status|private|review|auto|pending|approve|dismiss|retry.");
  }
  private async send(ctx: ExtensionContext, client: MemoryWorldsClient, rule: Rule, state: ProposalState, epoch: number, signal: AbortSignal): Promise<void> {
    if (state.receipt || state.dismissed) return;
    this.assertEpoch(epoch); this.checkBinding(ctx, rule);
    const candidate = this.sourceCandidate(ctx, state.proposal.source, rule.binding.sessionId), settings = this.settings!;
    const decisions = validateSharingDecisions([state.proposal.decision], [candidate], settings);
    if (decisions.length !== 1 || decisions[0]!.visibility === "private" || sharingBlocked(candidate.text, settings) || sharingBlocked(decisions[0]!.excerpt, settings)) throw new SharingError("This source is private, excluded or no longer eligible for publication.", true);
    const decision = decisions[0]!;
    const expected: Intent = { sessionId: rule.binding.sessionId, ruleId: rule.id, proposalId: state.proposal.id, destination: structuredClone(rule.destination), payload: { source_id: `pi:sharing:${digest([rule.id, candidate.entryId, digest(decision.excerpt)])}`, content: decision.excerpt, occurred_at: candidate.occurredAt, project: true } };
    if (state.intent && canonical(state.intent) !== canonical(expected)) throw new SharingError("The saved sharing intent does not match its immutable source and policy.", true);
    if (!state.intent) { this.append(SHARING_INTENT_TYPE, expected); state.intent = expected; }
    await this.preflight(ctx, client, rule, epoch, signal); if (state.dismissed || state.receipt) return;
    this.assertEpoch(epoch); this.checkBinding(ctx, rule); signal.throwIfAborted();
    const payload = structuredClone(state.intent.payload); let accepted: Accepted;
    if (rule.destination.kind === "local") {
      const receipt = await client.publishScope(rule.destination.scope, payload, signal), episode = receipt.episode;
      if (!text(episode.id) || episode.source_id !== payload.source_id || episode.content !== payload.content || Date.parse(episode.occurred_at) !== Date.parse(payload.occurred_at) || episode.project !== true || episode.source_type !== "publication" || episode.context_id != null || episode.metadata && Object.keys(episode.metadata).length) throw new SharingError("The publication receipt did not match its immutable request.");
      accepted = { kind: "local", id: episode.id, state: "stored" };
    } else {
      const receipt = await client.publish({ ...payload, destination_mount_id: rule.destination.mountId, world_id: rule.destination.worldId }, signal);
      if (!text(receipt.id) || receipt.destination_mount_id !== rule.destination.mountId || receipt.destination_authority_id !== rule.destination.authorityId || receipt.world_id !== rule.destination.worldId || receipt.source_id !== payload.source_id || !["pending", "delivered", "blocked"].includes(receipt.state)) throw new SharingError("The remote outbox receipt did not match its immutable request.");
      accepted = { kind: "mount", id: receipt.id, state: receipt.state };
    }
    // Accepted writes retain their receipt even when a mode switch raced the ACK.
    this.append(SHARING_RECEIPT_TYPE, { sessionId: rule.binding.sessionId, ruleId: rule.id, proposalId: state.proposal.id, receipt: accepted }); state.receipt = accepted;
  }
  private failed(ctx: ExtensionContext, error: unknown, epoch: number): void {
    if (epoch !== this.generation) return;
    if (error instanceof SharingError && error.invalidates || error instanceof MemoryWorldsError && [401, 403, 404].includes(error.status)) { this.stop(ctx); ctx.ui.notify(error instanceof SharingError ? error.message : "Sharing authorization changed; this chat is private.", "warning"); }
    else { this.paused = Boolean(this.rule); this.save(ctx); ctx.ui.notify("Chat sharing is paused; verify service and classifier, then use sharing retry. Private source is retained.", "warning"); }
  }
  async flush(ctx: ExtensionContext): Promise<void> {
    if (!this.rule || !this.settings || this.paused) return;
    const epoch = this.generation;
    await this.serialize(async () => {
      if (epoch !== this.generation || !this.rule || !this.settings || this.paused) return;
      const rule = this.rule, settings = this.settings, signal = this.signal(ctx, settings);
      try {
        await this.hooks.client().withCredentialSession(async client => {
          await this.preflight(ctx, client, rule, epoch, signal);
          const branch = ctx.sessionManager.getBranch(), after = rule.cursor === null ? -1 : branch.findIndex(entry => entry.id === rule.cursor);
          if (rule.cursor !== null && after < 0) throw new SharingError("Sharing source history changed; enable a fresh policy.", true);
          const candidates: SharingCandidate[] = [], processed: Source[] = []; let characters = 0, messages = 0, cursor = rule.cursor, excluded = false, overBudget = false;
          // Keep the pending conversation window intact: filtering a sensitive user
          // message alone can orphan an assistant reply that contains the same topic.
          for (let index = after + 1; index < branch.length; index++) {
            const entry = branch[index]!;
            if (entry.type !== "message" || !["user", "assistant"].includes(entry.message.role)) continue;
            messages++; cursor = entry.id;
            if (overBudget) continue;
            if (messages > 5) { overBudget = true; continue; }
            characters += candidateCharacters(entry, settings.maxInputChars - characters);
            if (characters > settings.maxInputChars) { overBudget = true; continue; }
            // User attachments are unavailable to the text classifier. Never
            // assess a reply after silently discarding part of its user input.
            if (entry.message.role === "user" && typeof entry.message.content !== "string" && (!Array.isArray(entry.message.content) || entry.message.content.some(part => !part || part.type !== "text" || !("text" in part) || typeof part.text !== "string"))) { excluded = true; continue; }
            const candidate = candidateFor(entry, rule.binding.sessionId);
            if (!candidate) { excluded = true; continue; }
            processed.push(sourceRecord(rule.binding.sessionId, candidate));
            excluded ||= sharingBlocked(candidate.text, settings); candidates.push(candidate);
          }
          if (cursor === rule.cursor) return;
          // The complete window must fit. Already withheld tails require no text
          // parsing, exclusion matching or hashing, but still advance the cursor.
          if (excluded || overBudget) candidates.length = 0;
          const raw = candidates.length ? await raceAbort(this.classify(ctx, Object.freeze(candidates.map(candidate => Object.freeze(structuredClone(candidate)))), settings, signal), signal) : [];
          this.assertEpoch(epoch); this.checkBinding(ctx, rule);
          const decisions = validateSharingDecisions(raw, candidates, settings);
          await this.preflight(ctx, client, rule, epoch, signal);
          const proposals: Proposal[] = decisions.map(decision => ({ id: randomUUID(), ruleId: rule.id, decision, source: sourceRecord(rule.binding.sessionId, candidates.find(candidate => candidate.entryId === decision.entryId)!) }));
          this.append(SHARING_DECISION_TYPE, { sessionId: rule.binding.sessionId, ruleId: rule.id, classifier: settings.classifier, policyDigest: rule.binding.settingsDigest, processed, proposals, cursor, windowMessages: messages, withheld: excluded ? "excluded-window" : overBudget ? "input-budget" : null });
          for (const proposal of proposals) this.proposals.set(proposal.id, { proposal, dismissed: false }); rule.cursor = cursor;
          for (const proposal of proposals) if (rule.mode === "auto" && proposal.decision.visibility === "shareable" && proposal.decision.confidence >= settings.minConfidence) await this.send(ctx, client, rule, this.proposals.get(proposal.id)!, epoch, signal);
        }, signal);
      } catch (error) { this.failed(ctx, error, epoch); }
    });
  }
}
