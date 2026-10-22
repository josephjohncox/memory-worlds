# Recorded validation

[Documentation](README.md) · [Reproduce local checks](qualification.md)

Validation reports identify the source and environment exercised. Counts from
different snapshots or overlapping selections are separate results. The
[detailed record](reference/validation.md) retains the earlier runs and limits.

## Private thread maintenance

The PR #2 implementation passed 1,128 Python tests with 12 skips on Python 3.15.0,
with an isolated PostgreSQL 18.6 server enabled alongside SQLite. Native Pi passed
165 tests and all three actual runtime/service/sharing smokes; OpenCode passed 27
tests. Helm passed 10 rendered profiles and rejected 49 unsafe configurations.
The [maintenance record](reference/validation.md#private-thread-maintenance)
describes the exercised failure cases and infrastructure limits.

## Guided setup review fixes

Setup commit `1cef120431b8e121859079b0ea5f3561ab5f0fcf` was checked locally on
Linux ARM64 with the locked toolchain:

| Check | Recorded result |
| --- | --- |
| Python 3.15.0 | 806 passed; 31 infrastructure/backend-dependent checks skipped |
| Ruff, formatting and dependency consistency | Passed |
| Native Pi | 153 tests and TypeScript checks passed |
| Helm | Eight profiles passed; 40 unsafe configurations rejected |
| Docker setup acceptance | Graphiti/FalkorDB extraction/recall, interruption, repeat apply, restart, retained volumes and cleanup passed |
| Bootstrap/controller | Real mise preserved a caller-relative config filename with shell metacharacters |
| Source and distribution audits | Passed |

The setup checks include process-exit recovery, offline connection finalization,
unchanged-file refusal for unrelated pending state, adoption overrides, managed
hard links and doctor probes that disclose no bearer. The macOS native rename
branch was reviewed but not executed in this Linux run. See
[full setup evidence](reference/validation.md#guided-setup-review-fixes).

## Ordinary-user discovery

Local Keycloak/kind qualification passed 17 discovery checks. The
[receipts](validation/oidc-discovery/README.md) bind the tested image, installed
modules, harness and cleanup. CLI, MCP and native Pi checks are recorded
[separately](reference/validation.md#ordinary-user-discovery).

## Memory Worlds 0.3.0

Earlier qualification covered SQLite/PostgreSQL persistence, native FalkorDB
recovery, encrypted backups, Cilium traffic controls, identity and artifact
transport. See the [recorded results](reference/validation.md#memory-worlds-030)
for that snapshot's test counts and infrastructure.

## Evidence boundaries

Local emulators establish transport, schema, isolation, retry and provenance
behavior. They do not establish production model accuracy or live Okta, RDS or
Neptune behavior. Helm rendering checks configuration; enforcement and recovery
require running infrastructure. A private container receipt identifies its exact
CI-tested source and registry digest.

## Sharing and imports

Native Pi runtime checks cover private spooling, named agent contexts, source
attribution, import chunking and explicit sharing rules. See the
[detailed sharing/import record](reference/validation.md#sharing-and-imports).
