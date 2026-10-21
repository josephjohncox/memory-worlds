# Memory Worlds Helm chart

Installs one API StatefulSet and, optionally, one authenticated FalkorDB
StatefulSet. Each owns a separate retained PVC. Requires Kubernetes 1.32 or later
and a StorageClass providing local or block-backed storage compatible with
SQLite WAL. The chart has no external chart dependencies.

The default API image is `memory-worlds:local`; load it into kind or select your
registry image. Projection defaults to `disabled`: installing the chart never
selects a model provider or sends source content to one. FalkorDB is enabled by
default and needs an existing password Secret even while projection is disabled.
Disable it for a source-only installation.

```sh
helm --kube-context YOUR_CONTEXT upgrade --install memory-worlds . \
  --namespace memory-worlds --create-namespace \
  --set falkordb.enabled=false --wait --timeout 5m
helm --kube-context YOUR_CONTEXT -n memory-worlds test memory-worlds
```

Run `memory-worlds init` once inside the API pod and redirect its one-time
administrative receipt into a private operator file. No bootstrap hooks run and
no credentials are generated or rendered by Helm. See the full
[installation, initialization, upgrade, and recovery guide](../../../docs/helm.md).

| Value | Default | Purpose |
| --- | --- | --- |
| `replicaCount` | `1` | Enforced single API owner; additional replicas are rejected |
| `image.repository`, `image.tag`, `image.digest` | `memory-worlds`, `local`, empty | API image; a nonempty SHA-256 digest pins its contents |
| `imagePullSecrets` | `[]` | Existing registry Secrets, shared by API, graph, and test pods |
| `runtime.existingSecret` | empty | Provider/model and OIDC environment configuration |
| `graph.backend` | `disabled` | `disabled`, `falkordb`, or experimental external `neptune` |
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
Run `python scripts/check-helm.py` from the repository root for profile rendering,
schema rejection, security, release isolation, and storage contract checks.

Licensed under Apache-2.0; [LICENSE](LICENSE) is included in packaged charts.
