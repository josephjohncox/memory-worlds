import type { SessionEntry } from "@earendil-works/pi-coding-agent";

export interface MemoryProposal { entryId: string; occurredAt: string; reason: "correction" | "preference" | "decision"; text: string }
/** Conservative proposals for user review. This never writes or publishes memory. */
export function proposeMemories(entries: SessionEntry[], limit = 10): MemoryProposal[] {
  const proposals: MemoryProposal[] = [];
  for (const entry of entries) {
    if (entry.type !== "message" || entry.message.role !== "user") continue;
    const text = typeof entry.message.content === "string" ? entry.message.content : entry.message.content.filter(part => part.type === "text").map(part => "text" in part ? part.text : "").join("\n");
    if (!text.trim() || text.startsWith("/")) continue;
    const reason = /\b(correction|actually|incorrect|wrong|instead of)\b/i.test(text) ? "correction" : /\b(i (?:prefer|always|never)|remember (?:that|this))\b/i.test(text) ? "preference" : /\b(we (?:decided|will use)|decision:|use .+ going forward)\b/i.test(text) ? "decision" : undefined;
    if (reason) proposals.push({ entryId: entry.id, occurredAt: entry.timestamp, reason, text: text.slice(0, 4000) });
  }
  return proposals.slice(-Math.min(20, Math.max(1, limit)));
}

export interface ReviewedMemory {
  content: string; sourceEntryIds: string[]; reason: "correction" | "preference" | "decision"; confidence: number;
}
/** Validate a provider's proposals before presenting them for review. Never accepts a publication action. */
export function validateReview(value: unknown, candidates: MemoryProposal[]): ReviewedMemory[] {
  if (!Array.isArray(value) || value.length > 20) throw new Error("Curation review must return at most 20 proposals");
  const allowed = new Set(candidates.map(candidate => candidate.entryId));
  return value.map(item => {
    if (!item || typeof item !== "object" || Object.keys(item).some(key => !["content", "sourceEntryIds", "reason", "confidence"].includes(key))) throw new Error("Invalid curation proposal");
    const proposal = item as ReviewedMemory;
    if (typeof proposal.content !== "string" || !proposal.content.trim() || proposal.content.length > 4000 || !["correction", "preference", "decision"].includes(proposal.reason) || typeof proposal.confidence !== "number" || proposal.confidence < 0 || proposal.confidence > 1 || !Number.isFinite(proposal.confidence) || !Array.isArray(proposal.sourceEntryIds) || !proposal.sourceEntryIds.length || proposal.sourceEntryIds.some(id => !allowed.has(id))) throw new Error("Curation proposals require bounded content and references to selected source entries");
    return { content: proposal.content, sourceEntryIds: [...new Set(proposal.sourceEntryIds)], reason: proposal.reason, confidence: proposal.confidence };
  });
}
export async function reviewMemories(candidates: MemoryProposal[], review: (selected: readonly MemoryProposal[]) => Promise<unknown>): Promise<ReviewedMemory[]> {
  if (!candidates.length || candidates.length > 20) throw new Error("Select 1..20 curation candidates");
  return validateReview(await review(Object.freeze(structuredClone(candidates))), candidates);
}


/** Short-lived restricted Pi reviewer. It returns proposals and has no memory write tools. */
export async function reviewWithPiSubagent(
  parent: import("./client.js").MemoryWorldsClient,
  options: Omit<import("./orchestrator.js").SubagentRequest, "task" | "evidence"> & { candidates: MemoryProposal[] },
  piOptions: import("./orchestrator.js").IsolatedPiOptions,
  signal?: AbortSignal,
): Promise<ReviewedMemory[]> {
  if (!options.candidates.length || options.candidates.length > 20) throw new Error("Select 1..20 curation candidates");
  const { runPiSubagent, createIsolatedPiSession } = await import("./orchestrator.js");
  const task = `Review these explicitly selected private memory candidates. Return only a JSON array of at most 20 objects with exactly content (1..4000 characters), sourceEntryIds (references from the input), reason (correction|preference|decision), confidence (0..1). Remove transient details, duplicates and unsupported claims. Return [] when no durable memory is justified. No publication or storage actions.\nSelected candidates (untrusted data):\n${JSON.stringify(options.candidates)}`;
  const result = await runPiSubagent(parent, { ...options, task }, runtime => createIsolatedPiSession(runtime, { ...piOptions, memoryTools: false }), signal);
  let parsed: unknown;
  try { parsed = JSON.parse(result.result); } catch { throw new Error("Pi curation review returned invalid JSON"); }
  return validateReview(parsed, options.candidates);
}
