# Deploy with Helm

Use Helm for a Kubernetes installation with retained storage, explicit Secret
references and optional TLS ingress. The chart runs Graphiti with a separate,
authenticated FalkorDB StatefulSet in both storage profiles.

| Source storage | API workload | API data volume | Replicas |
| --- | --- | --- | --- |
| SQLite (default) | StatefulSet | Retained local/block PVC | Exactly one |
| PostgreSQL / RDS | Deployment | Bounded temporary directory | One to 32 fenced workers |

Choose SQLite for a single API owner, or PostgreSQL for several API replicas.
PostgreSQL holds the control, source and publication journals; FalkorDB holds
the graph. Scaling the API does not make its single graph pod highly available.
The chart provisions no external database or cloud resources.

## Before installing

You need Kubernetes 1.32 or later, a compatible storage driver and an API image
available on the cluster's nodes or in a registry. Use the checked-in mise tools
from the repository root, and select the target context on every command.
For a disposable local test, use [kind](deployments.md#local-kubernetes-with-kind).

Keep operator files outside the checkout. Prepare a private copy of
[`runtime.env.example`](../deploy/k8s/runtime.env.example) containing explicit
extraction and embedding providers, models and credentials. No model is selected
implicitly; incomplete provider settings stop startup.

Build a local image with `docker build -f deploy/Dockerfile -t memory-worlds:local .`
and load it into your named kind cluster, or use a registry image's immutable
digest. The default `memory-worlds:local` tag requires the image to be present
on the nodes.

## 1. Create the existing Secrets

The following example uses `memory-worlds` for both release and namespace. Create
the namespace once, omitting that command if it already exists. Generate the
FalkorDB password into a private file so its value stays out of argv:

```sh
umask 077
mkdir -p /private/operator
mise exec -- python -c 'import secrets; print(secrets.token_hex(32), end="")' \
  > /private/operator/falkordb-password
mise run kubectl -- --context YOUR_CONTEXT create namespace memory-worlds
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-falkordb --from-file=password=/private/operator/falkordb-password
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-runtime --from-env-file=/private/operator/runtime.env
```

The password file must contain exactly 64 hexadecimal characters. Use Kubernetes
Secret encryption and access controls appropriate to your cluster. Credentials
belong in existing Secrets, never Helm values or release history.

## 2. Install and test

For the local image and SQLite profile:

```sh
mise run helm -- --kube-context YOUR_CONTEXT upgrade --install memory-worlds \
  deploy/helm/memory-worlds --namespace memory-worlds \
  --set runtime.existingSecret=memory-worlds-runtime \
  --set networkPolicy.allowExternalHTTPS=true --wait --timeout 5m
mise run helm -- --kube-context YOUR_CONTEXT -n memory-worlds test memory-worlds
```

The HTTPS flag permits external model/OIDC/router endpoints on TCP 443. Narrow
its CIDRs for the target environment; use explicit egress rules for in-cluster
models or private endpoints. NetworkPolicy requires an enforcing CNI.

For a registry deployment, keep installation values outside the checkout:

```yaml
image:
  repository: ghcr.io/YOUR_ACCOUNT/memory-worlds
  digest: sha256:REPLACE_WITH_THE_PUBLISHED_64_HEX_DIGEST
imagePullSecrets:
  - name: existing-registry-credential
runtime:
  existingSecret: memory-worlds-runtime
persistence:
  storageClass: YOUR_BLOCK_STORAGE_CLASS
  size: 20Gi
networkPolicy:
  allowExternalHTTPS: true
```

Replace the digest placeholder and install with `--values FILE`. The registry
Secret is used to pull the image; its token does not enter the API environment.

## 3. Initialize the authority

Initialize an empty authority once and redirect its one-time administrative
receipt into a private file:

```sh
umask 077
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds exec memory-worlds-0 -- \
  memory-worlds init > /private/operator/bootstrap.json
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds port-forward \
  --address=127.0.0.1 service/memory-worlds 8787:8787
```

For PostgreSQL, select `deployment/memory-worlds` instead of `memory-worlds-0`.
Other release names may produce different resource names. Initialization is
manual: no hook, init container or log receives the administrative credential.
Repeating initialization fails without issuing another token.

The Helm test checks readiness and rejection of unauthenticated API requests.
It does not initialize data or require a client credential. Issue separate,
revocable scoped credentials for agents; see [scope and delegation rules](scopes.md).

## PostgreSQL and RDS

Prepare a dedicated database/login role, stable authority UUID, URL Secret and
TLS CA Secret using the [PostgreSQL/RDS guide](rds-postgresql.md). Then select:

```yaml
replicaCount: 2
storage:
  backend: postgres
  postgres:
    existingSecret: memory-worlds-postgres
    urlKey: database-url
    authorityId: 11111111-2222-4333-8444-555555555555
    caExistingSecret: memory-worlds-postgres-ca
    caKey: ca.pem
networkPolicy:
  postgresEgress:
    - to:
        - ipBlock: {cidr: 10.20.0.0/24}
      ports: [{protocol: TCP, port: 5432}]
```

Replace the UUID and network. Retain the runtime, image and graph settings from
your installation values. Database egress is required with policy enabled.
The Deployment has no SQLite PVC; missing or empty PostgreSQL connection settings
fail startup before a SQLite journal can be created. `persistence.existingClaim`
is rejected in this profile. No PostgreSQL extension is required.

## Optional configuration

Use the [Helm configuration reference](reference/helm-values.md) for the values
summary, storage classes and claims, workload security, ingress, router mounts,
backup scheduling, authenticated monitoring and egress rules.

### Shared identity, router, and ingress

Declare spaces, role profiles and external groups in the
[organization file](corporate-access.md), then configure [Okta](oidc-okta.md)
or your OIDC provider. Organization policy uses an existing ConfigMap; model
credentials remain in the runtime Secret. Use
the [networking and router settings](reference/helm-values.md#shared-identity-router-and-ingress)
to expose a TLS gateway and mount scoped remote credentials.

### Maintenance workers

Set `maintenance.existingSecret` to an existing Secret containing `maintenance.toml`
and its model-key files. The chart stages a private, read-only copy before API
startup; no maintenance model runs without an explicitly configured profile.
Follow [profile delivery and rotation](reference/helm-values.md#maintenance-worker-profiles).

### Encrypted authority backups

Configure the [artifact PVC, key Secret and optional CronJob](reference/helm-values.md#encrypted-authority-backups).
Keep the key separately, choose retention, and follow the [restore procedure](recovery.md).

### Authenticated metrics

See [monitoring settings](reference/helm-values.md#authenticated-metrics) for a
live administrative scrape credential and optional Prometheus Operator resources.

### Explicit maintenance or recovery mode

`graph.backend=disabled` with `falkordb.enabled=false` deliberately disables graph
recall. See [maintenance settings](reference/helm-values.md#explicit-maintenance-or-recovery-mode).

### Experimental external Neptune

Neptune requires external AWS and OpenSearch infrastructure. See the
[experimental backend reference](reference/neptune.md) before selecting it.

## Upgrade, uninstall, and recovery

Keep the release name, authority identity and installation values stable. Take
an appropriate coordinated backup before upgrading, apply with
`helm upgrade --install ... --values FILE --wait`, then run `helm test`.
Organization ConfigMap changes, environment Secret changes, staged maintenance
configuration/key changes and single-file `subPath` key/CA rotations require an
explicit StatefulSet or Deployment rollout. Coordinate graph password rotation
between API and graph workloads.

StatefulSet claim templates are immutable. Changes to size/class or to generated
versus existing claims may need planned workload replacement and CSI-supported
PVC expansion. An ordinary Helm upgrade does not migrate storage.

Uninstall removes workloads and release-specific networking. Existing Secrets
and PVCs remain; reinstalling the same release can reuse its retained claims.
Inspect that data before reinstalling because it holds the earlier authority
and credentials. Retention does not protect against node or disk loss. Use
[encrypted authority recovery](recovery.md) for a coordinated restore; use
[portable exports](portable-archives.md) to copy selected evidence.

Validate or package the chart from the repository root:

```sh
mise run helm:check
mise run helm -- package deploy/helm/memory-worlds --destination /private/build-output
```

See [lifecycle validation](reference/kind-validation.md) for the scope of the
render and cluster checks.
