# Deployments

The [Helm chart](helm.md) supports a single-replica SQLite StatefulSet or
PostgreSQL-backed API replicas, with validated values, existing Secret references,
release-specific networking, and optional ingress. The Kustomize base below uses
the single-replica SQLite topology.

Memory Worlds runs the same authenticated API, scoped-world authorization,
router, and durable publication outbox locally and in Kubernetes. In the
Kustomize base, one API pod owns its SQLite directory, and FalkorDB has a separate
pod and volume. This topology provides persistent storage with one API replica.
Do not scale the SQLite API or mount SQLite WAL on a shared network filesystem.
For multiple API replicas, use the [PostgreSQL Helm profile](helm.md#postgresql-and-rds),
which stores source, control, and outbox journals in PostgreSQL.

The [local and corporate memory guide](hybrid.md) covers using a personal daemon
alongside a shared deployment.

## Local Kubernetes with kind

Docker and mise are required. The repository pins Python, kind, kubectl, Helm,
and their supporting tools; `mise.lock` records verified tool distributions.
The isolated smoke uses kind 0.33.0, kubectl 1.37.1, and the official kind 1.37.0
node image pinned by digest. Run the complete source/native graph/Helm lifecycle
check with:

```sh
mise install --locked
mise run deps
mise run kind:smoke
```

For an installation that you intend to use, configure explicit extraction and
embedding providers in a private copy of `deploy/k8s/runtime.env.example`, then
pass it to `mise run kind:deploy -- --runtime-env /private/operator/runtime.env`.
Missing provider/model configuration fails startup. No commercial model or
external endpoint is selected implicitly.

The script builds this checkout, creates a randomly named isolated cluster,
loads the API and pinned FalkorDB images, generates a database password, applies
the manifests, and tests the running service. Credentials, the explicit
kubeconfig, and test receipts go into a mode-0700 directory outside the
repository. It prints that directory. No current kube-context is changed.
`MEMORY_WORLDS_KIND_STATE=/outside/repo/new-directory` selects a durable location.
Add `--delete` for CI: the script removes only the cluster it created, retaining
the private test receipts. Deleting kind destroys its node-local volumes.

The smoke initializes one synthetic owner, verifies API and database
authentication, captures an episode and idempotent replay, writes a deterministic
native graph fixture, replaces both pods, and verifies the source and graph
remain available on their original PVCs. This proves pod replacement continuity.
It does not prove restoration after node/volume loss, model extraction quality,
or network isolation. The smoke supplies explicit synthetic provider settings and uses requests that
disable extraction. It tests source and native graph persistence without
calling a model; ordinary deployment uses Graphiti extraction by default.

With Helm installed, add `--helm-smoke` to exercise chart installation, the
read-only Helm test, source/graph persistence through pod replacement and an
upgrade, and retained PVC reuse through uninstall/reinstall. Credentials stay
in the private state directory. CI runs both deployment paths with `--delete`.

The deployment smoke passed through `mise run kind:smoke` on 2026-10-20
(local date), using kind 0.33.0, Kubernetes 1.37.0, kubectl 1.37.1, and Python
3.15.0 on Linux ARM64. The [kind receipt](../deploy/kind/validation.json) identifies
the tested image, installed source and script hashes, and persistence checks.
The [Helm receipt](../deploy/kind/helm-validation.json) records chart/PVC retention,
private invocation isolation, and revocation. Synthetic model endpoints are
configured in these lifecycle checks; their source operations explicitly skip
model extraction. Separate real Graphiti/FalkorDB tests verify extraction and
retrieval. The local test temporarily raised its exhausted inotify limit from
128 to 1024, restored 128 afterward, and deleted its owned cluster. The deployment
script itself does not change host kernel settings.

Connect to a retained test cluster with its printed values:

```sh
mise run kubectl -- --kubeconfig /private/state/kubeconfig \
  --context kind-memory-worlds-CLUSTER -n memory-worlds \
  port-forward --address=127.0.0.1 service/memory-worlds 8787:8787
```

The smoke's `bootstrap.json` is private administrative material. Issue separate
revocable, world-scoped client credentials for normal use. Remote agents can use
the same authenticated API through SSH forwarding to this loopback endpoint;
see the main README for both tunnel directions.

## Kubernetes configuration

`deploy/k8s/base` provides a namespace, internal ClusterIP services, API and
FalkorDB StatefulSets, retained 10-GiB PVCs, startup/readiness/liveness probes,
resource limits, and network policies. API UID/GID 10001 and graph UID/GID 999
run without root, capabilities, privilege escalation, writable root filesystems,
or automatic service-account credentials. `fsGroup` supplies volume access;
the API creates its private directory below the mounted volume. The CSI driver
must support the requested volume ownership and a local/block-backed filesystem.
Set a StorageClass and capacity appropriate to the target cluster in an overlay.

Secrets are deliberately absent from source. On a new target, use an explicit
kubeconfig/context on every command and create these secrets before applying
the workload manifests:

```sh
mise run kubectl -- --kubeconfig "$config" --context "$context" apply \
  -f deploy/k8s/base/namespace.yaml
mise run kubectl -- --kubeconfig "$config" --context "$context" -n memory-worlds \
  create secret generic memory-worlds-falkordb \
  --from-file=password=/private/config/falkordb-password
mise run kubectl -- --kubeconfig "$config" --context "$context" -n memory-worlds \
  create secret generic memory-worlds-runtime \
  --from-env-file=/private/config/runtime.env
mise run kubectl -- --kubeconfig "$config" --context "$context" apply -k deploy/k8s/base
```

The database password file must contain 64 random hexadecimal characters. Use a
private copy of `deploy/k8s/runtime.env.example` for service configuration. Keep
plaintext files outside synchronized source checkouts and provision Kubernetes
secret encryption/access according to the target cluster's operating policy.
For a registry deployment, replace `memory-worlds:local` with the built image's
immutable registry digest in a Kustomize overlay. The kind script does that
replacement with a unique locally loaded image tag.

Initialize an empty deployment once with `kubectl exec memory-worlds-0 --
memory-worlds init`; capture its JSON output directly to a mode-0600 private
operator file. Initialization is not in a pod init container, so a one-time
administrator credential never appears in pod logs. Reinitialization of an
existing authority fails rather than rotating or printing another credential.

The base deployment selects Graphiti/FalkorDB and injects its password from the
database Secret. Populate all provider/model settings before deployment. Local
models inside the cluster need an explicit egress rule for their labeled pods
and port; the default policies permit DNS, FalkorDB, and HTTPS only. Liveness
tests the HTTP process. Readiness tests the source service and required
background tasks; graph degradation is reported by the graph API while durable
capture remains available.

`deploy/k8s/overlays/work` adds read-only router files:
`memory-worlds-mounts` supplies `mounts.json` at `/config/mounts.json`, and
`memory-worlds-remote-tokens` supplies per-remote credentials below
`/secrets/remotes`. Use those paths in the router mount configuration described
in the router documentation. The router runs inside the API process and stores
its outbox on the same retained volume. A separate router replica is unnecessary.
Mounted remote credentials must be scoped on their destination authorities.

OIDC requires `MEMORY_WORLDS_OIDC_ISSUER`, `MEMORY_WORLDS_OIDC_AUDIENCE`, and
`MEMORY_WORLDS_OIDC_JWKS_URL` together. Each verified issuer/subject receives a
private work context. Team/company/ad-hoc access comes from explicit memberships
or configured group-to-world mappings. IdP claim names do not grant
administration. OIDC authenticates users; world membership still authorizes each
operation. Token verification settings and group mappings belong in the runtime
secret or equivalent operator-managed configuration.
When group mappings are configured, access tokens must include a signed `iat`;
older accepted group states and conflicting same-time states are rejected. A
mapping change takes effect on the user's next authentication. Explicit manual
memberships remain independent of mapped memberships.

The services remain internal. A shared installation needs an existing TLS
gateway/ingress and its certificate configuration before exposing the API.
The ingress policy accepts only pods and namespaces both labeled
`memory-worlds-client: "true"`; apply those labels to the chosen trusted gateway.
No raw database port is exposed externally. The default HTTPS egress rule is
broad because standard NetworkPolicy cannot select DNS names; narrow it using
an egress gateway or the target CNI's FQDN policy.

Network policies require a policy-enforcing CNI. The local qualification cluster
uses pinned Cilium and verifies allowed API traffic, denied clients and denied
graph traffic with positive controls. Default kindnet does not enforce policy.
Run the same checks for the CNI and network configuration used in a deployment.

## Backups and recovery

PVC retention keeps data through workload deletion/scaling. It is not a backup.
Node-local kind storage disappears with the node. For portable source recovery,
use the existing authenticated world export/restore CLI; copy only a finalized
artifact to object storage or a synchronized folder. Exports exclude live
credentials and grants. Re-extract graphs after portable source restoration.
The administrator `authority-backup` CLI creates and downloads an encrypted
authority cut, including credentials, every private/shared scope, durable jobs,
outbox and native graph namespaces. See [recovery](recovery.md).

FalkorDB uses AOF with `appendfsync everysec` and periodic RDB snapshots. A host
failure can lose approximately the last second of graph writes; source receipts
remain governed by their independent SQLite commit. If a graph write outcome
is unknown, the conservative worker stops that scope's graph use and requires
an isolated rebuild. A later empty or stale graph backend is not evidence that
the acknowledged source was lost.

The same API data volume retains scope journals, UUID identities, context
bindings, and delegated credential state. The Helm lifecycle smoke also checks
private invocation data and parent/sibling isolation after pod replacement,
upgrade, and uninstall/reinstall. See [agent scopes](scopes.md) for the data and
authorization model. Scope isolation does not require a pod or PVC per agent.

Whole-authority recovery is implemented for fresh SQLite and PostgreSQL targets.
Native FalkorDB restore preserves original keys, vectors, indexes and provenance
on an isolated empty backend. A source-only backup explicitly requests fresh
extraction instead. Maintenance coordinates the cut across replicas; stale
worker claims are cleared on recovery. Portable world restore continues to copy
selected evidence without activating archived grants. See [lifecycle](lifecycle.md)
for deletion receipts and independent backup retention.

## AWS without Neptune: FalkorDB, and the RDS boundary

Use RDS PostgreSQL for shared source/control/outbox journals and FalkorDB for
Graphiti extraction and retrieval. The chart's PostgreSQL profile uses multiple
API replicas, authority-wide maintenance locks and fenced per-scope workers.
SQLite remains the single-daemon local/PVC profile. No PostgreSQL extension is
required for the current relational journals. pgvector does not replace the
pinned Graphiti graph driver.

The [RDS PostgreSQL guide](rds-postgresql.md) covers TLS, stable authority identity,
schema ownership, deployment configuration and extension discovery.

## AWS Neptune evaluation lane

`MEMORY_WORLDS_GRAPH_BACKEND=neptune` selects Graphiti 0.31.2's Neptune driver.
Install `memory-worlds[neptune]`, configure extraction/embedding providers, and use
the values illustrated in `deploy/k8s/neptune.env.example`. `NEPTUNE_HOST` accepts
`neptune-db://endpoint` for Neptune Database or `neptune-graph://graph-id` for
Neptune Analytics. `AOSS_HOST` is a bare OpenSearch Serverless hostname. Ports are
explicitly configurable. The AWS SDK credential chain supplies identity;
projection exports never contain AWS credentials.

This lane is experimental. Configuration, constructor wiring, namespace checks,
and event-loop isolation are tested offline. No paid AWS infrastructure was
provisioned and no live Neptune/OpenSearch ingestion or restore was run. The
FalkorDB deployment is the validated local Kubernetes backend.

Neptune is not a single-URL replacement for the graph pod. A real installation
requires Neptune, an OpenSearch Serverless collection, VPC networking/security
groups, AWS region and workload IAM identity, collection encryption/network/data
access policies, plus the authoritative source/control store. Neptune Database
normally uses TCP 8182; add scoped egress to the private endpoint. The base
manifest disables service-account token mounting; EKS workload identity needs a
dedicated service account and the appropriate projected identity token. Neither
long-lived AWS access keys nor cluster-wide administrator roles are needed.

`deploy/k8s/overlays/neptune` removes the FalkorDB StatefulSet/Service and selects
the Neptune backend; supply its endpoints and provider settings through the
runtime secret. Add scoped Neptune egress and workload identity in an
installation-specific overlay, then test it against your existing VPC, identity
mechanism, and storage class. Choose the PostgreSQL profile for shared relational
durability; managed graph availability alone does not provide that durability.

The pinned upstream driver creates four fixed OpenSearch indices, performs
network calls synchronously inside async methods, waits after index creation,
and computes some vector similarity in Python after fetching embeddings. The
adapter isolates its complete Neptune client lifecycle on a dedicated loop
thread so those calls cannot stall source capture. The adapter also bypasses
upstream query-error logging that includes complete
source-bearing parameters and explicitly closes its OpenSearch connection.
These checks do not prove AWS performance, cancellation latency, or cross-world search
quality. Its namespace presence check detects an empty namespace, not a stale or
partially restored backend. Use a dedicated collection/backend per authority,
and validate isolation, indexing, load, credential refresh, and recovery on real
AWS before promoting this lane to a shared deployment.

Upstream references:

- [kind 0.33.0 release and node digests](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0)
- [Kubernetes supported releases](https://kubernetes.io/releases/)
- [NetworkPolicy prerequisites](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/)
- [FalkorDB persistence behavior](https://docs.falkordb.com/operations/durability/)
- [Graphiti Neptune and OpenSearch requirements](https://help.getzep.com/graphiti/configuration/neptune-configuration)
- [Neptune IAM data-access actions](https://docs.aws.amazon.com/neptune/latest/userguide/iam-dp-actions.html)
- [OpenSearch Serverless data-access policy requirements](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-data-access.html)
