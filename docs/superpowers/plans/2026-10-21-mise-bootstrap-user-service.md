# Mise bootstrap and optional user service

The user requested mise as the documented bootstrap and an option to install a
daemon. This extends the approved guided setup with explicit service management.

## Design

Use `mise trust`, `mise install --locked`, and `mise run setup` as the primary
source-checkout flow. Retain the Python bootstrap as an optional mise launcher.
There is one setup controller and one runtime, with no shell bootstrap or second
dependency installer.

Add `setup service install/start/stop/restart/status/uninstall` for systemd user
services on Linux and launchd user agents on macOS. Install registers login
startup; `--start` separately authorizes starting now. `setup init --service`
installs after the wizard's explicit apply step. It does not start the daemon.

The service invokes the current environment's absolute Python interpreter with
`-I -m memory_worlds.cli setup run --config ABSOLUTE_TOML`. Preserve the virtualenv
interpreter path rather than resolving its symlink. Isolated Python ignores
ambient Python import overrides. The API child runs in the private state
directory with an explicit minimal PATH containing Docker. Store no secrets in
service definitions. The configured runtime continues to read private key files
and enforce authority ownership, loopback binding, and a single daemon.

Keep service ownership separate from setup provisioning. Bind a service name to
the instance and authority. Record intended definition bytes and their digest
before publishing the manager file, so interrupted installation can resume.
Refuse unrelated, changed, symlinked or hardlinked service files. Never chmod
shared systemd/LaunchAgents directories. Stop and uninstall retain data,
credentials, profiles and graph storage; `setup down` remains graph shutdown.
Neither install nor its tests change sudo, linger or system-level services.

## Implementation and checks

1. Add service rendering, owned file publication, manager dispatch and receipts.
2. Add CLI actions and explicit wizard flag, with command dispatch tests.
3. Make README and setup guide mise-first; document lifecycle, logs, login
   behavior, Docker prerequisites and stable runtime paths in setup reference.
4. Test installation recovery, collisions, special path characters, secret
   exclusion, current virtualenv execution, manager failures and retained data.
5. Validate real systemd unit syntax locally, exercise the existing real mise
   bootstrap check, and run setup tests plus Python lint/format checks. macOS
   manager behavior is tested by command contracts on this Linux environment;
   do not claim live launchd qualification.
