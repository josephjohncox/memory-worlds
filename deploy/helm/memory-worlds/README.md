# Memory Worlds Helm chart

Installs one API StatefulSet with SQLite and one authenticated FalkorDB
StatefulSet by default, each with a separate retained PVC. Select PostgreSQL
storage for a stateless API Deployment with multiple fenced workers; Graphiti
continues using its own FalkorDB storage. Requires Kubernetes 1.32 or later.
SQLite WAL requires local or block storage. The chart has no external chart
dependencies and provisions no external database.

The default API image is `memory-worlds:local`; load it into kind or select your
registry image and immutable digest. Create the runtime provider/model and
FalkorDB password Secrets before installing:

```sh
mise run helm -- --kube-context YOUR_CONTEXT upgrade --install memory-worlds . \
  --namespace memory-worlds --create-namespace \
  --set runtime.existingSecret=memory-worlds-runtime \
  --set networkPolicy.allowExternalHTTPS=true --wait --timeout 5m
mise run helm -- --kube-context YOUR_CONTEXT -n memory-worlds test memory-worlds
```

No model/provider is chosen implicitly. Missing extraction/embedding settings
stop startup. `graph.backend=disabled` with `falkordb.enabled=false` is explicit
maintenance/recovery mode; it supplies no graph recall.

Initialize an empty authority once with `memory-worlds init` inside an API pod
and redirect its one-time administrative receipt into a private operator file.
No bootstrap hooks run, no Secrets are created by Helm, and credentials never
belong in values. See the full
[installation and operations guide](../../../docs/helm.md).

| Value | Default | Purpose |
| --- | --- | --- |
| `replicaCount` | `1` | SQLite requires one owner; PostgreSQL supports one to 32 API replicas |
| `image.repository`, `image.tag`, `image.digest` | `memory-worlds`, `local`, empty | API image and optional immutable digest |
| `imagePullSecrets` | `[]` | Existing registry Secrets used by workloads |
| `runtime.existingSecret` | empty, required | Explicit provider/model and optional OIDC settings |
| `graph.backend` | `falkordb` | Graphiti backend; experimental `neptune` or maintenance `disabled` |
| `storage.backend` | `sqlite` | SQLite StatefulSet or stateless PostgreSQL Deployment |
| `storage.postgres.existingSecret`, `urlKey` | empty, `database-url` | Existing Secret with the PostgreSQL URL |
| `storage.postgres.authorityId` | empty | Required stable authority UUID shared by PostgreSQL replicas |
| `storage.postgres.caExistingSecret`, `caKey` | empty, `ca.pem` | Optional trust bundle mounted with `PGSSLROOTCERT` |
| `persistence.size`, `storageClass`, `existingClaim` | `10Gi`, default class, empty | SQLite PVC; `existingClaim` uses an operator-owned claim |
| `falkordb.enabled`, `replicaCount` | `true`, `1` | Separate private graph StatefulSet |
| `falkordb.auth.existingSecret`, `passwordKey` | `memory-worlds-falkordb`, `password` | Secret holding exactly 64 hexadecimal characters |
| `falkordb.persistence.*` | same as SQLite defaults | Independent graph storage |
| `router.enabled` | `false` | Enable existing router configuration and token Secrets |
| `router.mountsExistingSecret`, `tokensExistingSecret` | empty | Required router mounts, read-only with mode 0440 |
| `backup.enabled` | `false` | Configure the encrypted authority backup API |
| `backup.existingClaim`, `keyExistingSecret`, `keyKey` | empty, empty, `key` | Artifact PVC and Secret holding a raw 32-byte encryption key |
| `backup.cronJob.enabled` | `false` | Schedule authenticated CLI backup downloads |
| `backup.cronJob.schedule`, `timeZone` | `0 3 * * *`, `Etc/UTC` | Explicit backup schedule |
| `backup.cronJob.outputExistingClaim` | empty | Separate PVC for downloaded encrypted archives |
| `backup.cronJob.adminTokenExistingSecret`, `adminTokenKey` | empty, `token` | Existing live admin credential mounted only in the job |
| `backup.cronJob.graphMode` | `native` | Native graph snapshot or explicit `rebuild` recovery cut |
| `metrics.enabled` | `false` | Permit selected monitoring clients to reach authenticated metrics |
| `metrics.serviceMonitor.enabled`, `existingSecret` | `false`, empty | Optional Prometheus Operator scrape with bearer authorization |
| `metrics.prometheusRule.enabled` | `false` | Optional scrape, readiness, purge, extraction and publication alerts |
| `ingress.enabled` | `false` | Requires explicit class, hosts and covering TLS Secret references |
| `networkPolicy.enabled` | `true` | Release-scoped isolation; requires an enforcing CNI |
| `networkPolicy.allowExternalHTTPS` | `false` | Explicit HTTPS CIDR access for model/OIDC/remote endpoints |
| `networkPolicy.extraAPIEgress` | `[]` | Explicit peers/ports for other private endpoints or local models |
| `networkPolicy.postgresEgress` | `[]` | Explicit SQL peers/ports; required for PostgreSQL with policy enabled |
| `networkPolicy.metricsNamespaceSelector`, `metricsPodSelector` | label `memory-worlds-metrics=true` | Both must match the actual scraper |
| `serviceAccount.annotations` | `{}` | Existing workload identity integration; no RBAC is granted |
| `tests.enabled` | `true` | Read-only readiness/authentication Helm test |

For RDS or in-cluster PostgreSQL, select `storage.backend=postgres`, provide the
URL Secret and authority UUID, and add explicit database egress. No PostgreSQL
extension is required. The chart sets the native storage selector so missing or
empty connection settings fail startup before a SQLite journal can be created.
See [PostgreSQL and RDS](../../../docs/rds-postgresql.md).

Backup API artifacts and scheduled downloads use distinct operator-owned PVCs.
Multiple API replicas require RWX access to the artifact claim. The CronJob
gets a token file and destination claim; it does not receive the encryption key,
SQL URL, source journal or model credentials. It has no overlapping runs or
automatic retries, and logs no archive content. The chart does not delete old
archive files or choose a retention period.

Monitoring requires existing Prometheus Operator CRDs and a live administrative
scrape token in the release namespace. The chart rejects requested monitoring
resources without matching CRDs. Aggregate metrics carry no source, user,
world or scope labels. No anonymous metrics endpoint is enabled.

Workloads run without root, privilege escalation, writable root filesystems,
Linux capabilities or automatically mounted Kubernetes API tokens. Temporary
volumes and resources are bounded. API probes use `/readyz` and `/healthz`.
FalkorDB generates its private configuration inside tmpfs so its password stays
out of argv. Secret key/CA subPath mounts require a rollout after rotation.

Selectors include release and component labels. Default deny applies only to
the release. Trusted API clients and metrics scrapers require both namespace
and pod selectors. Backup/test egress reaches only DNS and the release's API.
Default kind networking does not enforce these policies.

Uninstall retains StatefulSet claims and never deletes existing Secrets or
PVCs. Retention does not back up storage or protect against node loss. Keep one
SQLite owner and never move live SQLite WAL files onto a network filesystem.
Coordinated source/policy/outbox/graph recovery is described in
[storage](../../../docs/storage.md).

Run `mise run helm:check` from the repository root. Helm 4.3.1 is pinned; the
validator renders eight profiles and checks unsafe values, storage, backup
jobs, monitoring, secret mounts, workload security and release isolation.
Rendering does not prove a storage driver or NetworkPolicy enforcement.

Licensed under Apache-2.0; [LICENSE](LICENSE) is included in packaged charts.
