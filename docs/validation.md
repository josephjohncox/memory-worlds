# Validation

## Ordinary-user discovery

The discovery follow-up passed 100 targeted Python tests covering signed OIDC
identities, HTTP/Python/CLI discovery, MCP, scope clients and the qualification
harness. Native Pi passed 153 tests, TypeScript checks and all three actual
runtime/service/sharing smokes. Five actual CLI/stdio integration cases also
passed, including discovery with an ordinary agent credential. An actual isolated Pi model exercised delegated
discovery and continued after sanitized credential-file, transport and malformed
JSON errors. MCP validation errors disclose private input in neither tool output
nor server error logs. Explicit `include_scopes: false` also works with a selected
world; the CLI explicitly includes scopes for its documented `--world` convenience.
Pi accepts the API's 16,384-character bearer-token limit, including private token
files with LF or CRLF endings; larger group-bearing tokens are exercised through
discovery without exposing them in catalog results.

The final local image passed all 17 Keycloak 26.8.1 checks in kind with Cilium
1.20.3, including actual device-flow CLI discovery and aliases, private/sibling
metadata isolation, delegated catalog limits and group removal. Its installed
29 Python modules match the current service source byte for byte. The
[qualification receipts](validation/oidc-discovery/README.md) retain module and
harness hashes, the actual image/config binding and cleanup evidence. These are
local protocol checks using generated identities, without a live Okta tenant.

## Memory Worlds 0.3.0

Executed locally on Linux ARM64 with the locked mise toolchain. Hosted CI and
release assets carry their own exact source commit and image digest receipts.

| Check | Executed result |
| --- | --- |
| Python 3.15.0 complete suite with local PostgreSQL, native graph recovery and rclone | 553 passed; 10 backend-specific or separately enabled infrastructure checks skipped |
| Native PostgreSQL 18.6 and SQLite persistence | Competing claims, live heartbeats, process death, stale external completions, two HTTP replicas, pool exhaustion and maintenance cancellation/drain passed |
| Encrypted recovery | Private credentials/grants/scopes, sequences, tombstones, extraction intent, malformed archives and fresh-target rollback passed; SQLite → PostgreSQL → SQLite round trip passed |
| Native FalkorDB 6.0.0 recovery | Exact original namespaces, vector search, full-text indexes, edges and source provenance survived recovery into a fresh backend |
| Node 26.12.0 / Pi 1.3.0 | 138 unit cases, TypeScript checks and all three actual Pi runtime/service/sharing smokes passed |
| Helm 4.3.1 | Eight profile contracts passed; 40 unsafe configurations independently rejected |
| Local OIDC | Actual Keycloak 26.8.1 in kind passed 12 access, device-login, group-removal, privacy and revocation checks |
| Graphiti 0.31.2 | Actual extraction and retrieval against FalkorDB passed four contradictory scope fixtures with local HTTP chat/embedding emulators |
| Sharing evaluation | Labeled local registry/provider corpus exercised private/review/auto controls with no external calls |
| Network policy | Pinned Cilium 1.20.3 in an owned kind cluster passed allowed and denied ingress/egress checks with positive controls |
| Artifact transport | Pinned rclone 1.75.2 local backend passed authenticated upload, exact replay, downloaded-byte comparison and conflicting-object preservation |

The final kind application qualification uses the current PostgreSQL Helm
profile, two API replicas, real FalkorDB and local model HTTP emulators. Its
receipt records pod replacement, fenced work, native backup, scheduled backup,
source erasure and stopped-writer purge, followed by recovery into fresh source
and graph volumes. Use the receipt's result and image identity when assessing
that run; chart rendering alone is not disaster-recovery evidence.

See [local qualification](qualification.md), [recovery](recovery.md),
[lifecycle](lifecycle.md), [agent scopes](scopes.md) and
[Pi feature coverage](pi-graphiti-features.md) for reproducible contracts.

## Evidence boundaries

Local model emulators exercise actual Graphiti/Pi transport, extraction schemas,
namespace isolation, recall and publication controls. They do not measure a
production model's extraction or classification accuracy. The user replaced live
qualification with a local OIDC server and kind. No live Okta tenant, RDS service,
Neptune/OpenSearch deployment or additional paid-model run is an acceptance
requirement. Neptune remains experimental; scoped erasure rejects unsupported
secondary-index cleanup rather than reporting completion.

SQLite runs one owning daemon per data directory. PostgreSQL replicas share
relational journals with server-clock leases, fencing and authority-wide
maintenance. Local PostgreSQL concurrency tests establish those contracts;
kind qualification exercises the rendered deployment. Neither is a measurement
of a production RDS deployment's availability.

Independent adversarial review found and fixed lost-acknowledgment graph restore
cleanup, omitted active graph inventory, PostgreSQL selector validation,
database-aware readiness, guard/DAL pool self-deadlock, and cross-process SQLite
maintenance. Actual subprocess tests drain an existing writer, reject a new
writer during the cut, permit it afterward, and restore exactly the pre-cut source.
Regression tests
retain those current failure boundaries. The project has one current API,
schema, archive format and Pi state contract, with no historical readers,
automatic migrations or compatibility harnesses.

## Sharing and imports

Native Pi smokes execute the installed public extension APIs, SDK and actual
HTTP service. They cover named agents across independent chats, live world/scope
aliases, scope exposure through forks/reloads, credential attenuation/revocation,
private spooling, source attribution and complete receipts. The credential-free
Python planner is exercised through private process pipes; long rendered chat
snapshots round-trip losslessly through native chunked import and exact replay.

The sharing smoke uses a synthetic provider through the real Pi registry. It
checks lost acknowledgments and immutable retry without reclassification,
review approval, live revocation, whole-window sensitive-context exclusion,
Unicode boundaries, future-only mode activation, and omission of private pointers
from publication requests. Classifiers select excerpts inside a user-authorized
rule; they never choose destinations or grant access.
