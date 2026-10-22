# Kubernetes deployments

Memory Worlds runs the same authenticated API, scope authorization, router and
durable publication outbox locally and in Kubernetes. Choose a deployment path:

| Goal | Start here |
| --- | --- |
| Shared installation, SQLite or PostgreSQL | [Helm installation](helm.md) |
| Disposable local lifecycle check | [kind commands below](#local-kubernetes-with-kind) |
| Single-owner SQLite with custom overlays | [Kustomize deployment](reference/kustomize.md) |
| Personal daemon connected to a shared service | [Local and corporate memory](hybrid.md) |

SQLite requires one API owner and local/block storage. For several API replicas,
use PostgreSQL for the control, source and outbox journals. Graphiti uses a
separate FalkorDB volume in either profile. API replication does not replicate
the single graph pod; plan SQL, graph and model availability independently.

## Local Kubernetes with kind

Docker and mise are required. Install the locked tools/dependencies, then run the
complete Kustomize and Helm lifecycle smoke:

```sh
mise install --locked
mise run deps
mise run kind:smoke
```

This task creates a randomly named isolated cluster, builds and loads the API
image, loads the pinned FalkorDB image, creates private credentials, runs the
checks and deletes its own cluster. It leaves private receipts outside the
checkout and never changes the current kube-context. Deleting the cluster
destroys its node-local volumes.

The check uses synthetic provider settings and source captures with extraction
disabled. It verifies pod replacement, upgrade and retained-PVC reuse; it does
not call a model. See [validation details and recorded receipts](reference/kind-validation.md)
for exactly what is established.

To retain a local cluster for use, prepare explicit extraction and embedding
settings in a private copy of
[`runtime.env.example`](../deploy/k8s/runtime.env.example), then run:

```sh
mise run kind:deploy -- --runtime-env /private/operator/runtime.env
```

The script prints its private state directory, cluster name and kubeconfig.
`MEMORY_WORLDS_KIND_STATE=/outside/repo/new-directory` selects that location.
Use `--smoke --helm-smoke` to add lifecycle checks, or `--delete` to remove only
the cluster created by that invocation. Keep any generated `bootstrap.json`
private: it is administrative material. Issue scoped client credentials for
ordinary use.

Connect to a retained cluster using its printed values:

```sh
mise run kubectl -- --kubeconfig /private/state/kubeconfig \
  --context kind-memory-worlds-CLUSTER -n memory-worlds \
  port-forward --address=127.0.0.1 service/memory-worlds 8787:8787
```

For remote agents, forward that loopback API with [SSH](remote-access.md).
Never expose a raw database port to agents.

## Kubernetes configuration

The [Helm guide](helm.md) covers images, Secrets, authority initialization and
storage selection. The [configuration reference](reference/helm-values.md)
covers ingress, egress, router mounts, backups, monitoring and workload security.
Use the [Kustomize guide](reference/kustomize.md) when editing the SQLite overlays.

Keep services internal until an existing TLS gateway and certificates are
configured. Trusted clients must match both namespace and pod NetworkPolicy
selectors. A policy-enforcing CNI is required; default kindnet does not enforce
these rules. Run the [network qualification](qualification.md) for the target
CNI and network configuration.

Declare shared spaces and group-role bindings using [organization configuration](corporate-access.md),
then follow [Okta setup](oidc-okta.md) for provider settings. A verified user
receives a private-work context; configured groups or manual memberships grant
shared-world access. Neither authentication nor a group claim grants administration.

## Backups and recovery

Retained PVCs survive workload replacement and uninstall. They do not protect
against node loss. Copy only finalized exports or encrypted backup artifacts to
cloud storage; never synchronize live SQLite/WAL or graph directories.

Use [portable exports](portable-archives.md) to move selected source evidence
without live credentials or grants. Use [authority recovery](recovery.md) to
restore a coordinated cut of policy, private/shared source, outbox and native
graph data into isolated fresh resources. [Storage internals](storage.md) explain
worker fencing and uncertain graph writes; [lifecycle](lifecycle.md) covers
deletions and independent backup retention.

## AWS without Neptune: FalkorDB, and the RDS boundary

Use [RDS PostgreSQL](rds-postgresql.md) for shared SQL journals and FalkorDB for
Graphiti extraction and retrieval. No PostgreSQL extension is required;
`pgvector` does not replace the graph driver. SQLite remains the single-daemon
local/PVC profile.

## AWS Neptune evaluation lane

Neptune is an experimental graph backend with additional OpenSearch, network and
identity requirements. It does not replace the SQL journals. Configuration is
tested offline; live AWS ingestion and recovery remain unverified. See the
[Neptune reference](reference/neptune.md) for setup, implementation limits and
qualification requirements.
