# Chat sharing modes

[Documentation](README.md) · [Sharing reference](reference/chat-sharing.md)

Keep working notes private and publish selected useful excerpts to a team or
company world. Native Pi can classify future messages under a rule you enable
for one chat. You choose the destination and policy; the classifier selects text
within that rule and cannot grant access.

| Mode | What happens |
| --- | --- |
| `private` | No sharing-classifier calls; explicit selected-text publication remains available |
| `review` | Eligible excerpts become private proposals; you approve each publication |
| `auto` | Eligible excerpts above your confidence threshold publish; ambiguous proposals wait for review |

Start with `review` to inspect the classifier's choices. Every new chat starts
private; only resuming that chat's own valid saved rule can restore a mode.
Configuration alone enables none of them. Automatic private capture is separate.

## Configure a lightweight model

Add `sharing` to `~/.pi/agent/memory-worlds.json`. Replace the provider and model
placeholders with a classifier available and authenticated in your Pi registry,
for example your accessible Haiku or Luna model:

```json
{
  "sharing": {
    "classifier": {"provider": "YOUR_PROVIDER", "model": "YOUR_CLASSIFIER_MODEL"},
    "categories": [
      "decision", "procedure", "technical-finding", "explicit-share",
      "team-discussion", "team-preference"
    ],
    "excludedTopics": ["acquisition planning", "unannounced customer names"],
    "minConfidence": 0.95
  }
}
```

Merge this into your existing connection and private `writeScope` configuration.
The classifier uses a separate provider request with Pi's authentication. It
receives no Memory Worlds credential or tools, and has no implicit fallback to
another model. See [settings and bounds](reference/chat-sharing.md#configure-a-lightweight-model).

For explicit markers only, use `"categories":["explicit-share"]` and mark a user
message with `[shareable]` or `#shareable`. Assistant markers cannot authorize
publication, and exclusions still apply.

## Enable a mode for one chat

Use `/memory discover` to find a shared destination your credential can publish
to. Select that world and scope in Pi:

```text
/memory sharing review TEAM_WORLD_ID SHARED_SCOPE_UUID
/memory sharing pending
/memory sharing approve PROPOSAL_ID
/memory sharing dismiss PROPOSAL_ID
```

After reviewing the behavior, enable automatic publication for the chosen
future messages, or stop it:

```text
/memory sharing auto TEAM_WORLD_ID SHARED_SCOPE_UUID
/memory sharing private
```

For an operator-configured remote publisher mount:

```text
/memory sharing review mount TEAM_MOUNT REMOTE_WORLD_ID
/memory sharing auto mount TEAM_MOUNT REMOTE_WORLD_ID
/memory sharing status
```

Rules start a future-only window. Previous transcript, tool results, retrieved
memory, attachments and system instructions are excluded. Forks and tree
navigation stop sharing. Shared episodes contain exact selected excerpts and
normal publication fields; private source/context pointers and classifier
reasoning stay private.

## Exclusions and context boundaries

Default exclusions cover credentials, personal information, HR and performance
reviews, rewards and compensation, private/off-record material, and personal
positive or negative judgments about people or projects. Custom `excludedTopics`
adds literal topic filters and classifier instructions. If any message in the
pending window is excluded or unreadable, the entire window remains private.

A rule fixes the source, credential, classifier policy and destination. Changed
bindings or unknown exposure stop processing. Local publication checks other
exposed scopes against the destination audience. Automatic remote publication
requires exposure confined to the selected own private source; use explicit
selected-text publication for mixed-authority work.

Confidence scores are routing heuristics. Filters do not detect every indirect
disclosure, and synthetic tests do not establish classifier accuracy. See the
[complete exclusion and exposure rules](reference/chat-sharing.md#exclusions-and-context-boundaries).

## Receipts and retries

Retries preserve the selected excerpt, source identity and destination; they do
not ask the classifier for replacement text. `/memory sharing retry` retries
pending work under the current rule and authorization.

Switching to `private` stops new and unsubmitted work. It does not recall accepted
shared episodes or cancel already accepted remote deliveries. Inspect those with
`/memory deliveries`. Graphiti extraction follows publication acceptance
separately. See [receipt details](reference/chat-sharing.md#receipts-and-retries).

## Rich Pi integration

Use `memory-worlds-pi/sharing` or a bounded `classifySharing` callback when
embedding the native extension. Callback output passes the same validation and
cannot widen permissions. See [the SDK boundary](reference/chat-sharing.md#rich-pi-integration).
