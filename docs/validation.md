# Validation

## Scope implementation, version 0.2.0

Executed on 2026-10-20 local time (2026-10-21 UTC), Linux ARM64:

| Check | Result |
| --- | --- |
| Python 3.15.0 complete default suite | 246 passed, 3 opt-in checks skipped |
| Real Graphiti/FalkorDB check, enabled separately | Passed extraction and entity/fact retrieval across two worlds and a private scope within one world |
| Final built-image and authenticated Compose checks | 2 passed; all 16 service source modules match the built image byte for byte |
| Node 26.12.0 / Pi 1.3.0 | 66 unit cases, TypeScript checks, and both actual Pi RPC smokes passed |
| Helm 4.3.1 / Kubernetes 1.37.0 | Six profile contracts and 22 invalid configurations checked; isolated kind and Helm lifecycle smokes passed |

Scope tests cover restricted recursive delegation, ancestor revocation and
expiry, default parent/sibling isolation, opt-in read-only coordinator access,
actual audience inclusion, context-bound UUID attribution, migration races,
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
[upstream compatibility matrix](pi-graphiti-compatibility.md).

## Earlier world-level baseline

Verified on 2026-10-20, Linux ARM64. These are executed local results, not hosted
CI results or production-scale performance claims.

| Runtime | Result | Included integration checks |
| --- | --- | --- |
| Python 3.15.0 | 139 passed, no skips | Real FalkorDB, HTTP/CLI subprocesses, real OIDC/JWKS, durable router restart, stdio MCP, SSH, container/Compose persistence, offline Neptune contracts |
| Python 3.14.8 | 133 passed, 2 skipped; final changed suites: 55 passed | Broad compatibility run plus final router/OIDC containment regression; container checks omitted because the image uses Python 3.15 |
| Node 26.12.0, Pi 1.3.0 | 23 tests and two runtime smokes passed | Native hooks/state/client, actual Pi commands, publication retry, actual Pi to Python 3.15 HTTP to SQLite |

The suite exercises concurrent agent capture and duplicate delivery, conflicting
identity rejection, private-world grants, read/write separation, credential
revocation, restart durability, interrupted projection recovery, generation
rebuild, changed embedding configuration, live backup, branch-preserving import,
checksummed export, transactional restore, and malformed/oversized archives.
It also exercises signature/issuer/audience/expiry rejection, key rotation,
private work contexts, live membership changes, group-state rollback rejection,
per-agent credential scope, mount ACLs, partial recall failures, bounded remote
responses, remote scope/authority checks, offline publication, abrupt daemon
shutdown, lost acknowledgments, duplicate suppression, and fresh-login retry.

The router process check runs two real daemon subprocesses and CLI clients.
It queues selected text while the remote is down, kills and restarts the local
daemon, observes background delivery after remote recovery, and verifies that
private evidence never reaches the shared destination. Per-agent and per-user
mount scopes remain independent.
Malformed/deeply nested remote JSON fails only its request or delivery; it does
not terminate the outbox worker. Malformed JWKS/JWT input is also contained.

Native Pi tests use the current public extension APIs. The runtime checks launch
the actual Pi CLI in RPC mode without model credentials. One validates native
commands/publication transport against a deterministic fixture; the other runs
capture and recall against the actual Python daemon and SQLite. The downloaded
Node binary was checked against the official SHA-256 manifest. Package output
contains only compiled extension/client files, documentation, and Apache-2.0.
An in-memory fork retains foreign authority exposure; unknown parent exposure
stays disabled through reload. Independent new sessions start clean.

The live graph test uses Graphiti 0.31.2 against FalkorDB 6.0.0 with deterministic
typed extraction and embedding clients. It performs actual graph writes,
retrieval, world isolation, provenance lookup, and replacement-generation
activation. It does not measure real-model extraction quality or recall.

The SSH check launches an unprivileged temporary OpenSSH instance and a real
`ssh -N -L` tunnel. Missing API credentials still return 401; an agent credential
captures and retrieves through the tunnel. Disposable keys, SSH configuration,
and processes are removed; no host SSH configuration is modified.

The image builds with locked dependencies and a digest-pinned Python 3.15 base.
Its smoke test uses a non-root process, read-only root filesystem, dropped
capabilities, a private persistent volume, and a loopback HTTP port. Captured
evidence survives a container restart. The image excludes private conversation
exports, credentials, local configuration directories, and source bytecode.
Both Compose files pass `docker compose config --quiet`.

The graph Compose deployment additionally passed an isolated live check: absent
password rejects configuration; anonymous database access fails; authenticated
queries and graph data survive restart; AOF and fsync settings match the file.
The password is absent from process arguments. Its generated configuration has
mode 0600 inside a mode-0700 tmpfs directory. The deployment test cleans only its
own temporary project and volume.

The clean kind 0.33.0 / Kubernetes 1.37.0 run verified API/graph authentication,
source capture/replay, and both source/native graph continuity after replacing
both StatefulSet pods on their retained PVCs. See the public
[receipt](../deploy/kind/validation.json) for image IDs, module and manifest hashes,
and exact scope. The final image includes subsequent authentication/router
parser-containment changes; those pass the complete application/container suite.
The kind smoke does not exercise those parser failures or claim CNI enforcement.
Temporary test clusters were removed and the host inotify limit restored.

The final `memory-worlds:local` image is
`sha256:93df37482ae9875ee7da37185a4b4512fe12505f44bf389297878d3716cad296`.
Its installed module hashes match the final Python sources. Optional Neptune
dependencies import successfully on Python 3.15; compilation occurs in a separate
builder stage, with no compiler in the runtime image.

Ruff lint and format checks pass. Source and wheel archives were inspected for
private conversation inputs, credentials, internal instructions, cached bytecode,
local user paths, and organization branding. Package construction uses explicit
include lists; the container context uses an allowlist.

Reproduce the live suite after starting `deploy/compose.graph.yaml` and building
the image as `memory-worlds:local`:

```sh
MEMORY_WORLDS_RUN_FALKORDB_TEST=1 \
MEMORY_WORLDS_TEST_FALKORDB_PORT=16379 \
MEMORY_WORLDS_RUN_CONTAINER_TEST=1 \
uv run --frozen --all-extras pytest -q
```

Without the opt-in environment variables, the live graph and container tests
skip explicitly. The SSH test skips if its required local programs or an
unprivileged account are unavailable. The initial hosted
[CI run](https://github.com/josephjohncox/memory-worlds/actions/runs/39523640561)
passed at source commit `d321cd0f5443fb30d46896f0d84f417a2c1cc4e0`, covering Python
3.14/3.15, the real Pi runtime, and Kubernetes pod replacement. The Helm and
artifact tooling extends that baseline with its own checks and workflow runs.

## Dependency versions

Current stable versions were checked directly against upstream release metadata
and installed into the lockfile. Runtime/backend versions are fixed for
reproducibility rather than following mutable `latest` image tags.

| Component | Selected version | Upstream |
| --- | --- | --- |
| Python default | 3.15.0 | [Python release](https://www.python.org/downloads/release/python-3150/) |
| uv | 0.13.4 | [PyPI](https://pypi.org/project/uv/0.13.4/) |
| Hatchling build backend | 1.32.4 | [PyPI](https://pypi.org/project/hatchling/1.32.4/) |
| FastAPI | 0.156.4 | [PyPI](https://pypi.org/project/fastapi/0.156.4/) |
| Uvicorn | 0.54.0 | [PyPI](https://pypi.org/project/uvicorn/0.54.0/) |
| HTTPX | 0.28.1 | [PyPI](https://pypi.org/project/httpx/0.28.1/) |
| Pydantic | 2.14.1 | [PyPI](https://pypi.org/project/pydantic/2.14.1/) |
| Graphiti | 0.31.2 | [PyPI](https://pypi.org/project/graphiti-core/0.31.2/) |
| MCP SDK | 2.3.0 | [PyPI](https://pypi.org/project/mcp/2.3.0/) |
| FalkorDB | 6.0.0 | [Official releases](https://github.com/FalkorDB/FalkorDB/releases) |
| pytest | 9.1.1 | [PyPI](https://pypi.org/project/pytest/9.1.1/) |
| pytest-asyncio | 1.4.0 | [PyPI](https://pypi.org/project/pytest-asyncio/1.4.0/) |
| Ruff | 0.17.1 | [PyPI](https://pypi.org/project/ruff/0.17.1/) |
| PyJWT | 2.15.1 | [PyPI](https://pypi.org/project/PyJWT/2.15.1/) |
| Node | 26.12.0 | [Official current release](https://nodejs.org/en/download/current) |
| npm | 11.20.0 | Bundled with the verified Node release |
| Pi agent and TUI | 1.3.0 | [Official toolkit](https://github.com/earendil-works/pi) |
| TypeScript | 7.0.2 | [Official package](https://www.npmjs.com/package/typescript/v/7.0.2) |
| TypeBox | 1.4.2 | [Official package](https://www.npmjs.com/package/typebox/v/1.4.2) |
| kind | 0.33.0 | [Official release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0) |
| Helm | 4.3.1 | [Official release](https://github.com/helm/helm/releases/tag/v4.3.1) |
| kubectl / kind node | 1.37.1 / 1.37.0 | [Kubernetes releases](https://kubernetes.io/releases/) |

## Remaining scope

The Neptune/OpenSearch adapter is experimental: no live AWS services were
provisioned or tested. The shared service remains single-replica SQLite, with
retained local/block-backed volumes. Network policies require a CNI that
enforces them; ordinary kindnet does not establish that evidence.
Source erasure, old graph-generation garbage collection,
coordinated exact graph/authority snapshot restoration, real-model quality
evaluation, and cloud-provider OAuth are also outside the implemented contract.
Portable source recovery and consistent SQLite backups are implemented.
