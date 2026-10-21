# Agent instructions

Memory Worlds is a greenfield project. Implement the current design directly.
There are no released Memory Worlds contracts or deployed datasets that this
repository must preserve.

## One current contract

- Maintain one current API, source schema, archive format, and native Pi state
  format. Change the implementation, documentation, and fixtures together.
- Do not add backward compatibility layers, historical schema readers,
  automatic migrations, deprecated aliases, version negotiation, or silent
  fallbacks to earlier behavior unless the user explicitly requests them.
- Do not create or retain compatibility harnesses, frozen old-version models,
  tests against previous Memory Worlds releases, or replay exceptions for
  obsolete serialization. Test the current contract and its invariants.
- Remove superseded code and fixtures instead of preserving two paths. Reset
  disposable development data when a schema changes. If real data preservation
  becomes a requirement, get that requirement explicitly before designing it.
- Use current terminology. World-level selection is a convenience for the
  current world-shared scope, not a historical API mode.

## Current integrations and boundaries

- Pi Graphiti feature coverage is a product requirement. Implement useful graph
  actions as native functionality and test against the current supported Pi and
  Graphiti APIs. This does not require emulating historical Memory Worlds APIs
  or maintaining a separate compatibility subsystem.
- Every memory belongs to a world and a UUID scope. World-shared, run-shared,
  agent-persistent, and invocation-private are distinct audiences. UUIDs are
  identifiers; live delegated credentials and grants authorize access.
- Keep read selection separate from the write destination. Parent and sibling
  agents receive no implicit access to invocation-private memory. Broader
  publication requires an explicit authorized action.
- SQLite journals are the durable source. Graphiti performs graph extraction
  and retrieval in isolated scope namespaces. Verify these boundaries with
  meaningful current-contract tests.

## Repository hygiene

- Preserve unrelated local edits. Coordinate file ownership when using agents.
- Keep private chat exports, credentials, databases, and local state out of
  source distributions, containers, and published artifacts.
- Keep project names and documentation independent of any employer.
- Prefer current stable dependencies and reproducible lockfiles. Run the checks
  appropriate to the change and report the results accurately.
