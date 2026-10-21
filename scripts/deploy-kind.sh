#!/usr/bin/env bash
# Creates an isolated cluster. Never changes the caller's current kube-context.
set -euo pipefail
umask 077
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
kind_bin=${KIND:-kind}
kubectl_bin=${KUBECTL:-kubectl}
smoke=false
helm_smoke=false
delete=false
runtime_env=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --smoke) smoke=true; shift ;;
    --helm-smoke) helm_smoke=true; shift ;;
    --delete) delete=true; shift ;;
    --runtime-env)
      if [ "$#" -lt 2 ]; then echo '--runtime-env requires a file' >&2; exit 2; fi
      runtime_env=$2
      shift 2
      ;;
    *) echo "Usage: $0 [--runtime-env FILE] [--smoke] [--helm-smoke] [--delete]" >&2; exit 2 ;;
  esac
done
if [ -n "$runtime_env" ] && [ ! -f "$runtime_env" ]; then
  echo 'The runtime env file must be an existing regular file' >&2
  exit 2
fi
if [ -z "$runtime_env" ] && [ "$smoke" = false ] && [ "$helm_smoke" = false ]; then
  echo 'Supply --runtime-env FILE with the required Graphiti model/provider configuration' >&2
  exit 2
fi
state=${MEMORY_WORLDS_KIND_STATE:-$(mktemp -d "${TMPDIR:-/tmp}/memory-worlds-kind.XXXXXXXX")}
mkdir -p "$state"
state=$(cd "$state" && pwd)
case "$state/" in "$root/"*) echo 'Keep cluster credentials outside the repository' >&2; exit 2 ;; esac
if [ -e "$state/kubeconfig" ]; then
  echo 'State directory already contains a cluster; choose a new directory' >&2
  exit 2
fi
chmod 700 "$state"
if [ -z "$runtime_env" ]; then
  # Persistence checks submit project=false records and never request extraction.
  # The daemon still runs the normal configured Graphiti backend.
  runtime_env="$state/synthetic-runtime.env"
  cat > "$runtime_env" <<'ENV'
MEMORY_WORLDS_LLM_PROVIDER=openai-compatible
MEMORY_WORLDS_LLM_MODEL=synthetic-no-model-calls
MEMORY_WORLDS_LLM_BASE_URL=http://127.0.0.1:9/v1
MEMORY_WORLDS_EMBEDDING_PROVIDER=openai-compatible
MEMORY_WORLDS_EMBEDDING_MODEL=synthetic-no-model-calls
MEMORY_WORLDS_EMBEDDING_BASE_URL=http://127.0.0.1:9/v1
MEMORY_WORLDS_EMBEDDING_DIMENSIONS=1536
ENV
  chmod 600 "$runtime_env"
fi
cluster="memory-worlds-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
kubeconfig="$state/kubeconfig"
context="kind-$cluster"
created=false
cleanup() {
  status=$?
  if [ "$status" -ne 0 ] && [ "$created" = true ]; then
    "$kind_bin" export logs --name "$cluster" "$state/cluster-logs" >/dev/null 2>&1 || true
  fi
  if [ "$delete" = true ] && [ "$created" = true ]; then
    "$kind_bin" delete cluster --name "$cluster" --kubeconfig "$kubeconfig"
  fi
}
trap cleanup EXIT
printf 'Deployment state: %s\n' "$state"
"$kind_bin" version
"$kubectl_bin" version --client
image="memory-worlds:kind-${cluster#memory-worlds-}"
docker build -f "$root/deploy/Dockerfile" -t "$image" "$root"
created=true
"$kind_bin" create cluster --name "$cluster" --config "$root/deploy/kind/cluster.yaml" \
  --kubeconfig "$kubeconfig" --wait 180s --retain
"$kind_bin" load docker-image --name "$cluster" "$image"
graph_image='falkordb/falkordb-server:6.0.0@sha256:09928c7f8335147f13eec1d686e10a5a9a5a53f2e6df520f2847f94cb7e14925'
docker pull "$graph_image"
"$kind_bin" load docker-image --name "$cluster" "$graph_image"
kube=("$kubectl_bin" --kubeconfig "$kubeconfig" --context "$context")
"${kube[@]}" apply -f "$root/deploy/k8s/base/namespace.yaml"
python3 -c 'import secrets,sys; open(sys.argv[1], "w").write(secrets.token_hex(32))' "$state/falkordb-password"
"${kube[@]}" -n memory-worlds create secret generic memory-worlds-falkordb \
  --from-file="password=$state/falkordb-password"
"${kube[@]}" -n memory-worlds create secret generic memory-worlds-runtime \
  --from-env-file="$runtime_env"
"$kubectl_bin" kustomize "$root/deploy/k8s/base" > "$state/manifests.yaml"
python3 - "$state/manifests.yaml" "$image" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace('image: memory-worlds:local', 'image: ' + sys.argv[2]))
PY
"${kube[@]}" apply -f "$state/manifests.yaml"
"${kube[@]}" -n memory-worlds rollout status statefulset/falkordb --timeout=240s
"${kube[@]}" -n memory-worlds rollout status statefulset/memory-worlds --timeout=240s
if [ "$smoke" = true ]; then
  KUBECTL="$kubectl_bin" python3 "$root/scripts/kind-smoke.py" \
    --kubeconfig "$kubeconfig" --context "$context" --state-dir "$state"
fi
if [ "$helm_smoke" = true ]; then
  KUBECTL="$kubectl_bin" python3 "$root/scripts/helm-smoke.py" \
    --kubeconfig "$kubeconfig" --context "$context" --state-dir "$state" --image "$image"
fi
printf 'Cluster: %s\nKubeconfig: %s\n' "$cluster" "$kubeconfig"
printf 'Use explicit --kubeconfig and --context for every kubectl operation.\n'
