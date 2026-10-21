# Set up local and hybrid memory

[Documentation](README.md)

Guided setup gives you a local memory service, private agent credentials and
client profiles. Choose **local** for personal memory, or **hybrid** to also read
selected shared worlds on an existing corporate service. Both use a host Python
daemon and a managed FalkorDB container.

## Start with the wizard

Install Docker with Compose support and mise 2026.10.17 or newer, then start your
local Docker engine. From a reviewed source checkout:

```sh
mise trust
python3 scripts/bootstrap.py
```

The Python bootstrap runs the locked mise toolchain. If the toolchain is already
installed, `mise run setup` opens the same wizard. An installed package also
provides `memory-worlds setup` without a checkout or mise.

Have these settings ready:

- An installation name, local owner and private data/state directories.
- Extraction and embedding models, endpoints, embedding dimensions and any API
  key files. OpenAI requires a key; compatible endpoints can be unauthenticated.
- For hybrid mode, the corporate API URL and either a user token file or the
  public OAuth issuer/client configuration supplied by your operator.

The wizard shows the paths, model destinations, credential audiences and planned
files before saving. Secret entry is hidden. Hybrid discovery uses your ordinary
corporate identity, and no corporate worlds are mounted until you select them.
Accept the final apply prompt to provision the local service. The wizard does
not start the daemon.

If interrupted, rerun with the same `--config` path. Setup resumes its recorded
intent and refuses unrelated or changed state. See [recovery details](reference/setup.md#wizard-recovery)
if you need to understand what was committed.

## Run and stop

The default configuration path is shown below. Substitute the path chosen in the
wizard if you used another one.

```sh
config="$HOME/.config/memory-worlds/default/setup.toml"
mise run setup -- doctor --config "$config"
mise run setup -- run --config "$config"
```

Run starts the owned graph, waits for health and serves the API in the foreground.
The API and graph bind loopback. Press **Ctrl-C** to stop the API; the graph and
its volume remain. Once the API has stopped:

```sh
mise run setup -- down --config "$config"
```

Down removes this installation's graph containers and network while retaining
source data and graph volumes. It requires the recorded ownership and graph
definition to match; missing model keys or damaged client profiles do not prevent
shutdown.

The generated Pi profiles live in `state_dir`. Select the personal profile for
personal chats. Hybrid mode also generates separate work-research and direct
corporate profiles. Automatic retrieval, capture and sharing start disabled.
The [hybrid guide](hybrid.md#pi-profiles-and-mixed-sessions) explains when to use
each profile; the [setup reference](reference/setup.md#generated-files-and-client-profiles)
shows how to launch one.

## Scripted local setup

Copy the [local TOML example](../deploy/setup/local.toml) to a private location
outside the checkout. Set your owner, paths, model names, endpoints and embedding
dimensions, then review and apply it:

```sh
config="$HOME/.config/memory-worlds/local/setup.toml"
mise run setup -- validate --config "$config"
mise run setup -- plan --config "$config"
mise run setup -- apply --config "$config"
mise run setup -- run --config "$config"
```

Validate and plan leave authority data unchanged. Apply requires a stopped API;
it creates or recovers the installation's identities, credentials and profiles
without starting a daemon or calling a model. Repeated apply reuses recorded
identities and secrets. See the [configuration reference](reference/setup.md#configuration)
for path rules, adoption and supported changes.

## Add corporate shared memory

Choose hybrid mode before first apply, or start from the
[hybrid TOML example](../deploy/setup/hybrid.toml). Use authenticated
[discovery](discovery.md) to choose canonical shared-world IDs, then:

```sh
mise run setup -- connect --config "$config"
mise run setup -- plan --config "$config"
mise run setup -- apply --config "$config"
```

Connect pins the corporate authority and user. The local work credential gets
reader access to the selected mounts; the personal credential gets none.
Corporate private-work memory uses a separate direct corporate connection.
Changing an existing local installation to hybrid requires a new installation;
reusing an existing authority requires explicit adoption.

Ordinary OAuth authentication can establish a corporate session and reconcile
private-work identity and signed group membership. Setup does not publish
content or administer corporate memberships. See [local and corporate memory](hybrid.md).

## Generated files and diagnostics

Keep operator credentials out of routine agents. Keep SQLite and active graph
storage outside synchronized folders; transport [completed backups](recovery.md)
instead. `setup doctor` checks local state, authority, credential bindings,
graph health and API readiness without calling a model or sending a bearer over
HTTP. Healthy API/graph checks do not establish model availability.

For file ownership rules, detailed diagnostic fields and interruption behavior,
see the [setup reference](reference/setup.md). For the optional Docker acceptance
check, see [setup validation](reference/setup.md#acceptance-check).
