# Helm configuration reference

Use this reference after the [Helm installation guide](../helm.md). Values contain
resource names and deployment settings; put credentials in existing Kubernetes
Secrets. The chart never provisions an external database or cloud resources.

## Values at a glance

| Value | Default | Purpose |
| --- | --- | --- |
| `replicaCount` | `1` | SQLite requires one owner; PostgreSQL supports one to 32 API replicas |
| `image.repository`, `image.tag`, `image.digest` | `memory-worlds`, `local`, empty | API image and optional immutable digest |
| `imagePullSecrets` | `[]` | Existing registry Secrets used by workloads |
| `runtime.existingSecret` | empty, required | Explicit provider/model settings and credentials |
| `organization.existingConfigMap`, `configKey` | empty, `organization.toml` | Optional organization file for OIDC, shared spaces and group-role bindings |
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

## Shared identity, router, and ingress

Use one [organization TOML](organization.md) for OIDC identity, shared spaces,
role profiles and group bindings. Reference an existing ConfigMap:

```yaml
organization:
  existingConfigMap: memory-worlds-organization
  configKey: organization.toml
```

The chart mounts that key read-only at
`/etc/memory-worlds/organization/organization.toml` and sets
`MEMORY_WORLDS_ORGANIZATION_FILE`. The file contains no tokens or client secrets.
Keep model credentials in `runtime.existingSecret`. Changing ConfigMap contents
requires an explicit API rollout. Follow [company/team setup](../corporate-access.md)
for validation and deployment and [Okta setup](../oidc-okta.md) for the provider.

Group policy grants access; [publication](../sharing.md) and
[per-chat sharing consent](../chat-sharing.md) remain separate.

For the router, enable `router.enabled` and reference existing Secrets through
`router.mountsExistingSecret` and `router.tokensExistingSecret`. The first must
contain `mounts.json`; it mounts below `/config`. Remote tokens mount below
`/secrets/remotes`. Reference their absolute paths in `mounts.json`. Both volumes
are read-only with mode 0440, readable by the API's filesystem group. No inline
router endpoints, tokens, passwords, or model keys are accepted as chart values.

All Services are internal. An optional ingress requires an explicit ingress
class, hosts, and TLS Secret references covering every host. Certificate
provisioning and the controller are existing infrastructure. Configure its
namespace and pod selectors explicitly, for example:

```yaml
ingress:
  enabled: true
  className: nginx
  hosts: [memory.example.org]
  tls:
    - secretName: existing-memory-tls
      hosts: [memory.example.org]
networkPolicy:
  clientNamespaceSelector:
    matchLabels:
      memory-worlds-client: "true"
  clientPodSelector:
    matchLabels:
      memory-worlds-client: "true"
```

Both selectors must match the gateway. The chart's default deny policy selects
only its release's pods, preserving unrelated workloads in the namespace.
API-to-FalkorDB and Helm-test-to-API rules also include release selectors.
NetworkPolicy requires an enforcing CNI; default kind networking does not
provide one, so a kind smoke cannot establish network isolation.

Outbound access defaults to DNS and, when selected, the release's FalkorDB.
Set `networkPolicy.allowExternalHTTPS=true` for external model, OIDC, and router
endpoints, and narrow `externalHTTPSCIDRs` to the target environment. Its opt-in
default CIDRs allow all IPv4/IPv6 destinations on TCP 443. Standard policies lack
FQDN matching; use an egress gateway or the target CNI's DNS-aware policy where
required. `extraAPIEgress` accepts explicit peer and port rules for in-cluster
models or private Neptune endpoints.

## Encrypted authority backups

Enable the authenticated backup API with an operator-owned artifact PVC and
encryption-key Secret. Generate the raw 32-byte key outside the repository:

```sh
mise run cli -- authority-keygen /private/operator/authority-backup.key
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-backup-key --from-file=key=/private/operator/authority-backup.key
```

```yaml
backup:
  enabled: true
  existingClaim: memory-worlds-backup-artifacts
  keyExistingSecret: memory-worlds-backup-key
  keyKey: key
```

The API mounts the artifact claim at `/backups`; a single-file, read-only key
mount provides `/secrets/backup/key`. No key or administrator credential is
rendered into Helm values or logs. Provision the claim before installing. It
must be writable by UID/GID 10001, and a multi-replica Deployment requires shared
RWX semantics so a download can reach the artifact created by another replica.
This requirement applies to encrypted artifact files; PostgreSQL source
journals remain in SQL.

Request and download a complete encrypted artifact through the authenticated
service:

```sh
mise run cli -- --url https://memory.example.org \
  --token-file /private/operator/admin-token authority-backup \
  /private/operator/authority.mwauthority --graph-mode native
```

`native` includes the configured FalkorDB namespaces. `rebuild` records a
source/policy/outbox cut for explicit graph rebuilding after restore. The
service chooses the remote artifact path; the CLI chooses only its local
download path. Preserve the encryption key separately from the artifact.
See [lifecycle](../lifecycle.md) for erasure boundaries and [storage](../storage.md)
for coordinated fresh-target recovery.

An optional CronJob runs the same authenticated CLI. Give it a separate
destination PVC and an existing Secret holding a live administrator token:

```yaml
backup:
  enabled: true
  existingClaim: memory-worlds-backup-artifacts
  keyExistingSecret: memory-worlds-backup-key
  cronJob:
    enabled: true
    schedule: "0 3 * * *"
    timeZone: Etc/UTC
    outputExistingClaim: memory-worlds-downloaded-archives
    adminTokenExistingSecret: memory-worlds-backup-admin
    adminTokenKey: token
    graphMode: native
```

The job gets the token as a read-only file and never receives the encryption
key, SQL URL, runtime provider Secrets or source PVC. It writes uniquely named
encrypted downloads with private permissions. Jobs cannot overlap, have a
bounded deadline and do not retry automatically after failure. The release's
NetworkPolicy permits only DNS and its API, with reciprocal API ingress for the
job. The chart does not delete old artifact files or choose your retention
period. Archive completed files and monitor available space; job history limits
only remove Kubernetes job records.

## Authenticated metrics

`/metrics` requires a live administrative bearer credential. Metrics include
bounded request method/status counters and latency, readiness, worker counts,
and aggregate projection, purge, generation and delivery state. Source content,
world IDs, scope IDs, user IDs and credentials are excluded from metric labels.

```yaml
metrics:
  enabled: true
  serviceMonitor:
    enabled: true
    existingSecret: memory-worlds-metrics-admin
    tokenKey: token
    labels: {release: prometheus}
  prometheusRule:
    enabled: true
    labels: {release: prometheus}
networkPolicy:
  metricsNamespaceSelector:
    matchLabels: {kubernetes.io/metadata.name: monitoring}
  metricsPodSelector:
    matchLabels: {app.kubernetes.io/name: prometheus}
```

Install the Prometheus Operator CRDs first; the chart rejects a requested
ServiceMonitor or PrometheusRule without the matching CRD. The token Secret
belongs in the release namespace. Select these resources with your Prometheus
installation's resource labels, and use a dedicated revocable administrator
credential for scraping. Both NetworkPolicy selectors must match the actual
scraper pods. For a different collector, leave `serviceMonitor.enabled=false`
and configure authenticated scraping explicitly.

The optional rules alert on failed scraping, unavailable authority, failed
graph purges, unknown extraction outcomes and blocked publication. Their
selectors bind the release's Service and namespace. Scrape authorization uses
the operator's [Secret-backed authorization field](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.SafeAuthorization).
The backup schedule uses the standard [CronJob timezone field](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/#time-zones).

## Explicit maintenance or recovery mode

Set `graph.backend=disabled` and `falkordb.enabled=false` only for deliberate
source maintenance or recovery. Capture still records extraction intent by
default; pending jobs wait for a configured Graphiti daemon. Use explicit source
queries while the graph is disabled. This mode does not provide graph memory.

## Workload and storage settings

The API uses UID/GID 10001; FalkorDB uses UID/GID 999. Workloads disable root,
privilege escalation, Linux capabilities, writable root filesystems and automatic
Kubernetes API token mounts. Temporary volumes and resources are bounded. API
probes use `/readyz` and `/healthz`. FalkorDB generates its private configuration
in tmpfs so its password stays out of argv.

An empty `storageClass` uses the cluster default; `"-"` explicitly requests no
class. `persistence.existingClaim` and `falkordb.persistence.existingClaim` select
operator-owned claims instead of claim templates. Claims must support the
workload's ownership and filesystem semantics. Never share one data claim between
releases or put SQLite WAL on NFS/RWX storage.

External ConfigMap and Secret contents are not hashed into pod annotations.
Organization policy updates, environment Secret changes and single-file
`subPath` key/CA rotations require a rollout. Coordinate database
password changes between the API and graph workloads.

## Validation

Run `mise run helm:check` from the repository root. The validator renders eight
profiles and rejects unsafe configurations, checking secret mounts, workload
security, retained storage, backup jobs, authenticated monitoring and release
isolation. Render checks do not establish storage-driver behavior or enforcement
by the installed CNI. See [Kubernetes lifecycle validation](kind-validation.md).
