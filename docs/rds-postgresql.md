# PostgreSQL, RDS, and the graph backend

RDS PostgreSQL is not a supported Memory Worlds backend. Do not point
`MEMORY_WORLDS_GRAPH_BACKEND` at PostgreSQL or add a `DATABASE_URL`: the application
has no PostgreSQL graph driver or source/control-store implementation.

For a deployment on AWS that avoids Neptune, use the normal Graphiti/FalkorDB
chart on Kubernetes or EKS, with separate block-backed PVCs for the API journals
and graph data. This uses the tested backend and does not require an OpenSearch
Serverless collection. See [Helm installation](helm.md).

## Why pgvector is insufficient

Graphiti 0.31.2 ships Neo4j, FalkorDB, Neptune, and a deprecated Kuzu driver. It
does not ship a PostgreSQL driver. Its graph interface includes episode/entity
storage, temporal edges, traversal, full-text and vector retrieval, and scoped
group operations. A PostgreSQL vector extension supplies only part of that
contract. See the [upstream driver interface](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/driver/driver.py)
and [supported backend configuration](https://github.com/getzep/graphiti/blob/v0.31.2/README.md).

AWS lists pgvector among supported RDS PostgreSQL extensions, with versions that
vary by PostgreSQL engine release. Its published extension matrix does not list
Apache AGE. Neither the presence of pgvector nor PostgreSQL's support for custom
extensions makes an unimplemented Graphiti driver available. Check the
[AWS extension matrix](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html)
for the exact engine version and region you operate.

A proposed PostgreSQL/PostGraph driver exists in upstream
[PR #1777](https://github.com/getzep/graphiti/pull/1777), but it was closed without
merging. The associated [RFC #1781](https://github.com/getzep/graphiti/issues/1781)
was closed as not planned on September 21, 2026. It is not part of the pinned
Graphiti distribution or this service. Installing that proposal is not an
installation step supported by these deployment instructions.

## Deploy on AWS without Neptune

Create a runtime Secret with explicit extraction and embedding models, and a
separate FalkorDB password Secret as described in [helm.md](helm.md). Select an
existing block StorageClass supplied by your EKS cluster; the example below uses
an operator-created class named `gp3`:

```yaml
# Keep installation values outside the source checkout.
runtime:
  existingSecret: memory-worlds-runtime
persistence:
  storageClass: gp3
  size: 20Gi
falkordb:
  auth:
    existingSecret: memory-worlds-falkordb
  persistence:
    storageClass: gp3
    size: 30Gi
networkPolicy:
  allowExternalHTTPS: true
```

Install using the image digest, ingress, and egress settings for your environment:

```sh
mise run helm -- --kube-context YOUR_CONTEXT upgrade --install memory-worlds \
  deploy/helm/memory-worlds --namespace memory-worlds --create-namespace \
  --values /private/operator/memory-worlds.yaml --wait --timeout 5m
```

Storage sizes are examples, not capacity recommendations. The chart uses one API owner and one graph owner; moving to AWS
does not change its single-instance availability boundary. Keep independent
backups and verify restore procedures.

## Evaluating an existing RDS instance

These read-only queries establish which extensions an existing PostgreSQL
instance offers. They do not configure Memory Worlds to use it:

```sql
SELECT version();
SHOW rds.extensions;
SELECT name, default_version, installed_version
FROM pg_available_extensions
WHERE name IN ('vector', 'age', 'pg_trgm', 'btree_gin');
SELECT extname, extversion FROM pg_extension ORDER BY extname;
```

RDS controls which extensions can be installed. Extension privileges and
allowlists are separate from application permissions; see
[AWS extension administration](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.Extensions.html).
Do not install unrelated extensions to satisfy an assumed graph requirement.

Using RDS as the graph backend would require a supported Graphiti driver covering
its complete current graph/search contract. Moving the SQLite source, control,
and outbox journals to RDS is a separate implementation, including durable worker
ownership. Both changes need real PostgreSQL concurrency and recovery tests;
the graph driver additionally needs ingestion, namespace-isolation, and temporal
retrieval validation before a chart can advertise support. A vector-only retrieval
service would change the product's memory model.
