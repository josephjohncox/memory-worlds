# Artifact receipts and publication pipeline

[Documentation](../README.md) · [Build and download](../releases.md)

`manifest.json` records the full source commit, whether the checkout was clean, the
tracked working-tree digest, the actual mise and build-tool versions, and each
distribution's size and SHA-256. `SHA256SUMS` covers all six distributions and the
manifest. The manifest is a build receipt; it is not a cryptographic signature. The
builder sets `SOURCE_DATE_EPOCH` from the source commit timestamp. Use the artifact
checksums to identify built bytes; the source commit identifies their code.

The six distributions are listed in [the build guide](../releases.md). Native
OpenCode uses npm's `package/` archive root. The Claude plugin archive uses
`memory-worlds-claude/` and preserves `.claude-plugin/plugin.json` and `.mcp.json`.
Its gzip/tar metadata uses the source timestamp for repeatable packaging.

Python, Pi, OpenCode, Claude plugin manifest, chart version and chart `appVersion`
must match. Release tags must use
`v<version>`, such as `v0.3.0`. Builds fail if these identities differ.

## Source identity and content auditing

The builder requires a Git commit. It never fabricates source identity for an unversioned
directory. `--source-sha <40-character-commit>` also verifies that the checkout is the
expected immutable commit.

The audit rejects unexpected package paths, archive traversal, links and device files,
duplicate entries, excessive file or expanded archive sizes, local databases, exports,
credentials, private inputs and common secret signatures. It also checks package metadata
and license copies. `PUBLIC_CONTENT_DENY_PATTERN` can add an operator-supplied regular
expression for private names without adding those names to the source tree. Content
scanning is evidence about the checked files, rather than proof that every possible
sensitive string has been recognized.

## GitHub Actions

The **Artifacts** workflow builds and uploads the six distributions, manifest and
checksums on pull requests, pushes to `main`, version tags, and manual runs. These uploads
use the repository's Actions access controls and have a 30-day retention period. Download
them from a run's **Artifacts** section, or with GitHub CLI:

```sh
gh run list --repo josephjohncox/memory-worlds --workflow Artifacts
gh run download RUN_ID --repo josephjohncox/memory-worlds --dir /tmp/memory-worlds-download
```

An artifact upload does not mean the full CI suite has passed; the run's source receipt
identifies exactly what was built. The separate **CI** workflow remains the test gate.

After **CI** succeeds for a push to `main` or a version tag in the original repository,
**Artifacts** builds and publishes a private container for `linux/amd64` and `linux/arm64`
to `ghcr.io/josephjohncox/memory-worlds`. It checks out the exact full commit tested by
CI. Pull requests and manual distribution builds cannot publish images. Only the container
job receives `packages: write`; the other jobs receive read access to source.

Container publication requires a private repository. An existing package must have private
visibility before publication, and visibility is checked again afterward. An absent
package is created with GHCR's default private visibility. A visibility lookup error fails
the job. If an administrator later changes repository or package visibility, the next
publication stops until the configuration is reviewed.

Every image receives a `sha-<full-source-commit>` tag. A matching version-tag build also
receives `<version>`. There is no automatically updated `latest` tag. A
`container-receipt.json` artifact records the immutable registry digest, tags, platforms,
tested commit and verified visibility. Tags are registry references and can be reassigned
by authorized operators; use `image@sha256:...` from that receipt for deployments that
must identify exact bytes. BuildKit also attaches provenance and an SBOM to the image.

To pull private images, authenticate with a GitHub credential that has `read:packages` and
access to the package. Configure Kubernetes `imagePullSecrets` when required. Do not put
registry tokens in chart values, manifests or Git history.

The automated pipeline does not publish to PyPI, npm or a public Helm registry.
Actions artifacts expire. Durable private releases retain the signed source tag,
six distributions, manifest, checksums, immutable image receipt and sanitized
local qualification receipts as GitHub Release assets in the private repository.
Create them only after the exact source commit passes CI, verify repository and
container privacy, and verify every asset checksum after downloading it. A
private release is available only to repository readers; it does not change
repository or package visibility. Public publication requires a separate explicit
user decision.
