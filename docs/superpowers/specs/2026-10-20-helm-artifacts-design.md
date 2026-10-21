# Helm and private build artifacts

This extends the approved Memory Worlds deployment design under the user's
request for Helm packaging, a private personal GitHub repository, and artifact
builds. The source and native Pi packages retain Apache-2.0 licensing and the
neutral Memory Worlds name.

## Deployment contract

Use one application Helm chart around the existing API and optional FalkorDB
StatefulSets. Each owns a separate retained PVC. Values validation rejects
multiple API writers, shared API/graph claims, missing secret references,
ambiguous backends, and ingress without explicit TLS. Source-only mode needs
no model service. FalkorDB projection and the experimental external Neptune
adapter require explicit provider configuration.

Operators create credentials separately. Chart values and Helm history contain
Secret references, never generated administrator credentials, graph passwords,
model keys, or router tokens. Initialization remains an authenticated operator
operation through the installed CLI. Helm tests check readiness and reject
unauthenticated API access without changing source data.

Services, workload selectors, and network policies are specific to a release.
Default outbound access is limited to DNS and the configured local graph;
external model, OIDC, and router access requires explicit egress. An enforcing
CNI is necessary to establish network isolation.

## Source and build contract

The repository is private under the user's personal GitHub account. Copy only
allowlisted source/configuration templates into its initial history. Keep local
conversation exports, service data, credentials, caches, and private agent
instructions outside source control and build inputs.

One builder creates the Python wheel and sdist, native Pi npm archive, and Helm
archive with matching package versions. It records the actual full source
commit, clean/dirty state, tracked source digest, tool versions, artifact sizes,
and SHA-256 values. A bounded audit verifies paths, file types, licenses,
metadata, private-input exclusions, and manifest/checksum agreement.

GitHub Actions stores these distributions privately for review on pushes, pull
requests, and manual builds. A separate job publishes a private AMD64/ARM64
container only after the exact commit passes the CI workflow. It validates
workflow identity, repository origin, source SHA, tag/version agreement, and
package visibility. It records the immutable image digest, provenance, and
SBOM. PyPI, npm, public Helm publication, and release creation remain separate
actions.

## Verification

Render source-only, FalkorDB, shared/router, Neptune, and existing-PVC profiles.
Reject unsafe values and verify distinct release names, credential references,
retained storage, and restricted containers. On an isolated kind cluster,
install, test, capture synthetic evidence, replace pods, upgrade, uninstall,
and reinstall with the same retained PVCs. Verify source identity, credentials,
and native graph data survive those operations.

Build all distributions from the real committed checkout, inspect their
contents, then observe hosted CI/artifact jobs and verify repository/package
privacy. NetworkPolicy enforcement, volume-loss recovery, real-model quality,
and live AWS behavior remain outside that smoke's claims.
