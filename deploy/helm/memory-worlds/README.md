# Memory Worlds Helm chart

Installs one API StatefulSet and one authenticated FalkorDB StatefulSet by
default. Each owns a separate retained PVC. Requires Kubernetes 1.32 or later
and a StorageClass providing local or block-backed storage compatible with
SQLite WAL. The chart has no external chart dependencies.

The default API image is `memory-worlds:local`; load it into kind or select your
registry image. Graphiti extraction and retrieval use FalkorDB by default.
Create the FalkorDB password and runtime provider/model Secrets before installing:

```sh
mise run helm -- --kube-context YOUR_CONTEXT upgrade --install memory-worlds . \
  --namespace memory-worlds --create-namespace \
  --set runtime.existingSecret=memory-worlds-runtime \
  --set networkPolicy.allowExternalHTTPS=true --wait --timeout 5m
mise run helm -- --kube-context YOUR_CONTEXT -n memory-worlds test memory-worlds
```

No model/provider is chosen implicitly. Missing extraction/embedding settings
stop startup. `graph.backend=disabled` with `falkordb.enabled=false` is an explicit
maintenance/recovery mode; it does not supply graph recall.

Run `memory-worlds init` once inside the API pod and redirect its one-time
administrative receipt into a private operator file. No bootstrap hooks run and
no credentials are generated or rendered by Helm. See the full
[installation, initialization, upgrade, and recovery guide](../../../docs/helm.md).

| Value | Default | Purpose |
| --- | --- | --- |
| `replicaCount` | `1` | Enforced single API owner; additional replicas are rejected |
| `image.repository`, `image.tag`, `image.digest` | `memory-worlds`, `local`, empty | API image; a nonempty SHA-256 digest pins its contents |
| `imagePullSecrets` | `[]` | Existing registry Secrets, shared by API, graph, and test pods |
| `runtime.existingSecret` | empty, required | Explicit provider/model and optional OIDC settings |
| `graph.backend` | `falkordb` | Graphiti backend; experimental `neptune`, or explicit maintenance `disabled` |
| `persistence.size`, `persistence.storageClass` | `10Gi`, default class | API source/control/outbox volume |
| `persistence.existingClaim` | empty | Operator-owned claim instead of a volume claim template |
| `falkordb.enabled`, `falkordb.replicaCount` | `true`, `1` | Private graph StatefulSet; never exposed by ingress |
| `falkordb.auth.existingSecret`, `passwordKey` | `memory-worlds-falkordb`, `password` | Existing Secret holding exactly 64 hexadecimal characters |
| `falkordb.persistence.*` | same as API | Independent graph storage |
| `router.enabled` | `false` | Mount existing router configuration and token Secrets |
| `router.mountsExistingSecret`, `tokensExistingSecret` | empty | Required when router is enabled; both mount read-only with mode 0440 |
| `ingress.enabled` | `false` | Requires explicit class, hosts, and TLS Secret references |
| `networkPolicy.enabled` | `true` | Release-scoped isolation; requires an enforcing CNI |
| `networkPolicy.allowExternalHTTPS` | `false` | Explicitly permit configured HTTPS CIDRs for model/OIDC/remote traffic |
| `networkPolicy.extraAPIEgress` | `[]` | Explicit peers and ports for private endpoints or local models |
| `serviceAccount.annotations` | `{}` | Existing workload identity integration; no RBAC is granted |
| `tests.enabled` | `true` | Read-only readiness and unauthenticated-access Helm test |

Both pods run without root, privilege escalation, writable root filesystems,
Linux capabilities, or automatically mounted Kubernetes API tokens. Temporary
volumes and resources are bounded. API probes use `/readyz` and `/healthz`;
FalkorDB probes authenticate through its Secret-backed environment. Its private
configuration is generated inside tmpfs, keeping the password out of argv.

Selectors include release and component labels. The default deny policy selects
only this release, preserving other workloads in the namespace. API ingress
requires both the configured namespace and pod selectors; label the trusted
gateway accordingly. The Helm test can reach only its own release's API.
Default kind networking does not enforce NetworkPolicy.

`helm uninstall` retains claims created by the StatefulSets and never owns
pre-existing Secrets or PVCs. Retention does not back up storage or protect it
from node loss. Never increase replicas or move live SQLite files to a shared
network filesystem. See [deployment recovery limits](../../../docs/deployments.md).

Validation uses Helm 4.3.1, the latest stable release verified on 2026-10-20 from
the [official release](https://github.com/helm/helm/releases/tag/v4.3.1).
Run `mise run helm:check` from the repository root for profile rendering,
schema rejection, security, release isolation, and storage contract checks.

Licensed under Apache-2.0; [LICENSE](LICENSE) is included in packaged charts.

For AWS without Neptune, use the normal FalkorDB/PVC deployment. RDS PostgreSQL
with pgvector is not a supported Graphiti backend; see the
[RDS assessment](../../../docs/rds-postgresql.md).
