# Chat sharing modes

Private capture and shared publication have separate destinations and controls.
The native Pi extension can classify future messages and publish eligible
excerpts under a rule enabled by the user for that chat. The classifier selects
text; service credentials and the user's rule authorize publication.

| Mode | Classifier | Publication |
| --- | --- | --- |
| `private` | No sharing-classifier calls | Explicit selected-text commands only |
| `review` | Bounded future user/assistant messages | User approves a proposal ID |
| `auto` | Bounded future user/assistant messages | Eligible excerpts above the threshold; ambiguous proposals wait for review |

Every chat starts private unless it resumes its own valid saved rule. Forks and
tree navigation stop sharing. Configuration alone cannot enable a mode.
Automatic private capture is independent and need not be enabled.

## Configure a lightweight model

Merge a `sharing` section into `~/.pi/agent/memory-worlds.json`. Keep the existing
private `writeScope`, service URL, token file, and selected read scopes:

```json
{
  "sharing": {
    "classifier": { "provider": "anthropic", "model": "claude-haiku-5-5" },
    "categories": [
      "decision", "procedure", "technical-finding", "explicit-share",
      "team-discussion", "team-preference"
    ],
    "excludedTopics": ["acquisition planning", "unannounced customer names"],
    "minConfidence": 0.95,
    "timeoutMs": 10000,
    "maxInputChars": 12000,
    "maxExcerptChars": 1000
  }
}
```

Select a model available through your Pi model registry and authenticate through
Pi normally. Other installed catalog examples are `openai/gpt-6-luna` and
`openai-codex/gpt-6-luna`; their provider/model pairs must be configured explicitly.
The classifier uses the registry's request-time authentication and provider
configuration. It receives no Memory Worlds token or tools. There is no implicit
fallback to the main chat model or a virtual model router. The model call is a
separate request and may consume the selected provider's credits or quota.

Haiku's current model ID is documented in the
[Claude model overview](https://platform.claude.com/docs/en/models/overview).
Available Luna provider/model pairs depend on the installed Pi catalog and your
provider access. Synthetic integration tests do not establish a model's privacy
classification accuracy.

Default categories include project decisions, procedures, technical findings,
explicit opt-in excerpts, and durable team discussions and preferences. Preserve
attribution and uncertainty in the original message; the classifier cannot invent
an attribution or turn a hypothesis into a verified finding.

For explicit markers only, set `categories` to `["explicit-share"]` and mark a
user message with `[shareable]` or `#shareable`. Assistant-generated markers cannot
create user opt-in. Clean unmarked messages remain classifier context; only marked
user excerpts are eligible. Excluded material remains excluded even when marked.

## Enable a mode for one chat

Discover canonical IDs through the CLI before configuring Pi bindings:

```sh
mise run cli -- --token-file /private/agent.token discover --scopes
mise run cli -- --token-file /private/agent.token mounts --table
```

Interactive `/memory` also offers a sharing-mode selector and lists accessible
local shared scopes and publisher mounts by name.

Use these native Pi commands for a local shared scope:

```text
/memory sharing review TEAM_WORLD_ID SHARED_SCOPE_UUID
/memory sharing pending
/memory sharing approve PROPOSAL_ID
/memory sharing dismiss PROPOSAL_ID
/memory sharing auto TEAM_WORLD_ID SHARED_SCOPE_UUID
/memory sharing private
```

For a remote team or company world, choose an operator-configured publisher mount:

```text
/memory sharing review mount TEAM_MOUNT REMOTE_WORLD_ID
/memory sharing auto mount TEAM_MOUNT REMOTE_WORLD_ID
/memory sharing retry
/memory sharing status
```

Enabling or changing the rule starts a future-only window. Earlier messages,
copied fork history, tool results, attachments, system instructions, and retrieved
memory messages are not classifier inputs. The extension does not retrospectively
publish an existing transcript. It publishes exact validated excerpts, with at
most five decisions from one bounded classifier request.

Review proposals and their decisions remain in private session metadata. Approval
is an explicit selected-text publication. Auto mode publishes only permitted,
shareable excerpts meeting the rule's threshold; it holds ambiguous proposals for
review. Shared episodes contain only the selected text and normal publication
fields. Private source/context pointers and classifier reasoning are not sent
to the destination.

## Exclusions and context boundaries

Default exclusions cover obvious credentials, personal information, HR and
performance reviews, rewards and compensation, and personal positive or negative
judgments about people or projects. Private/off-record markers also exclude text.
Custom `excludedTopics` adds literal topic filters and classifier instructions.
These checks run before model submission and again on selected excerpts. A
shareable marker never overrides an exclusion.

If any message in the pending conversation window is excluded, the whole window
stays private. This avoids classifying an assistant reply after removing the
sensitive user message that explains it. Windows exceeding the input budget also
stay private in full. A window contains at most five messages and the configured
character budget. The next clean future window can be processed normally.
Unreadable text and user attachments also withhold their whole window rather
than leaving an assistant reply to be assessed without its source input.

The rule pins the private source scope, session/context, service, credential,
classifier/settings, and canonical destination. Changed bindings, unknown
exposure, or foreign authority exposure stop processing. For a local destination,
other exposed scopes must pass the service's audience-flow check. The private
source explicitly authorized by the rule is the selective-publication exception;
reading another private invocation does not authorize publication from it.

Remote authorities have no shared audience-containment protocol. Automated remote
processing therefore requires memory exposure confined to the selected own
private source. Use explicit selected-text publication for mixed-authority work.
Changing branches or compacting history cannot erase exposure records.

Classifier confidence is a routing heuristic. Review mode lets the user inspect
decisions; auto mode accepts the classifier's residual false-positive risk under
the explicitly selected rule. Deterministic filters are conservative checks, not
a complete detector of every secret, personnel opinion, or indirect disclosure.

## Receipts and retries

Private session entries retain source identities/hashes, classification decisions,
rule bindings, immutable publication intents, and returned receipts. Lost HTTP
acknowledgments retry the same excerpt, timestamp, source ID, and destination
without asking the model to generate a replacement. Every retry checks the active
rule and current authorization. Changing the rule cannot redirect an old intent.

Turning the mode private stops new and unsubmitted work. Publications already
accepted by the service, including queued remote deliveries, remain accepted.
The switch does not recall existing shared episodes or cancel accepted outbox
deliveries. Inspect remote delivery state with `/memory deliveries`.

Published excerpts queue Graphiti extraction normally. Publication acceptance and
graph extraction completion are distinct receipts, as with explicit publication.

## Rich Pi integration

`memory-worlds-pi/sharing` exports the classifier settings/parser, decision
validator, provider-backed classifier, and `ChatSharing` controller. Extensions
can supply `registerMemoryWorlds(pi, { classifySharing })` to use their own bounded
classification callback. Its output passes the same strict reference, category,
exact-excerpt, exclusion, and confidence validation. A callback cannot select the
destination or widen service permissions.
