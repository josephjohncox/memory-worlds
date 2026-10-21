# Set up local and hybrid memory

The guided setup creates one local authority and its private client credentials.
Choose local memory alone, or add read access to selected shared worlds on an
existing corporate service. The same Python controller supports a terminal wizard
and a reviewed TOML configuration. It uses the host Python daemon and a local
FalkorDB container; it does not deploy corporate infrastructure.

Install Docker with Compose support and mise 2026.10.17 or newer. Start the local
Docker engine. From a reviewed source checkout, trust its mise configuration once,
then launch the wizard:

```sh
mise trust
python3 scripts/bootstrap.py
```

The bootstrap uses only the host Python standard library to invoke mise. Mise
installs the locked Python/uv tools and dependencies; the setup controller and
service run with that managed Python. Arguments and the caller's working
directory pass through unchanged. The bootstrap does not install system
packages, trust source automatically or edit your shell configuration.
`mise run setup` launches the same wizard. An installed Python package can use
`memory-worlds setup` without a source checkout or mise; its graph template ships
inside the wheel.

The wizard collects the installation name, owner, private paths, loopback ports,
explicit extraction/embedding settings and optional corporate connection. API
keys use private file references. Interactive secret entry is hidden. Review the
paths, model destinations, credential audiences and generated files before
applying. Existing local authorities require explicit adoption and their local
administrator credential; corporate login supplies no local administrative rights.

## Scripted local setup

Save this as `~/.config/memory-worlds/local/setup.toml` in a private directory.
The model names and endpoints are placeholders for providers you operate.

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

For an authenticated provider, add `api_key_file` to its section. Selecting
`provider = "openai"` requires that reference. Credentials are not inline TOML
fields. Relative paths resolve against the configuration file; `~` expands to the
current user's home directory. No shell is used to evaluate configuration.

```sh
config="$HOME/.config/memory-worlds/local/setup.toml"
mise run setup -- validate --config "$config"
mise run setup -- plan --config "$config"
mise run setup -- apply --config "$config"
mise run setup -- doctor --config "$config"
mise run setup -- run --config "$config"
```

Validate and plan leave authority data unchanged. Apply provisions a stopped
SQLite authority, private operator/agent credentials, an agent-persistent notes
scope, router configuration, client profiles and an owned graph definition. It
does not start background daemons or call a model. Repeated application reuses
recorded identities and secrets. Modified managed files, revoked credentials and
changed authority bindings require explicit review; setup does not silently
repair access by issuing broader credentials. Reapplying a reviewed configuration
can update loopback ports, provider settings, profile output flags and selected
corporate shared worlds. The instance, owner, mode, authority paths and corporate
identity remain pinned. Disabling profile output retains previously generated
profile files; remove unwanted files explicitly after reviewing their consumers.

To adopt an existing stopped SQLite authority, point `data_dir` at it and add
`adopt_admin_file = "~/.config/memory-worlds/existing-admin.token"` as a top-level
setting. The wizard's **adopt** choice records that private file reference.
`setup apply --adopt-admin-file FILE` overrides the configured reference for the
operation. Adoption requires a live local administrator credential; a corporate
user token cannot authorize it.

Run verifies ownership and file hashes, starts the installation's graph, waits
for its health check and serves the API in the foreground. The API and graph
ports bind loopback. Its environment comes from validated setup settings and
referenced secrets; ambient database URLs, OIDC settings and provider keys do not
select a different backend. A remote Docker context is refused.

Press **Ctrl-C** to stop the foreground API. The graph stays running with its
volume. After the daemon has stopped, remove only this installation's graph
containers and network with:

```sh
mise run setup -- down --config "$config"
```

Down refuses an active daemon and retains graph volumes and source data. It does
not kill another process, remove unrelated containers, erase data or register
systemd/launchd services. Missing model keys or damaged client profiles do not
prevent stopping the owned graph; ownership and the graph definition must still
match the setup receipt.

## Add corporate shared memory

Before the first application, use the wizard's hybrid mode, or set
`mode = "hybrid"` and supply a corporate section. Changing an existing local
installation to hybrid requires a new installation; reusing an existing
authority requires explicit adoption.

```toml
[corporate]
url = "https://memory.example.org"
token_file = "~/.config/memory-worlds/corporate.token"
world_ids = ["SELECTED_TEAM_WORLD_ID", "SELECTED_COMPANY_WORLD_ID"]
```

Use canonical shared-world IDs from authenticated discovery. The wizard
preselects none. A normal user token is sufficient; keep the corporate operator
credential off the user's machine. For device login, configure the corporate
`issuer`, public `client_id` and `scopes` instead of an existing `token_file`.
Connect is the explicit login/discovery operation:

```sh
mise run setup -- connect --config "$config"
mise run setup -- plan --config "$config"
mise run setup -- apply --config "$config"
```

Ordinary OAuth authentication can create session/private-work identity state and
reconcile signed group membership at the corporate service. Setup does not write
corporate source records, publish content or administer memberships.

Hybrid application creates separate local personal and work credentials/worlds.
The corporate mount grants read access only to the local work credential, with
publication disabled. Personal profiles have no corporate mounts. Research
profiles have no default write target, automatic capture or automatic retrieval.
A separate direct corporate profile reaches private-work memory; private
corporate worlds cannot become router mounts. See the
[local and corporate memory guide](hybrid.md) for these audience boundaries.

## Generated files and diagnostics

The configured private state directory contains the setup receipt/journal,
managed token files, `compose.graph.yaml`, `mounts.json` and generated
`pi-personal.json`, `pi-work.json`, `pi-corporate.json` and MCP examples as
applicable. No global Pi or MCP client configuration is overwritten. Select a Pi
profile explicitly:

```sh
MEMORY_WORLDS_CONFIG="$HOME/.local/state/memory-worlds/local/pi-personal.json" pi
```

Private directories use mode 0700 and secret/state files use 0600. Keep operator
credentials out of routine agents. Source SQLite and active graph storage stay
outside synchronized folders; use [completed backups](recovery.md) for transport.

Doctor checks configuration/state/file consistency, graph ownership/health and
the API's readiness and authenticated identity without reading source contents
or initializing a store. Missing setup paths remain absent. It does not test
model extraction; a healthy graph and API do not establish model availability.
Authenticated API checks require the authority's daemon lock to be held; a
stopped authority's credential is never sent to another listener reusing its
loopback port. Model processing uses the explicitly configured providers, which
may be remote.

The optional acceptance check uses an isolated owned Docker project and local
model emulators. It exercises actual extraction/recall, interruption, repeated
apply, restart and retained volumes without paid model calls:

```sh
mise run setup:smoke
```

The test removes only its temporary resources. Normal down continues to retain
your installation's persistent volumes.

The repository also provides reviewed starting configurations for
[local setup](../deploy/setup/local.toml) and
[hybrid setup](../deploy/setup/hybrid.toml).
