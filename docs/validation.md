# Validation

## Scope implementation, version 0.2.0

Executed on 2026-10-20 local time (2026-10-21 UTC), Linux ARM64:

| Check | Result |
| --- | --- |
| Python 3.15.0 complete default suite | 255 passed, 3 opt-in checks skipped |
| Real Graphiti/FalkorDB check, enabled separately | Passed extraction and entity/fact retrieval across two worlds and a private scope within one world |
| Final built-image and authenticated Compose checks | 2 passed; all 16 service source modules match the built image byte for byte |
| Node 26.12.0 / Pi 1.3.0 | 68 unit cases, TypeScript checks, and both actual Pi RPC smokes passed |
| Helm 4.3.1 / Kubernetes 1.37.0 | Six profile contracts and 22 invalid configurations checked; isolated kind and Helm lifecycle smokes passed |

Scope tests cover restricted recursive delegation, ancestor revocation and
expiry, default parent/sibling isolation, opt-in read-only coordinator access,
actual audience inclusion, context-bound UUID attribution, canonical source identity,
immutable source replay, scoped archive recovery, and graph extraction before
retrieval. Independent adversarial review has 13 executable cases.

The Helm smoke retains private invocation source and credentials through pod
replacement, upgrade, and uninstall/reinstall, then verifies scope isolation and
credential revocation. Native Pi tests exercise isolated SDK child construction,
scope exposure through forks/branches/reload, credential rotation, immutable
capture intents, bounded private spooling, and complete receipt validation.
They exercise the curation proposal contract without a paid model call.

These are local results. Hosted CI and artifact builds retain their own exact
source commit receipts. See [scope behavior](scopes.md) and the
[Pi Graphiti feature matrix](pi-graphiti-features.md).

## Coverage and limits

Current-contract tests exercise concurrent multi-agent capture, duplicate delivery,
conflicting identities, private-world and scope grants, read/write separation,
revocation, durable restart, projection recovery and rebuild, source backup,
branch-preserving import, checksummed exports, scoped restore, and malformed or
oversized archives. OIDC checks cover signatures, issuer/audience/expiry, key
rotation, live membership, and grant containment. Router tests exercise two real
daemons, durable offline publication, restart, recovery, and validated receipts.
SSH tests use a real temporary tunnel and still require API credentials.

Graph checks use the actual pinned Graphiti library and FalkorDB with synthetic
LLM and embedding responses. They verify ingestion, namespace isolation, entity
and fact retrieval, provenance, and graph rebuilds. They do not measure model
quality. Neptune driver contracts are checked offline; no AWS deployment has
been provisioned or live-tested.

Container and Kubernetes checks verify authentication, retained source and graph
storage, pod replacement, scoped source recovery, and credential revocation.
These checks do not establish multi-node availability or recovery from lost
storage. The supported SQLite deployment uses one authority process per data
volume.

The project maintains one current contract. There are no historical Memory Worlds
API/schema readers, migrations, or compatibility harnesses. See the repository
[agent instructions](../AGENTS.md).
