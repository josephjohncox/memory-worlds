# Setup reference

[Documentation](../README.md) · [Setup guide](../setup.md)

The same Python controller drives the wizard and scripted configuration. It
manages one local SQLite authority, a local FalkorDB container, private
credentials and optional corporate routes. Corporate infrastructure is operated
separately.

## Configuration

Save the TOML outside the checkout in a private directory. This local example
uses placeholder models and endpoints; configure providers you operate and the
actual embedding dimension they return.

```toml
instance = "local"
mode = "local"
owner = "alice"
data_dir = "~/.local/share/memory-worlds/local"
state_dir = "~/.local/state/memory-worlds/local"
port = 8787
graph_port = 16379
embedding_dimensions = 1536
pi_profiles = true
mcp_profiles = false

[extraction]
provider = "openai-compatible"
model = "your-extraction-model"
base_url = "http://127.0.0.1:9000/v1"

[embedding]
provider = "openai-compatible"
model = "your-embedding-model"
base_url = "http://127.0.0.1:9001/v1"
```

For authenticated providers, add `api_key_file` to the relevant section.
`provider = "openai"` requires that reference; `openai-compatible` requires an
explicit `base_url` and allows an unauthenticated endpoint. Secret values are
not inline configuration fields. Relative paths resolve beside the TOML, `~`
expands to your home directory, and no shell evaluates configuration.

`data_dir` and `state_dir` must be separate dedicated directories. API and graph
ports must differ and bind `127.0.0.1`. Setup refuses a remote Docker context.
It does not install system packages, trust source automatically, change your
shell or register a background service.

For hybrid mode, set `mode = "hybrid"` before first apply and add:

```toml
[corporate]
url = "https://memory.example.org"
issuer = "https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID"
client_id = "YOUR_NATIVE_APPLICATION_CLIENT_ID"
scopes = ["openid", "memory-worlds"]
world_ids = ["SELECTED_TEAM_WORLD_ID", "SELECTED_COMPANY_WORLD_ID"]
```

Alternatively, provide `token_file` instead of device-login settings. Use
canonical shared-world IDs from [discovery](../discovery.md). No worlds are
selected by default, and an empty selection is valid. Connect pins the corporate
URL, authority and user; private corporate worlds cannot become mounts.
Device login does not request or store refresh tokens. Reviewed starting files
are available for [local](../../deploy/setup/local.toml) and
[hybrid](../../deploy/setup/hybrid.toml) installations.

## Commands and supported changes

All commands accept `--config FILE`:

| Command | Effect |
| --- | --- |
| `setup` or `setup init` | Collect and review configuration; offer a separate apply step |
| `setup validate` | Check configuration and referenced files without changing state |
| `setup plan` | Show configured resources and planned effects |
| `setup connect` | Authenticate as a corporate user and record selected access |
| `setup apply` | Provision or recover local identities, credentials and profiles while the API is stopped |
| `setup run` | Start the owned graph and serve the API in the foreground |
| `setup doctor` | Inspect ownership, credential bindings and readiness |
| `setup down` | Remove the owned graph containers/network after the API stops; retain data |

Apply provisions an agent-persistent notes scope, router configuration and an
owned graph definition. It does not call a model. Repeat apply preserves recorded
identities and secrets. Modified managed files, revoked credentials and changed
authority bindings are refused; setup does not issue broader replacement access.

A reviewed configuration can change loopback ports, provider settings, profile
output flags and selected corporate shared worlds. Run connect before applying
changed corporate selections. Instance, owner, mode, authority paths and
corporate identity stay pinned. Changing local mode to hybrid requires a new
installation; adopting an existing authority is a separate explicit action.
Disabling profile generation retains previously generated files: review their
consumers and remove unwanted files explicitly.

Run verifies managed hashes and ownership before starting the graph. Its daemon
environment is built from validated settings and referenced secrets. Ambient
database URLs, OIDC configuration and provider keys cannot select another
backend. Ctrl-C stops the API and leaves the graph running. Down refuses an
active API and does not remove unrelated containers or persistent volumes.
Missing provider keys or damaged client profiles do not block down when graph
ownership and its definition still match.

## Adopt an existing authority

Stop the authority's daemon. Point `data_dir` at the existing SQLite authority
and add a top-level reference:

```toml
adopt_admin_file = "~/.config/memory-worlds/existing-admin.token"
```

The wizard's **adopt** option records the same reference. Apply authenticates a
live local administrator credential before provisioning. A corporate user token
gives no local administrative rights. To use another administrator file for one
operation:

```sh
mise run setup -- apply --config "$config" --adopt-admin-file /private/admin.token
```

The override is validated instead of the saved reference for that application;
it does not rewrite the TOML or its digest. Standalone `setup validate` continues
to check the saved reference.

## Wizard recovery

After you approve the review, the wizard saves a private
`<config filename>.wizard.json` intent beside the requested TOML. It records
settings, identity pins and file hashes, never provider keys or access tokens.
Provider files and the TOML are saved before a corporate connection is published.
The intent is removed after completion.

Resume by rerunning the wizard with the same output path:

```sh
python3 scripts/bootstrap.py --config "$config"
```

If a newly entered provider key had not reached its private file, enter that
same key again when asked. Recovery checks its recorded hash. Changed files and
unrelated pre-existing state are refused. A valid committed corporate connection
can finish recovery offline without the original external token file or another
discovery request. An uncommitted connection can still require fresh login.
Do not delete or reset state to force an unrelated installation through recovery.

## Generated files and client profiles

The configured `state_dir` contains the setup receipt/journal, managed token
files, `compose.graph.yaml`, `mounts.json` and enabled client profiles. Setup does
not overwrite global Pi or MCP client configuration.

| Output | Audience and defaults |
| --- | --- |
| `pi-personal.json` | Separate local personal credential and private persistent write target; no corporate mounts |
| `pi-work.json` | Hybrid local work credential and selected reader mounts; no default write target |
| `pi-corporate.json` | Hybrid ordinary corporate session at the corporate URL; no local scopes or mounts |
| `mcp-personal.json`, `mcp-work.json` | Optional MCP configuration examples for their corresponding local credentials |

All generated Pi profiles disable automatic retrieval, capture and sharing.
For example, when `state_dir = "~/.local/state/memory-worlds/local"`:

```sh
env -u MEMORY_WORLDS_URL -u MEMORY_WORLDS_TOKEN_FILE \
  MEMORY_WORLDS_CONFIG="$HOME/.local/state/memory-worlds/local/pi-personal.json" pi
```

The `env -u` options prevent global URL/token overrides from redirecting the
chosen profile. Use independent chats for the audiences described in the
[hybrid guide](../hybrid.md#pi-profiles-and-mixed-sessions).

Private directories use mode `0700`; managed secret/state files use `0600`.
Keep operator credentials out of routine agents. Managed files, locks and
authority SQLite files must each have one hard link. Setup refuses linked
managed files before reading or replacing them. External provider-key and
administrator-token references may have multiple links. Atomic no-overwrite
publication uses supported Linux/macOS rename primitives.

SQLite and active graph storage belong outside synchronized folders. Export
completed [backups](../recovery.md) for transport rather than syncing live files.

## Doctor diagnostics

Doctor reports independent checks, so a valid receipt does not imply a healthy
authority or a working provider:

| Field | Meaning |
| --- | --- |
| `state` | Setup receipt and managed runtime-file consistency |
| `authority` | Recorded authority and provisioning metadata in durable control storage |
| `credentials` | Managed credential-file consistency |
| `personal_credential` | Personal token's offline binding to expected credential ID, user and agent; revocation and native non-admin shape |
| `daemon` | Ownership-lock status |
| `graph` | Owned graph resources and health |
| `api` | Unauthenticated health/readiness response when a running daemon and local authority have been established |
| `api_identity` | Always `not_verified`; health probes do not prove the HTTP peer's identity |
| `model_processing` | Not checked; doctor does not call a model |

The personal credential check does not prove grants. Authority and credential
checks use the same bounded private snapshot of SQLite and its WAL. Doctor never
initializes a store or creates source-directory sidecars; missing paths stay
absent. The temporary snapshot contains raw control-storage bytes and is removed
after inspection. Inspection permits two attempts within two seconds and at
most 64 MiB across control storage and sidecars. Concurrent changes or storage
beyond the bound produce an unavailable diagnostic.

Doctor sends no bearer credentials over HTTP: a daemon can exit and its port
can be reused between inspection and the health request. `api = "health_ready"`
means only that `/healthz` and `/readyz` returned success. Healthy API and graph
checks do not establish extraction or embedding availability. Actual processing
uses the explicitly configured providers, which may be remote.

## Acceptance check

The optional Docker check uses an isolated owned project and local model
emulators:

```sh
mise run setup:smoke
```

It exercises extraction/recall, interruption, repeat apply, restart and retained
volumes without paid model calls. The test removes only its temporary resources;
normal down continues to retain your installation's persistent volumes.
