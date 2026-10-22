# Build and download artifacts

[Documentation](README.md)

One build produces six distributions for the service, clients and deployment:

| File | Contents |
| --- | --- |
| `memory_worlds-<version>-py3-none-any.whl` | Python service, client and CLI |
| `memory_worlds-<version>.tar.gz` | Source, tests, docs, deployments and client/plugin source |
| `memory-worlds-pi-<version>.tgz` | Compiled Pi extension and typed client |
| `memory-worlds-opencode-<version>.tgz` | Compiled native OpenCode tools |
| `memory-worlds-claude-<version>.tar.gz` | Claude Code plugin manifest, MCP connection and scoped-memory skill |
| `memory-worlds-<version>.tgz` | Helm chart, templates, schema and license |

The output also includes `manifest.json` and `SHA256SUMS`. The manifest records
the exact source identity, tools and file digests. See the
[receipt and auditing reference](reference/artifacts.md).

## Build locally

From a reviewed, clean Git checkout:

```sh
mise trust
mise install --locked
mise run artifacts:build -- --require-clean
mise run artifacts:audit -- dist/artifacts
```

The output directory must be empty. For a new directory:

```sh
mise run artifacts:build -- --require-clean --output /tmp/memory-worlds-build
```

Verify `SHA256SUMS` with `sha256sum --check SHA256SUMS` on Linux or
`shasum -a 256 --check SHA256SUMS` on macOS, from the artifact directory.
Python, Pi, OpenCode, Claude plugin and chart versions must match; a release tag
uses `v<version>`.
`--source-sha FULL_COMMIT` additionally pins the expected source commit.

The audit checks package contents, metadata, license copies and private-data
signatures. Build receipts and content scans have the limits described in the
[artifact reference](reference/artifacts.md#source-identity-and-content-auditing).

Client packages and exported MCP connections use an existing running service.
Follow [the coding-agent guide](clients.md) to select credentials and install
the package. The Claude archive extracts under `memory-worlds-claude/` with its
hidden manifest and MCP files intact.

## Download from GitHub Actions

The Artifacts workflow uploads distributions and receipts under the repository's
Actions access controls. Downloads expire after 30 days:

```sh
gh run list --repo josephjohncox/memory-worlds --workflow Artifacts
gh run download RUN_ID --repo josephjohncox/memory-worlds --dir /tmp/memory-worlds-download
```

An uploaded distribution identifies built source; the separate CI workflow is
the test gate. Check both run results and the downloaded checksums.

## Use a container image

After CI passes at an eligible commit, the container job builds private
`linux/amd64` and `linux/arm64` images at
`ghcr.io/josephjohncox/memory-worlds:sha-FULL_COMMIT`. Its
`container-receipt.json` records the tested commit and immutable registry digest.
Use `image@sha256:...` from that receipt to pin a deployment.

Private pulls require package access and `read:packages`; Kubernetes may need
`imagePullSecrets`. Keep registry tokens out of Git and chart values. See
[Helm installation](helm.md) and [publication controls](reference/artifacts.md#github-actions).

The source repository is public. The current workflow keeps container packages
private and does not publish to public package registries. Durable release assets
and package publication are separate release decisions; the reference describes
their gates.
