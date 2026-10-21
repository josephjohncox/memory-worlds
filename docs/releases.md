# Builds and private artifacts

The repository builds four versioned distributions together:

| File | Contents |
| --- | --- |
| `memory_worlds-<version>-py3-none-any.whl` | Python service, client and CLI |
| `memory_worlds-<version>.tar.gz` | Python sources, tests, documentation, deployments and native Pi sources |
| `memory-worlds-pi-<version>.tgz` | Compiled native Pi extension and typed client |
| `memory-worlds-<version>.tgz` | Helm chart, schema, templates and Apache-2.0 license |

`manifest.json` records the full source commit, whether the checkout was clean, the tracked working-tree digest, tool versions, and each distribution's size and SHA-256. `SHA256SUMS` covers all four distributions and the manifest. The manifest is a build receipt; it is not a cryptographic signature. Archive timestamps may differ between builds, so matching source commits do not imply byte-identical archives.

Python, Pi, chart version and chart `appVersion` must match. Release tags must use `v<version>`, such as `v0.1.0`. Builds fail if these identities differ.

## Build locally

Use Python 3.15, uv 0.13.4, Node 26.12.0, and Helm 4.3.1. The checked-in npm and uv lockfiles define dependency resolution. The build script records the versions actually used.

From a clean Git checkout:

```sh
python scripts/build-artifacts.py --require-clean
python scripts/audit-artifacts.py dist/artifacts
(cd dist/artifacts && sha256sum --check SHA256SUMS)
```

The output directory must be empty. Select a fresh directory with `--output /tmp/memory-worlds-build`. Use `--uv`, `--npm` and `--helm` to select tool executables. `--skip-npm-install` uses an existing installation from the checked-in npm lockfile. Without `--require-clean`, local development builds are permitted and the receipt records that their commit alone does not identify the working tree.

The builder requires a Git commit. It never fabricates source identity for an unversioned directory. `--source-sha <40-character-commit>` also verifies that the checkout is the expected immutable commit.

The audit rejects unexpected package paths, archive traversal, links and device files, duplicate entries, excessive file or expanded archive sizes, local databases, exports, credentials, private inputs and common secret signatures. It also checks package metadata and license copies. `PUBLIC_CONTENT_DENY_PATTERN` can add an operator-supplied regular expression for private names without adding those names to the source tree. Content scanning is evidence about the checked files, rather than proof that every possible sensitive string has been recognized.

## GitHub Actions

The **Artifacts** workflow builds and uploads the four distributions, manifest and checksums on pull requests, pushes to `main`, version tags, and manual runs. These uploads use the repository's Actions access controls and have a 30-day retention period. Download them from a run's **Artifacts** section, or with GitHub CLI:

```sh
gh run list --repo josephjohncox/memory-worlds --workflow Artifacts
gh run download RUN_ID --repo josephjohncox/memory-worlds --dir /tmp/memory-worlds-download
```

An artifact upload does not mean the full CI suite has passed; the run's source receipt identifies exactly what was built. The separate **CI** workflow remains the test gate.

After **CI** succeeds for a push to `main` or a version tag in the original repository, **Artifacts** builds and publishes a private container for `linux/amd64` and `linux/arm64` to `ghcr.io/josephjohncox/memory-worlds`. It checks out the exact full commit tested by CI. Pull requests and manual distribution builds cannot publish images. Only the container job receives `packages: write`; the other jobs receive read access to source.

Container publication requires a private repository. An existing package must have private visibility before publication, and visibility is checked again afterward. An absent package is created with GHCR's default private visibility. A visibility lookup error fails the job. If an administrator later changes repository or package visibility, the next publication stops until the configuration is reviewed.

Every image receives a `sha-<full-source-commit>` tag. A matching version-tag build also receives `<version>`. There is no automatically updated `latest` tag. A `container-receipt.json` artifact records the immutable registry digest, tags, platforms, tested commit and verified visibility. Tags are registry references and can be reassigned by authorized operators; use `image@sha256:...` from that receipt for deployments that must identify exact bytes. BuildKit also attaches provenance and an SBOM to the image.

To pull private images, authenticate with a GitHub credential that has `read:packages` and access to the package. Configure Kubernetes `imagePullSecrets` when required. Do not put registry tokens in chart values, manifests or Git history.

This pipeline does not publish to PyPI, npm or a public Helm registry. It does not create GitHub releases. Actions artifacts expire; a durable release needs a separate explicit publication decision and retention policy.
