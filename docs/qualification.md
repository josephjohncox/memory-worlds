# Run local qualification

[Documentation](README.md) · [Recorded validation](validation.md)

Qualification checks integration and isolation with synthetic data, local model
emulators, Keycloak and disposable Kubernetes resources. It does not measure a
production model's judgment or a live Okta/RDS deployment's availability.

Prepare the [locked toolchain](development.md) and Docker. Each output path must
be new; receipts are created as private files. Start with the checks you need:

| Check | What it exercises | Procedure |
| --- | --- | --- |
| Sharing controls | Explicit consent, exclusions, excerpt validation and negative controls | [Sharing](#sharing-controls) |
| OIDC and networking | Device login, groups, discovery and enforced traffic policy | [Kind and OIDC](#isolated-kind-cluster-and-oidc) |
| Graph extraction | Actual Graphiti/FalkorDB transport, scopes and provenance | [Graphiti](#graphiti-extraction-and-recall) |
| Replicas and recovery | PostgreSQL fencing, pod replacement, backup and fresh-volume restore | [Deployment](#postgresql-replicas-and-lost-volume-recovery) |

## Sharing controls

```sh
mise run qualify:sharing -- --mode offline --output /tmp/sharing-guards.json
mise run qualify:sharing -- --mode emulated --emulator labeled --output /tmp/sharing-local.json
```

The corpus includes technical knowledge, consent, sensitive topics, source
attribution and hostile inputs. Deliberately unsafe and fabricated-span controls
must fail. See the [corpus, controls and interpretation](reference/qualification.md#sharing-controls).

## Isolated kind cluster and OIDC

```sh
mise run qualify:cluster -- --state-dir /tmp/memory-worlds-local-qualification
```

The new private state directory records the owned cluster, context and kubeconfig.
Use those exact values for subsequent commands. The runner installs Cilium;
OIDC qualification starts Keycloak with generated identities in a disposable
namespace. It leaves the cluster for other checks and removes only its own
namespaces. Follow the [image loading, qualification and cleanup sequence](reference/qualification.md#isolated-kind-cluster-and-oidc).

## Graphiti extraction and recall

Use a disposable loopback FalkorDB and put its password in a private file:

```sh
mise run qualify:graph -- --falkordb-port 16379 --password-file /private/graph.password --output /tmp/graph-local.json
```

The runner uses local HTTP model emulators and contradictory facts in separate
scopes. It checks retrieval and source provenance, then removes its own namespaces.
See the [Graphiti procedure](reference/qualification.md#graphiti-extraction-and-recall).

## PostgreSQL replicas and lost-volume recovery

The deployment runner installs the actual Helm chart with two API replicas,
PostgreSQL, FalkorDB, backup storage and authenticated metrics. It checks source
and graph persistence, revocation, erased records, native backups and restore
into fresh volumes. Run it only against the owned qualification cluster.

The [complete recovery sequence](reference/qualification.md#postgresql-replicas-and-lost-volume-recovery)
lists images, commands, cleanup and evidence limits. This single-node check does
not qualify a multi-node storage system. Use [validation](validation.md) for
recorded outcomes and image/source bindings.
