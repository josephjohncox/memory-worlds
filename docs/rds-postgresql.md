# PostgreSQL and Amazon RDS

RDS PostgreSQL can host Memory Worlds' shared control, scoped source and outbox
journals. Graphiti continues to use FalkorDB for extraction and graph retrieval.
This gives an AWS deployment managed SQL storage without requiring Neptune or
OpenSearch Serverless.

The current backend uses SQLAlchemy 2.1 and Psycopg 3.3.6. Local integration tests
exercise PostgreSQL 18.6, including two API replicas, current authorization,
transaction rollback, worker death, lease takeover and stale completion fences.
RDS setup is an operator deployment step; the tests do not provision AWS.

## Database setup

Provision a PostgreSQL instance and dedicated database in a private network.
Choose an RDS-supported engine minor available in your region. PostgreSQL 18 is
a stable major; PostgreSQL 19 RC is a preview, not the production selection.
Check the current [PostgreSQL release policy](https://www.postgresql.org/support/versioning/)
and [RDS engine release notes](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-versions.html).

Give the application a dedicated login role that owns its database and can
create its authority and scope schemas, tables, indexes and sequences. The
application does not need a superuser role or an extension installation grant.
Do not grant agents direct SQL access. Restrict the database security group to
the API workloads and operator recovery access.

No PostgreSQL extension is required. `pgvector` does not provide Graphiti's
property graph contract, and installing it does not change the configured graph
backend. AWS publishes extension versions by engine release in its
[extension matrix](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html).
The pinned Graphiti distribution has no PostgreSQL graph driver; its
[driver interface](https://github.com/getzep/graphiti/blob/v0.31.2/graphiti_core/driver/driver.py)
requires episode/entity/edge operations, traversal, full-text and vector search.

## Runtime configuration

Put connection material in a private environment file or an existing Kubernetes
Secret. Use the same explicit authority UUID for all replicas; keep it stable
through rollouts and failover. A different authority ID selects different
schemas and does not discover an existing deployment.

```dotenv
MEMORY_WORLDS_STORAGE_BACKEND=postgres
MEMORY_WORLDS_AUTHORITY_ID=11111111-2222-4333-8444-555555555555
MEMORY_WORLDS_DATABASE_URL=postgresql+psycopg://memory_worlds:URL_ENCODED_PASSWORD@DB_INSTANCE_ENDPOINT:5432/memory_worlds?sslmode=verify-full&sslrootcert=/secrets/postgres/ca.pem
MEMORY_WORLDS_GRAPH_BACKEND=falkordb
# Also configure the explicit FalkorDB endpoint/password, extraction model,
# embedding model, and provider credentials described in the Helm guide.
```

Use the actual RDS endpoint hostname. Mount the current RDS CA bundle at the
referenced path and verify the hostname and certificate with `verify-full`.
The default `prefer` mode does not enforce this verification. See
[AWS PostgreSQL TLS configuration](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/PostgreSQL.Concepts.General.SSL.html).
Use URL encoding for reserved characters in credentials. Do not put a live URL
in checked-in values, command-line arguments or issue descriptions.

The database role creates one authority control schema, one outbox schema and a
source schema per canonical scope UUID. Table references are schema qualified;
changing `search_path` cannot change the selected audience. Additional schema
objects grow with the number of scopes, so monitor catalog size and connection
counts alongside source-table and graph volume.

Start with the [Helm deployment guide](helm.md). Select PostgreSQL storage and
an existing runtime Secret containing the values above; use an operator-owned
FalkorDB password Secret and persistent graph volume. The SQL source path does
not require a shared filesystem or a SQLite PVC. The service may still use a
private runtime directory for downloaded/staged backup artifacts; completed
artifacts need separate durable backup storage.

## Replicas, failure and recovery

API replicas must share the SQL authority, graph endpoint, model configuration,
OIDC mappings and mount configuration. Each replica permits at most 30 pooled SQL
connections and 16 separate long-lived operation guards; maintenance can open an
additional connection. Budget the database connection limit across all replicas
and operator tasks. Saturated SQL or graph capacity fails promptly with `503`.
Do not use SQLite WAL on an NFS/RWX PVC
as a substitute for the PostgreSQL backend. Database leases and fencing prevent
live work from being stolen on rollout and reject stale worker completions.
Unknown external graph writes require a fresh generation rebuild; SQL failover
does not prove that a Graphiti request never reached its backend.

Choose RDS availability, storage, connection limits and backup retention for the
operational requirements of the deployment. Multi-AZ availability is a database
property; it does not replicate a single FalkorDB pod. Configure graph recovery
and external model availability independently.

Use encrypted authority backups for a coordinated source/policy/outbox/graph
cut. Native database point-in-time recovery can recover SQL, but independently
restoring an older SQL snapshot and a newer graph snapshot is not a coordinated
authority recovery. Restore into isolated resources, verify scope access,
revocations, pending publications and extraction state, then connect clients.
See [storage and recovery](storage.md).

A running API does not silently switch to SQLite when RDS is unavailable.
Database and graph outages remain visible, and durable unknown work stays
conservative. This repository's local PostgreSQL tests establish the application
storage/fencing behavior; they do not claim that a particular RDS network,
credential rotation, region, failover policy or production workload was tested.
