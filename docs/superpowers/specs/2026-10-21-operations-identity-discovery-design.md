# Operations, identity, and discovery

Implementation requested by the owner on 2026-10-21 UTC.

Graphiti is the normal extraction and recall layer. Captures, publications,
imports, and restored source queue extraction by default; recall defaults to
Graphiti. FalkorDB is the default backend. Normal daemon startup requires explicit
model/provider configuration and never selects a paid provider silently. Explicit
source retrieval and maintenance configuration remain available for recovery.
An unavailable graph does not discard durable source or silently downgrade a
graph query to source search.

RDS PostgreSQL is not a supported Graphiti backend. Its vector extension alone
cannot implement Graphiti's graph driver. Deployment guidance therefore uses
FalkorDB on a StatefulSet/PVC for Kubernetes/EKS, documents actual RDS extension
preflight, and identifies PostgreSQL source/control storage as unimplemented.
It does not provide a misleading PostgreSQL connection setting.

Worlds and scopes retain canonical IDs and gain optional lowercase aliases.
Aliases are labels, not authority. CLI `@alias` references resolve only among
objects accessible to the current credential; multiple visible matches require
an ID. Duplicate labels do not leak hidden private scopes through uniqueness
errors. API requests, context bindings, graph namespaces, publication links, and
receipts continue to use canonical IDs. World aliases are operator managed;
scope aliases require the current manage grant. Discovery lists world names,
IDs, aliases, readable scopes, mounts, and mounted world IDs in tables or JSON.

OIDC retains issuer/JWKS/audience/signature/expiry verification and live local
authorization. Optional client-ID and OAuth-scope gates restrict applications.
Native device login validates provider endpoints, follows bounded RFC 8628
polling, verifies the access token against this service, and saves it only in a
new private file. Okta examples use a custom authorization server, API access-token
groups, and mappings to existing shared-world IDs. Neither IdP groups nor aliases
grant access to invocation-private scopes. Local signature checks do not claim
instant external token revocation.

Mise owns the pinned toolchain and developer/CI task entry points. uv and npm
remain the underlying reproducible package installers. Task definitions cover
dependencies, Python/Pi checks, graph services, Helm/kind, CLI, and artifacts.
The lockfile and task definitions ship with the source distribution. No older
schema, command alias, or historical compatibility harness is introduced.

Validation includes alias ambiguity and authorization, current graph defaults,
source durability during graph failures, OIDC client/scope rejection, Okta group
sharing/private isolation, native device-flow error paths and secret handling,
real mise execution, current Pi RPC, deployment contracts, and artifact audits.
