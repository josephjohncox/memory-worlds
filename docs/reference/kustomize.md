# Kustomize deployment

Use this path when you need overlays for the single-owner SQLite deployment.
For shared PostgreSQL deployments, use [Helm](../helm.md).

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
`/secrets/remotes`. Use those paths in the [router mount configuration](../sharing.md).
The router runs inside the API process and stores
its outbox on the same retained volume. A separate router replica is unnecessary.
Mounted remote credentials must be scoped on their destination authorities.

See [OIDC and Okta](../oidc-okta.md) for identity setup. OIDC requires
`MEMORY_WORLDS_OIDC_ISSUER`, `MEMORY_WORLDS_OIDC_AUDIENCE`, and
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

Continue with [recovery](../recovery.md) before admitting durable client data.
For local lifecycle checks, use the [kind guide](../deployments.md#local-kubernetes-with-kind).
