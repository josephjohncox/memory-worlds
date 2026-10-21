#!/usr/bin/env bash
# Downloads verified, pinned upstream binaries into an isolated tools directory.
set -euo pipefail
umask 077
destination=${1:?Usage: install-deploy-tools.sh /outside/repository/tools-directory}
mkdir -p "$destination"
destination=$(cd "$destination" && pwd)
case "$(uname -s)" in Linux) platform=linux ;; Darwin) platform=darwin ;; *) exit 2 ;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=amd64 ;; *) exit 2 ;; esac
fetch() {
  name=$1
  url=$2
  checksum_url=$3
  curl --fail --location --retry 3 "$url" --output "$destination/$name.download"
  curl --fail --location --retry 3 "$checksum_url" --output "$destination/$name.sha256"
  python3 - "$destination/$name.download" "$destination/$name.sha256" <<'PY'
import hashlib, pathlib, sys
artifact, checksum = map(pathlib.Path, sys.argv[1:])
expected = checksum.read_text().split()[0].lower()
actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit('Upstream binary checksum mismatch')
PY
  chmod 755 "$destination/$name.download"
  mv "$destination/$name.download" "$destination/$name"
}
fetch kind "https://kind.sigs.k8s.io/dl/v0.33.0/kind-$platform-$arch" \
  "https://kind.sigs.k8s.io/dl/v0.33.0/kind-$platform-$arch.sha256sum"
fetch kubectl "https://dl.k8s.io/release/v1.37.1/bin/$platform/$arch/kubectl" \
  "https://dl.k8s.io/release/v1.37.1/bin/$platform/$arch/kubectl.sha256"
"$destination/kind" version
"$destination/kubectl" version --client
