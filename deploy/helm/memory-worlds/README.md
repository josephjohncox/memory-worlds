# Memory Worlds Helm chart

Deploys the authenticated memory API with Graphiti and FalkorDB. The default is
one SQLite API StatefulSet and one authenticated FalkorDB StatefulSet, each with
a separate retained PVC. PostgreSQL storage selects a stateless API Deployment
with one to 32 API replicas and fenced background work; FalkorDB remains the graph backend.

Requires Kubernetes 1.32 or later and compatible storage. The chart has no
external chart dependencies and provisions no external database or cloud resources.

## Install

Before installing, create the existing Secrets in the target namespace:

- `memory-worlds-runtime`: explicit extraction/embedding providers, models and
  credentials, plus optional OIDC configuration.
- `memory-worlds-falkordb`: a `password` key containing exactly 64 hexadecimal
  characters.

Use the [installation guide](https://github.com/josephjohncox/memory-worlds/blob/main/docs/helm.md)
for Secret creation and image preparation. The default API image is
`memory-worlds:local`; load it into your cluster or set a registry image with an
immutable digest. From the unpacked chart directory:

```sh
helm --kube-context YOUR_CONTEXT upgrade --install memory-worlds . \
  --namespace memory-worlds --create-namespace \
  --set runtime.existingSecret=memory-worlds-runtime \
  --set networkPolicy.allowExternalHTTPS=true --wait --timeout 5m
helm --kube-context YOUR_CONTEXT -n memory-worlds test memory-worlds
```

The HTTPS flag allows external model/OIDC/router endpoints; narrow its CIDRs for
your environment. A policy-enforcing CNI is required. Default kind networking
does not enforce NetworkPolicy.

Initialize an empty authority once with `memory-worlds init` inside an API pod,
redirecting its one-time administrative receipt into a private operator file.
No bootstrap hook runs and Helm creates no Secrets. Use separate scoped
credentials for ordinary agents. Provider/model settings are required at startup.

## Configure and operate

| Task | Guide |
| --- | --- |
| Values, storage claims, TLS ingress, router, egress | [Configuration reference](https://github.com/josephjohncox/memory-worlds/blob/main/docs/reference/helm-values.md) |
| Explicit summary and enrichment workers | [Maintenance profiles](https://github.com/josephjohncox/memory-worlds/blob/main/docs/reference/helm-values.md#maintenance-worker-profiles) |
| Shared SQL journals and replicas | [PostgreSQL and RDS](https://github.com/josephjohncox/memory-worlds/blob/main/docs/rds-postgresql.md) |
| User identity and team groups | [OIDC and Okta](https://github.com/josephjohncox/memory-worlds/blob/main/docs/oidc-okta.md) |
| Encrypted backups and fresh-target restore | [Authority recovery](https://github.com/josephjohncox/memory-worlds/blob/main/docs/recovery.md) |
| Upgrade, Secret rotation and uninstall | [Operations](https://github.com/josephjohncox/memory-worlds/blob/main/docs/helm.md#upgrade-uninstall-and-recovery) |

Keep exactly one SQLite API owner and never put live SQLite WAL on a network
filesystem. PostgreSQL requires a URL Secret, stable authority UUID and explicit
database egress; no PostgreSQL extension is required. API replication does not
make the single graph pod highly available.

Uninstall retains StatefulSet claims and never deletes operator-owned Secrets
or PVCs. Retention is not a backup and does not protect against node loss.
Secret environment changes, staged maintenance profiles/keys and single-file
key/CA rotations require a rollout. Set `maintenance.existingSecret` only when
you have explicitly configured worker profiles; it is empty by default.

Licensed under Apache-2.0; [LICENSE](LICENSE) is included in packaged charts.
