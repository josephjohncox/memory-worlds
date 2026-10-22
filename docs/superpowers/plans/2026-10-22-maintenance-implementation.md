# Memory maintenance implementation

The user approved continuing the RFC in PR #2. Implement its recommended private
thread boundary directly. Keep this work on the PR branch, preserve main and the
running installation, and qualify the current contracts without compatibility code.

## First working delivery

- [x] Thread-private scopes, idempotent thread creation, explicit grants,
  revisions, expiry selection and concurrent append checks.
- [x] Bounded typed context bundles, lossless compression, exact digest/coverage
  receipts and source-journal durability before any context replacement.
- [x] Source-side derived-artifact dependencies, immediate read fences on input
  retraction/erasure and payload cleanup before erasure completion.
- [x] Selectable lightweight/Luna profiles and bounded structured provider calls.
- [x] Durable sync/async jobs, fencing, exact accepted-result recovery, current
  authorization, cancellation and budget accounting.
- [x] Authenticated HTTP, Python client, CLI and appropriate MCP tools.
- [x] Current-contract tests, independent reviews, docs and public source audit.

After committing, build the six distributions from the clean commit, push the
branch and update PR #2 with the actual checks, artifact binding and limits.
The PR records post-commit qualification; the running installation stays on main.

## Following deliveries

- [ ] Credential-independent, explicitly granted OIDC thread continuity and
  audited job-authority renewal; qualify refresh, late login, revocation,
  delegation and scope-flow behavior without widening agent audiences.
- [ ] Durable claim revisions and enrichment resolution beyond an assessment artifact.
- [ ] Native Pi archive-before-compaction handoff and restart reconciliation.
- [ ] Scheduled expiry/pruning and lifecycle automation under the existing graph
  maintenance barrier.
- [ ] Isolated recursive execution with shared descendant budgets; do not call
  ordinary summaries or retrieval a complete RLM executor.

Parallel ownership: thread/scope agent owns scope and thread persistence; jobs
agent owns job/profile/provider execution; client agent owns client/CLI/MCP and
native scope-kind recognition; root owns archives, dependency lifecycle, HTTP,
integration qualification and PR publication. All new artifacts need dependency
read protection in the first working delivery.
