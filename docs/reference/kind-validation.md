# Kubernetes lifecycle validation

The `mise run kind:smoke` task creates an isolated kind cluster, runs both
Kustomize and Helm lifecycle checks, and deletes its cluster. It leaves private
receipts outside the repository. It does not change the caller's current
kube-context or host kernel settings. See [deployment commands](../deployments.md).

## What the checks prove

The Kustomize check initializes a synthetic owner, verifies API and database
authentication, captures an episode and idempotent replay, writes a deterministic
native graph fixture, replaces both pods, and checks the original PVCs still
contain source and graph data.

The Helm check adds the read-only Helm test, an upgrade, retained-PVC reuse
through uninstall/reinstall, private invocation isolation and revocation. The
same API volume retains UUID identities, context bindings and delegated
credential state; scope isolation does not require one pod or PVC per agent.

Both checks configure synthetic model endpoints and submit source operations
with extraction disabled. They establish source/native-graph persistence across
pod and workload replacement. They do not establish model extraction quality,
restoration after node/volume loss, or network isolation. Separate Graphiti tests
exercise extraction and retrieval.

## Recorded run

The [kind receipt](../../deploy/kind/validation.json) and
[Helm receipt](../../deploy/kind/helm-validation.json) record the 2026-10-20 local
run with kind 0.33.0, Kubernetes 1.37.0, kubectl 1.37.1 and Python 3.15.0 on Linux
ARM64. They identify the image, source/script hashes and persistence checks.
These are evidence for that run, not a claim about every later checkout.

The operator temporarily raised an exhausted inotify limit from 128 to 1024,
restored 128 afterward and deleted the owned cluster. The deployment script does
not change that limit.

## NetworkPolicy validation

Default kindnet does not enforce NetworkPolicy. The separate local qualification
uses pinned Cilium and checks allowed API traffic, denied clients and denied
graph traffic with positive controls. Repeat those checks for the CNI and
network configuration used by a deployment; see [qualification](../qualification.md).

## Upstream references

- [kind 0.33.0 release and node digests](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0)
- [Kubernetes supported releases](https://kubernetes.io/releases/)
- [NetworkPolicy prerequisites](https://kubernetes.io/docs/tasks/administer-cluster/declare-network-policy/)
