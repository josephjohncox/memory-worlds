# Local qualification procedures

[Documentation](../README.md) · [Guide](../qualification.md)

Qualification uses synthetic inputs and local emulators. It makes no live Okta,
Anthropic, OpenAI, or AWS calls. External provider behavior is assumed. Receipts
describe the local boundary actually exercised; scripted labels are not evidence
of a real model's judgment or accuracy.

Run `mise trust`, `mise install --locked`, and `mise run deps` first. Docker must
already be available. The scripts write new receipts exclusively with mode 0600;
choose a new output filename for each run.

## Sharing controls

```sh
mise run qualify:sharing -- --mode offline --output /tmp/sharing-guards.json
mise run qualify:sharing -- --mode emulated --emulator labeled --output /tmp/sharing-local.json
```

The 31-case labeled corpus covers procedures, decisions, technical observations,
team discussions and preferences, explicit user consent, source attribution,
HR, compensation, rewards, person and project sentiment, credentials, private
markers, excluded topics, multilingual and indirect sentiment, prompt injection,
and a sensitive user message followed by an innocuous assistant reply. Credential
fixtures are generated strings, never real keys.

Emulated mode registers a local provider through Pi's actual `ModelRuntime` and
`ModelRegistry`, and exercises the native streaming classifier and validators.
It uses an in-memory credential store and model catalog, never the user's Pi
credentials. Sensitive windows and invalid consent are withheld before dispatch.
Its report includes dataset and excerpt digests, call counts, classification
errors, unsafe automatic sharing, and missed shareable cases. Confidence scores
are model assertions, not verification.

The evaluator also has deliberate failure controls:

```sh
mise run qualify:sharing -- --mode emulated --emulator unsafe --output /tmp/sharing-unsafe.json
mise run qualify:sharing -- --mode emulated --emulator invalid-span --limit 2 --output /tmp/sharing-invalid.json
```

Both commands must exit unsuccessfully. The first supplies plausible but unsafe
scripted decisions; the second fabricates excerpts that are absent from the
source. This demonstrates that the evaluator detects failures rather than merely
repeating a successful fixture. Review corpus changes with their labels.

## Isolated kind cluster and OIDC

```sh
mise run qualify:cluster -- --state-dir /tmp/memory-worlds-local-qualification
```

The new private directory contains `state.json`, its own kubeconfig, the pinned
Cilium chart, and a private setup log. The script creates one uniquely named
cluster, disables kind's default CNI, installs Cilium 1.20.3, and waits for ready
nodes. It never changes the default kubeconfig or host kernel settings. If startup
fails with an inotify error, inspect the local setup log and the
[kind resource-limit guidance](https://kind.sigs.k8s.io/docs/user/known-issues/#pod-errors-due-to-too-many-open-files).

Build the current API image using the [deployment instructions](../deployments.md), then load that
image and the pinned Keycloak image. Read the cluster name, context and kubeconfig
from the new `state.json`; substitute their exact values below:

```sh
docker pull quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244
mise exec kind@0.33.0 -- kind load docker-image --name CLUSTER_NAME memory-worlds:local quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244
mise run qualify:oidc -- --kubeconfig KUBECONFIG --context CONTEXT --api-image memory-worlds:local --output /tmp/oidc-local.json
mise run qualify:network -- --kubeconfig KUBECONFIG --context CONTEXT --api-image memory-worlds:local --output /tmp/network-local.json
```

OIDC qualification starts a real Keycloak 26.8.1 container with generated users,
passwords, clients and group mappers in a disposable namespace. Keycloak and the
API share Pod loopback; host port forwarding preserves the exact issuer URL.
This exercises normal loopback URL validation without weakening production TLS
rules. The development IdP is not a deployment profile for a real tenant.

The checks cover discovery and JWKS, native CLI device login, private token-file
permissions, company and team group grants, private world and scope isolation,
rejected audience/client/scope claims, membership removal on a fresh token,
rejection of older group claims, descendant grant attenuation, service credential
revocation, and service user disable. Ordinary-user discovery also runs the
actual CLI with that device-flow access token, resolves visible world aliases,
excludes private/sibling metadata, checks the delegated catalog ceiling, and
rechecks discovery after group removal. Group removal cannot invalidate an
unexpired JWT until the service observes new claims or an operator revokes it;
that protocol limit also applies to real identity providers.

Network qualification first proves all synthetic clients can reach a healthy
server. It then applies real Kubernetes NetworkPolicies and requires repeated
successful selected ingress, failed same-namespace and other-namespace ingress,
failed selected egress, and successful explicitly allowed egress. A ready kind
cluster alone does not establish isolation.

Both runners remove only their generated namespaces, including generated
credentials. They preserve the cluster for other deployment checks. Remove it
when finished:

```sh
mise exec kind@0.33.0 -- kind delete cluster --name CLUSTER_NAME --kubeconfig KUBECONFIG
```

## Graphiti extraction and recall

Use a disposable loopback FalkorDB 6.0.0 instance, as described in the deployment
guide. Pass its generated password through a private file, not a command argument:

```sh
mise run qualify:graph -- --falkordb-port 16379 --password-file /path/to/private/password --output /tmp/graph-local.json
```

This runs actual Graphiti extraction and retrieval against a deterministic local
HTTP model and embedding endpoint. Four synthetic facts include contradictory
objects in separate private scopes. Each result must contain the expected fact,
the correct scope, its source episode, and no other scope's contradictory fact.
The runner removes only graph namespaces generated by its fresh authority. It
does not clear the database or fetch an external model. This proves integration,
scope isolation and provenance; it assumes external extraction quality.

## PostgreSQL replicas and lost-volume recovery

Load the pinned PostgreSQL and FalkorDB images into the same isolated cluster,
alongside the current API image:

```sh
docker pull postgres:18.6-bookworm@sha256:ed9475dab2438f7a8ee063fa29a0740e7fbf7185e5549db1866eb722f12b49f8
docker pull falkordb/falkordb-server:6.0.0@sha256:09928c7f8335147f13eec1d686e10a5a9a5a53f2e6df520f2847f94cb7e14925
mise exec kind@0.33.0 -- kind load docker-image --name CLUSTER_NAME \
  postgres:18.6-bookworm@sha256:ed9475dab2438f7a8ee063fa29a0740e7fbf7185e5549db1866eb722f12b49f8 \
  falkordb/falkordb-server:6.0.0@sha256:09928c7f8335147f13eec1d686e10a5a9a5a53f2e6df520f2847f94cb7e14925
mise run qualify:deployment -- --kubeconfig KUBECONFIG --context CONTEXT --api-image memory-worlds:local --output /tmp/deployment-local.json
```

This test creates its own namespace and installs the actual Helm chart with two
API replicas, PostgreSQL, FalkorDB, backup storage, authenticated metrics, and
network policies. Its model and embedding service runs inside the namespace and
serves only deterministic synthetic fixtures. It never enables external HTTPS
egress. A separate maintenance Pod does not match the API replica selector.

The runner checks the following recovery sequence:

1. Write through both API replicas and read the shared source journals. Revoke a
   credential through one replica and reject it through the other. Require an
   administrator for metrics.
2. Extract two synthetic graph facts and retrieve their source provenance.
   Replace an API Pod and retrieve the same durable sources and graph.
3. Erase one fact, stop and drain all API workers, and purge its graph namespace.
4. Restart the replicas, request and download an encrypted native authority
   backup through the API, and execute a Job from the chart's suspended backup
   CronJob template. This proves the stored job template immediately; it does
   not wait for a wall-clock scheduled run.
5. Stop the replicas and databases. Delete only this namespace's PostgreSQL and
   FalkorDB PVCs, recreate them, and verify that their Kubernetes UIDs changed.
6. Restore the encrypted archive into empty PostgreSQL and graph backends, then
   restart both API replicas. Retrieve the kept fact with its original source
   provenance and require no additional model extraction calls. Reject the
   revoked credential, hide the erased record, and reject replay of its source ID.

The receipt records actual container image IDs, archive digest and size, restored
graph count, model operation counts, and each assertion. Receipts contain no
tokens, passwords, encryption keys, runtime configuration, or private file paths.
The runner removes its namespace after completion; the cluster remains available.

This is a single-node recovery proof. It uses RWO backup claims shared on that
node. A production PostgreSQL deployment with replicas on multiple nodes needs
RWX artifact storage and its own infrastructure availability configuration.

See [recorded validation](../validation.md) for executed results.

Official implementation references:
[Keycloak containers](https://www.keycloak.org/server/containers),
[Keycloak device authorization](https://www.keycloak.org/docs/latest/server_admin/index.html#_device-authorization-grant),
[Cilium on kind](https://docs.cilium.io/en/stable/installation/kind/).
