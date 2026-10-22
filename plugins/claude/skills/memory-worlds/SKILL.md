---
name: memory-worlds
description: Use when the user asks to find, recall, capture or publish knowledge through Memory Worlds. Discover authorized spaces and choose explicit scopes before using memory tools.
---

Use the plugin's Memory Worlds MCP tools to work with durable memory.

1. Call `memory_discover` to inspect the caller's authorized worlds and scopes.
   Names and UUIDs identify destinations; they do not grant access.
2. Search only the scopes relevant to the user's task. Keep source authority,
   world, scope and episode attribution with retrieved evidence.
3. Capture only explicitly selected useful text in the intended private scope.
   Do not upload a transcript, tool output, retrieved evidence or system prompt
   merely because this plugin is enabled. Keep reads separate from writes.
4. Publish to team/company worlds only when the user has authorized the specific
   information and destination. Group membership does not authorize automatic
   sharing. When private retrieved context contributes, call `memory_scope_flow`
   with its read scopes and the selected write scope. The result reports audience
   containment, not permission to publish; broader publication still requires
   the user's authorization for the selected information and destination.
5. Preserve the full original write request on retry, including source ID,
   timestamp, context, metadata, destination and content. Report its receipt.

Private invocation memory stays private unless separately granted. Never ask a
child agent to use an operator credential or infer access from its parent's name.
This MCP integration has no automatic session capture or Pi exposure ledger.
