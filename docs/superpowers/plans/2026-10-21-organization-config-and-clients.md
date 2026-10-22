# Organization policy and local clients

The user requested config-driven company, team, role and sharing mappings, then
authorized bootstrapping this workstation, installing Pi, and adding integrations
and documentation for Codex, Claude Code and OpenCode.

## Organization configuration

Replace inline OIDC environment maps with one operator-owned `organization.toml`,
selected by `MEMORY_WORLDS_ORGANIZATION_FILE`. `[identity]` pins the verifier;
`[spaces.KEY]` declares company/team/ad-hoc worlds; `[roles.KEY].worlds` bundles
the existing reader/writer/manager roles; `[groups]` maps exact IdP group names
to role-bundle names. Unknown references and private-world kinds fail validation.

Startup provisions declared shared worlds with stable authority-scoped IDs before
reconciling OIDC grants. Registry ownership prevents adoption of unrelated worlds.
Display-name/alias changes preserve IDs; changing a key declares another world.
Removed declarations retain data and independent manual grants; removed mapped
access is reconciled by the existing signed-token and live-membership rules.
Group access grants world-shared memory only and never enables chat publication.

Helm references an existing ConfigMap and mounts the same file read-only at
`/etc/memory-worlds/organization/organization.toml`. Provider and database secrets
remain separate. Updates require a workload restart. Add offline organization
validate/plan commands and one concise operator guide plus exact reference.

## Workstation and harnesses

Inspect the workstation without printing credentials. Use the approved existing
Pi model gateway for extraction and embeddings; inspect its model catalog and
configure explicit supported models. Install a persistent user-owned runtime
outside `/tmp`, provision personal SQLite/FalkorDB, register/start the user service,
and install the built Pi package while preserving unrelated Pi configuration.

Build reusable MCP client profiles for Codex, Claude Code and OpenCode, using
private credential files and the same HTTP service. Add native client packaging
where supported and useful; automatic capture/sharing stays disabled. Verify
official current harness contracts, preserve existing client settings, and keep
operator credentials out of ordinary clients. Document connection, discovery,
private scope selection, manual publication and removal.

## Verification

Test organization schema, deterministic IDs, collision rollback, repeated startup,
role merging, binding removal, manual grants, private scopes and delegated access.
Update signed OIDC/Keycloak qualification fixtures and Helm schema/render checks.
Test MCP handshakes/tool calls and native plugin boundaries against installed
client runtimes where available. Exercise real local API/graph startup and report
which model and OS-manager checks actually ran. Audit build artifacts, commit and
push the completed changes to the existing private repository.
