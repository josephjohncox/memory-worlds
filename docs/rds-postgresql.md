# PostgreSQL and Amazon RDS

RDS PostgreSQL can host Memory Worlds' shared control, scoped source and outbox
journals. Graphiti continues to use FalkorDB for extraction and graph retrieval.
This gives an AWS deployment managed SQL storage without requiring Neptune or
OpenSearch Serverless.

Use this guide to prepare the database and connection Secrets, then return to
the [Helm PostgreSQL profile](helm.md#postgresql-and-rds). The chart provisions
neither the database nor AWS resources.

## Database setup

Provision a PostgreSQL instance and dedicated database in a private network.
Choose a supported stable engine/minor available in your RDS region. Check the
[PostgreSQL release policy](https://www.postgresql.org/support/versioning/) and
[RDS engine release notes](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-versions.html).

Give the application a dedicated login role that owns its database and can
create its authority and scope schemas, tables, indexes and sequences. The
application does not need a superuser role or an extension installation grant.
Do not grant agents direct SQL access. Restrict the database security group to
the API workloads and operator recovery access.

No PostgreSQL extension is required. `pgvector` does not provide Graphiti's
property graph contract, and installing it does not change the configured graph
backend. AWS publishes extension versions by engine release in its
[extension matrix](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html).
The configured Graphiti driver needs property-graph operations, traversal,
full-text and vector search; [storage](storage.md) explains the SQL/graph boundary.

## Configure the connection

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

For Helm, put the URL alone in `/private/operator/database-url` and the current
CA bundle in `/private/operator/postgres-ca.pem`, then create existing Secrets:

```sh
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-postgres --from-file=database-url=/private/operator/database-url
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-postgres-ca --from-file=ca.pem=/private/operator/postgres-ca.pem
```

The [Helm PostgreSQL values](helm.md#postgresql-and-rds) mount the CA at
`/secrets/postgres/ca.pem` and set `PGSSLROOTCERT`; a URL `sslrootcert` parameter
takes precedence. Helm's storage selector, authority ID, graph and backup
selections take precedence over matching runtime Secret entries.

Keep the existing runtime Secret for explicit model/provider and OIDC settings,
and retain the FalkorDB password Secret and graph volume. The SQL profile needs
no shared filesystem or SQLite PVC. Staged/downloaded backup artifacts need
private temporary space and separate durable artifact storage; see
[backup settings](reference/helm-values.md#encrypted-authority-backups).

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
Follow [authority recovery](recovery.md) for the coordinated restore procedure.

A running API does not silently switch to SQLite when RDS is unavailable.
Database and graph outages remain visible, and durable unknown work stays
conservative. Local integration tests exercise PostgreSQL 18.6 with two API replicas,
authorization, rollback, worker death, lease takeover and stale completion
fences. They establish application storage behavior, rather than a particular
RDS network, credential rotation, regional failover or production workload.
