# Development with mise

Install [mise](https://mise.jdx.dev/installing-mise.html) 2026.10.17 or newer, then
review and trust this checkout before installing its locked tools:

```sh
mise trust
mise install --locked
mise run deps
mise tasks
```

`mise.toml` pins Python 3.15.0, the supported Python 3.14.8 test runtime, uv
0.13.4, and Node 26.12.0. Helm 4.3.1, kind 0.33.0, and kubectl 1.37.1 are
installed when a task needs them. `mise.lock` records exact downloads and
checksums for Linux x64, Linux ARM64, and macOS ARM64. Normal tool installation
fails if the selected platform or version is absent from the lockfile.

uv and npm install dependencies from `uv.lock` and
`packages/pi/package-lock.json`; mise owns tool selection and the public task
interface. Python tasks use this checkout's `.venv`, including when the caller
has an unrelated `UV_PROJECT_ENVIRONMENT` set. Automatic uv Python downloads are
disabled so the interpreter comes from the mise toolchain.

The optional, ignored `.env` holds local provider and database configuration:

```sh
cp .env.example .env
chmod 600 .env
```

Fill the model/provider settings and private graph password before starting a
daemon or Compose stack. mise parses this file as dotenv and marks its loaded
values for task-output redaction. It skips a missing `.env`, so CI and checks do
not require a local secret file. No shell script is sourced. Toolchain settings
remain defined by the checked-in configuration.

## Common tasks

| Command | Behavior |
| --- | --- |
| `mise run deps` | Install locked Python and native Pi dependencies |
| `mise run check` | Python lint, formatting, tests, dependency checks, Pi type checks/tests, and Helm validation |
| `mise run ci` | Run `check`, then the actual Pi runtime, scoped service, and sharing smokes |
| `mise run test` | Run current Python tests |
| `mise run test -- tests/test_scopes.py` | Pass additional arguments directly to pytest |
| `mise run format` | Apply the locked Ruff formatter |
| `mise run pi:verify` | Check and test the native Pi package |
| `mise run pi:smoke` | Run real Pi runtime, scoped service, and classifier-driven sharing smokes |
| `mise run cli -- --help` | Run the current Memory Worlds CLI |
| `mise run setup` | Launch guided local/hybrid configuration and provisioning |
| `mise run setup:smoke` | Verify an isolated actual local setup with Docker and model emulators |
| `mise run graph:backend-up` | Start FalkorDB for a daemon running directly on the host |
| `mise run graph:up` | Build and start the configured API and graph Compose stack |
| `mise run graph:down` | Stop the stack and retain its named volumes |
| `mise run helm:check` | Validate deployment profiles and reject unsafe chart settings |
| `mise run kind:smoke` | Create an isolated cluster, verify persistence and isolation, and delete that cluster |
| `mise run artifacts:build -- --require-clean` | Build and audit the wheel, source archive, Pi package, and chart |

The graph stack requires explicit provider/model settings and a graph password;
see [deployment configuration](deployments.md). No task silently substitutes a
different memory backend. `test:graph` requires a reachable FalkorDB and uses
the actual Graphiti driver with deterministic model responses. `test:container`
requires Docker and builds the deployment under test. The normal test run reports
these external checks as skipped unless explicitly enabled.

The [setup controller](setup.md) also works from an installed wheel. Its shell
entrypoint and mise task delegate to the same Python implementation; setup
configuration and credentials stay outside the source checkout.

The kind lifecycle smoke supplies deliberately synthetic provider settings and
captures source records with `project=false`. It checks durable source, native
graph storage, authentication, and scoped access without contacting a model.
Graphiti extraction and retrieval are verified separately by `test:graph`.
The smoke owns a fresh cluster and keeps its credentials outside the repository.
`kind:deploy` retains its isolated cluster and requires a real runtime env file:

```sh
mise run kind:deploy -- --runtime-env /private/path/memory-worlds-runtime.env
mise run kubectl -- --kubeconfig /private/path/kubeconfig --context kind-memory-worlds-EXAMPLE get pods
mise run helm -- --kubeconfig /private/path/kubeconfig --kube-context kind-memory-worlds-EXAMPLE list
```

Select the recorded cluster and kubeconfig explicitly. The wrappers forward
arguments to the pinned tool. They do not change the caller's current context.

## Tool updates and CI

Update exact versions in `mise.toml` and task declarations, then generate the
lockfile with the current mise CLI:

```sh
mise lock --platform linux-x64,linux-arm64,macos-arm64
mise install --locked --include-task-tools
mise run check
```

Review the changed URLs and checksums with the source change. Keep `uv.lock` and
the npm lockfile synchronized when updating library dependencies. mise does not
replace either library dependency lock.

GitHub Actions installs mise 2026.10.17 through the immutable mise-action 5.2.1
commit and invokes these same targets. Python jobs select their exact matrix
runtime through `MEMORY_WORLDS_PYTHON`; the default is 3.15.0. To exercise the
other supported interpreter locally:

```sh
MEMORY_WORLDS_PYTHON=3.14.8 mise run python:check
```

Each matrix job has its own checkout and `.venv`. Run interpreter changes
sequentially when using one local checkout. CI uses fresh tool installations
from the lockfile rather than trusting a restored installed-tool cache.
