# Helm deployment

The chart at `deploy/helm/memory-worlds` packages the existing Kubernetes
topology: one Memory Worlds API process owns the SQLite source, control, and
router databases, and one optional FalkorDB process owns graph storage. Both use
retained PVCs. API and graph replicas are fixed at one by the values schema.
This is persistent single-instance storage, with the same availability and
recovery limits described in [deployments.md](deployments.md).

Kubernetes 1.32 or later, a compatible local/block StorageClass, and Helm are
required. Helm 4.3.1 was verified against the
[official release](https://github.com/helm/helm/releases/tag/v4.3.1) on 2026-10-20.
The chart uses the stable StatefulSet PVC retention policy available since
[Kubernetes 1.32](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#persistentvolumeclaim-retention).

## Source-only installation

Use an explicit context on every command. The examples use a release and
namespace both named `memory-worlds`, making the API pod `memory-worlds-0`.
Other release names receive distinct Services, StatefulSets, and policies.

Build the image with `docker build -f deploy/Dockerfile -t memory-worlds:local .`
and load it into your named kind cluster, or select the registry image produced
by the artifact pipeline. A private registry requires an existing pull Secret.
The example local tag is suitable only when the image is already present on
the cluster's nodes.

```sh
helm --kube-context YOUR_CONTEXT upgrade --install memory-worlds \
  deploy/helm/memory-worlds --namespace memory-worlds --create-namespace \
  --set falkordb.enabled=false --wait --timeout 5m
helm --kube-context YOUR_CONTEXT -n memory-worlds test memory-worlds
```

For registry deployment, use an installation values file with an immutable
digest and an existing registry credential reference:

```yaml
image:
  repository: ghcr.io/YOUR_ACCOUNT/memory-worlds
  tag: 0.2.0
  digest: sha256:REPLACE_WITH_THE_PUBLISHED_64_HEX_DIGEST
imagePullSecrets:
  - name: existing-registry-credential
falkordb:
  enabled: false
persistence:
  storageClass: YOUR_BLOCK_STORAGE_CLASS
  size: 20Gi
```

Replace the digest placeholder before installation; the schema rejects it.
The registry credential remains in Kubernetes, outside Helm values and release
history. The API container never receives the registry token as an environment
variable.

Initialize the empty authority once. Its receipt contains a one-time
administrative credential; redirect stdout directly into a private file outside
the repository. Initialization is a manual operation, so it does not become a
Helm hook, pod log, or release value.

```sh
umask 077
mkdir -p /private/operator
kubectl --context YOUR_CONTEXT -n memory-worlds exec memory-worlds-0 -- \
  memory-worlds init > /private/operator/bootstrap.json
kubectl --context YOUR_CONTEXT -n memory-worlds port-forward \
  --address=127.0.0.1 service/memory-worlds 8787:8787
```

Issue separate revocable credentials for agents using the CLI. Re-running
initialization against an initialized authority fails; it does not issue a new
administrator token. The read-only Helm test checks readiness and verifies the
protected API rejects unauthenticated requests. It does not initialize data or
need an agent credential.

## FalkorDB and graph projection

The default chart includes FalkorDB with its pinned 6.0.0 image. Create its
password Secret before installing the default profile. Generate a 64-character
hexadecimal password into a private file and provide that file to kubectl;
the secret value never enters command-line arguments:

```sh
umask 077
python -c 'import secrets; print(secrets.token_hex(32), end="")' \
  > /private/operator/falkordb-password
kubectl --context YOUR_CONTEXT create namespace memory-worlds
kubectl --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-falkordb --from-file=password=/private/operator/falkordb-password
```

Omit namespace creation when it already exists. To enable extraction, copy
`deploy/k8s/runtime.env.example` outside the repository, fill in explicit model
and embedding provider settings, and create the runtime Secret:

```sh
kubectl --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-runtime --from-env-file=/private/operator/runtime.env
helm --kube-context YOUR_CONTEXT upgrade --install memory-worlds \
  deploy/helm/memory-worlds --namespace memory-worlds \
  --set graph.backend=falkordb \
  --set runtime.existingSecret=memory-worlds-runtime \
  --set networkPolicy.allowExternalHTTPS=true --wait --timeout 5m
```

`graph.backend` is authoritative even if the runtime Secret contains
`MEMORY_WORLDS_GRAPH_BACKEND`. Provider/model settings belong in the runtime
Secret. Creating a graph pod alone leaves projection disabled; source capture
and source search remain usable without a model.

The API and graph PVCs have independent storage settings. An empty
`storageClass` selects the cluster default; `"-"` sets an explicitly empty class.
`persistence.existingClaim` and `falkordb.persistence.existingClaim` use
operator-owned claims instead of creating claim templates. Claims must have
compatible filesystem semantics and support the chart's UID/GID ownership:
10001 for API data, 999 for graph data. Never share one data claim between
releases or mount SQLite WAL over NFS/RWX storage.

## Shared identity, router, and ingress

OIDC settings use `runtime.existingSecret`, including issuer, audience, JWKS URL,
and optional group-to-world mappings. World authorization remains independent
of OIDC authentication. See [sharing.md](sharing.md) for the identity and
publication contracts.

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

## Experimental external Neptune

Select `graph.backend=neptune`, disable `falkordb.enabled`, and provide
`runtime.existingSecret` with the Neptune/OpenSearch and provider settings in
`deploy/k8s/neptune.env.example`. The schema rejects a Neptune profile that also
deploys FalkorDB. Supply private Neptune egress on TCP 8182 and HTTPS access for
OpenSearch/model endpoints. `serviceAccount.annotations` permits integration
with existing workload identity; the chart grants no RBAC and keeps standard
Kubernetes API token automount disabled. Validate your identity webhook's
projected token requirements on the target cluster.

This configures an external backend; it provisions no AWS resources. Live AWS
ingestion and recovery remain unverified. Review backend support and isolation
limits in [deployments.md](deployments.md#aws-neptune-evaluation-lane).

## Upgrade, uninstall, and recovery

Use stable release names and retain installation values outside the checkout.
Take authenticated source exports or appropriate quiesced backups before
upgrading. Apply changes using `helm upgrade --install ... --values FILE --wait`,
then run `helm test`. Secret contents are not hashed into pod annotations;
restart the appropriate StatefulSet after changing an environment Secret.
Coordinate database password rotation between API and graph pods.

StatefulSet claim templates are immutable. Changing a size/class or switching
between generated and existing claims can require a planned workload
replacement and CSI-supported PVC expansion; an ordinary Helm upgrade does not
migrate storage. Do not use forced replacement as a substitute for a recovery
plan. Claims remain through pod replacement and uninstall; installing the same
release name reuses the retained claims. Review any existing data before
reinstallation because it contains the previous authority and credentials.

`helm uninstall` removes workloads and release-specific networking. Existing
Secrets and PVCs remain operator-owned. Retention protects against workload
deletion, not node or disk loss. Portable exports exclude active credentials
and grants; restoring the full authority and its graph requires the separate
cross-store procedure described in [deployments.md](deployments.md#backups-and-recovery).

Validate the packaged deployment contract locally:

```sh
python scripts/check-helm.py --helm /path/to/helm
helm lint --strict deploy/helm/memory-worlds
helm package deploy/helm/memory-worlds --destination /private/build-output
```

The validator checks six profiles, rejects unsafe values, and checks security,
secret mounts, retained storage, and distinct release names. Rendering does not
prove storage-driver behavior or NetworkPolicy enforcement.
