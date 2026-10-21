# Per-chat sharing modes

## Purpose

Let a user authorize selected knowledge from one chat to reach a particular
shared world automatically. Keep private capture, classification, and publication
as separate operations. The user's mode and current service grants authorize
publication; a classifier selects excerpts within that authorization.

## Modes and settings

- `private` is the default. It makes no sharing-classifier calls or automatic
  shared writes. Existing explicit publication commands remain available.
- `review` classifies future user and assistant messages into private proposals.
  A user command approves an individual proposal before publication.
- `auto` publishes eligible proposals above a configured confidence threshold.
  Ambiguous proposals remain available for review. Private proposals stay private.

The user enables a mode with an explicit destination: a local shared-world scope,
or a publisher-authorized mount and remote world. A separate configured
agent-persistent or invocation-private source scope remains the chat's private
working-memory target. Configuration supplies an explicit classifier provider and
model, categories, exclusions, and bounded input/output settings; it cannot enable
automatic publication by itself.

Eligible categories are decisions, procedures, technical findings, explicitly
marked shareable information, durable attributed team discussions, and team
preferences. Default exclusions include credentials, personal information,
personnel and performance judgments, HR reviews, rewards, compensation, and
positive or negative personal sentiment about people or projects. Users can add
excluded topics. Exclusions take precedence over a shareable marker. A classifier
confidence score neither establishes factual verification nor proves privacy.
Explicit-share markers must originate in a user message; an assistant cannot
manufacture that opt-in. Explicit-only classification retains the clean window
as context while permitting excerpts from marked user sources only.

## Classifier boundary

Use the installed Pi model registry and its existing authentication. Require an
explicit physical chat model; never select the main agent model or a virtual
router implicitly. Haiku and Luna are examples, not automatic provider choices.
The isolated request has no tools, memory credential, source lookup capability,
or destination parameter. It receives only a bounded window of new textual user
and assistant entries. Obvious secrets and excluded material are filtered before
the request. Tool results, attachments, custom retrieved-memory messages, copied
fork history, and system instructions are excluded.
Withhold the complete pending window when any of its messages is excluded, or
the complete window exceeds the input budget. Do not remove sensitive user text
and classify its assistant reply without that context. Advance beyond withheld
windows so the next clean turn remains independently eligible.

Accept strict JSON with entry references, exact contiguous excerpts, category,
visibility, and finite confidence. Reject extra fields, invented text, invalid
Unicode, truncated replies, tool calls, unknown references, and oversized output.
Apply deterministic exclusions again to each selected excerpt. These gates reduce
errors; they do not constitute a general-purpose information-flow proof.

## Authority, exposure, and lifecycle

Bind each sharing rule to its chat, private source scope, context, service URL,
authority, credential, classifier/settings snapshot, destination, and future-entry
cursor. Pin a rotating credential for each operation. Recheck the binding and
exposure before classification and before sending an immutable publication.

Unknown or foreign authority exposure stops automation. For a local destination,
memory exposure outside the explicitly authorized private source must pass the
existing audience-flow check into the destination. For a remote destination,
there is no cross-authority audience-containment protocol: automatic processing
requires exposure confined to the own private source. Branching and compaction
cannot erase exposure. Forking, tree navigation, changed bindings, and mode changes
stop in-flight work and require an explicit new rule where appropriate.

## Durability and publication

Keep source entry identities and hashes, classifier decisions, rules, pending
proposals, immutable publication intents, and returned receipts in private Pi
session entries. Persist an intent before its first network submission. Retries
reuse the exact target, text, source identity, and timestamp without reclassification.
The current rule and grants must still authorize the retry.

Use existing local scoped-publication and remote routed-publication endpoints.
Send only the selected excerpt, stable source ID, timestamp, and projection flag,
plus required remote routing fields. Do not send private source references,
transcripts, classifier reasoning, or the private context ID. Graphiti extracts
published knowledge at its destination under normal namespace isolation.

Switching to `private` prevents future and unsubmitted work. A remote publication
already accepted into the service outbox continues under its existing durable
delivery and live-authorization contract. Turning off a mode is not a recall or
cancellation operation for accepted publications.

## Verification

Test classifier schema and exclusion behavior, explicit-only markers, Unicode,
private-mode zero calls, future-only windows, review approval, threshold routing,
immutable lost-ack retries, cancellation races, credential/context/exposure changes,
fork and branch handling, service authorization failure, remote payload privacy,
and actual Pi model-registry invocation with a synthetic provider. Retain current
Python publication and graph-isolation checks. No paid model or live tenant is
required to prove these implementation boundaries.
