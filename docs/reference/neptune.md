# Experimental Neptune backend

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

## Helm configuration

Select `graph.backend=neptune`, disable `falkordb.enabled`, and reference the
runtime Secret with `runtime.existingSecret`. The chart rejects a Neptune profile
that also deploys FalkorDB. Supply private Neptune egress on TCP 8182 and HTTPS
access for OpenSearch/model endpoints. `serviceAccount.annotations` supports
existing workload identity integration; the chart grants no RBAC and keeps
Kubernetes API token automount disabled. Check the identity webhook's projected
token requirements on the target cluster.

For managed relational durability, use the [PostgreSQL/RDS profile](../rds-postgresql.md).
See the [Helm installation guide](../helm.md) for deployment and the
[configuration reference](helm-values.md) for network settings.

## Upstream references

- [Graphiti Neptune and OpenSearch requirements](https://help.getzep.com/graphiti/configuration/neptune-configuration)
- [Neptune IAM data-access actions](https://docs.aws.amazon.com/neptune/latest/userguide/iam-dp-actions.html)
- [OpenSearch Serverless data-access policy requirements](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-data-access.html)
