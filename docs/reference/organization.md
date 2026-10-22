# Organization configuration reference

[Documentation](../README.md) · [Operator guide](../corporate-access.md)

`MEMORY_WORLDS_ORGANIZATION_FILE` selects one operator-owned TOML file for OIDC
verification, shared spaces, role profiles and group bindings. It is read at
startup. See the [complete example](../../deploy/organization/okta.toml) and
[Okta provider setup](../oidc-okta.md). Runtime models and credentials remain
separate.

## File structure and checks

| Section | Contents |
| --- | --- |
| `[identity]` | Trusted OIDC verifier and signed-claim settings |
| `[spaces.KEY]` | One company, team or ad-hoc world |
| `[roles.KEY]` | Reusable assignments of built-in world roles to configured spaces |
| `[groups]` | Exact external group names mapped to lists of profile keys |

Run `mise run cli -- organization validate --config PATH` to check the contract,
or `mise run cli -- organization plan --config PATH` to inspect resources and
expanded group assignments. Both work offline without a token or database. A
plan describes configuration; it does not inspect an authority or the user's
actual IdP memberships.

Files must be UTF-8 TOML, at most 1 MiB. Unknown fields, invalid roles, undeclared
space/profile references and duplicate aliases are rejected. Space and profile
keys follow `[a-z][a-z0-9_-]{0,63}`. Group names are exact signed claim values,
including case; quote TOML keys containing spaces or punctuation. No wildcard
group matching occurs. There may be at most 1,000 spaces, profiles and group
bindings, with at most 1,000 assignments per profile or profile references per group.

## Identity settings

| Field | Default | Meaning |
| --- | --- | --- |
| `issuer` | required | Exact trusted OIDC issuer |
| `audience` | required | Audience identifying the Memory Worlds API |
| `jwks_url` | required | Operator-pinned signing-key endpoint |
| `algorithms` | `["RS256"]` | Accepted asymmetric signing algorithms |
| `client_ids` | `[]` | Accepted application clients; empty adds no client allowlist |
| `client_id_claim` | `"cid"` | Claim checked against the client allowlist |
| `required_scopes` | `[]` | OAuth scopes required in every token |
| `scopes_claim` | `"scp"` | Claim containing OAuth scopes |
| `groups_claim` | `"groups"` | Claim containing external group names |
| `cache_ttl` | `300` | JWKS cache lifetime in seconds, 1 to 3,600 |
| `refresh_cooldown` | `5` | Key-refresh interval in seconds, 0 to 60 |
| `request_timeout` | `5` | JWKS timeout in seconds, greater than zero and at most 30 |

Lists use TOML arrays, including a single client, scope or algorithm. The API
audience must differ from every allowed client ID. HTTPS is required outside
loopback development. Tokens select no trusted endpoint or administrator role.
Identity is the verified issuer/subject pair, without an email fallback.

Mapped group access requires a signed `iat`. Group-state freshness is separate
from policy-file freshness. See [OIDC security](oidc-security.md) for the token,
session and revocation rules.

## Spaces

Each `[spaces.KEY]` requires `name` and `kind`. `kind` is `company`, `team` or
`ad-hoc`; personal and private-work worlds are prohibited. Optional `alias`
defaults to `KEY` and follows normal lowercase alias rules. Clients discover an
authorized world as `@alias` and use its canonical ID.

Spaces are independent audiences. A team does not inherit company access from
its kind or name; assign the company profile explicitly to its group when needed.

Startup provisions worlds and records their association with stable space keys.
World IDs derive from the authority UUID and space key; the same file on a
different authority names different worlds. Repeated starts reuse their UUIDs.
Editing a name or alias preserves the world
and world-shared scope IDs. Keys and kinds must remain stable. A new key creates
a separate world rather than renaming one. A recorded world that is missing
causes startup to fail instead of being silently replaced. An alias colliding
with another existing world also fails; an alias cannot reassign its identity.

Authority initialization remains explicit. This file creates no owner or
administrator credential and never replaces an initialized authority.

## Roles and access boundaries

A profile's `worlds` table maps space keys to built-in roles:

| Role | Capability |
| --- | --- |
| `reader` | Read authorized shared evidence and graph knowledge |
| `writer` | Reader access plus selected capture/publication where scope permits |
| `manager` | Writer access plus managed lifecycle operations where scope permits |

Names such as `employee` or `data-lead` are reusable profiles, not new permission
definitions. A group can select several profiles. Matched groups compose, with
`manager` stronger than `writer` and `writer` stronger than `reader` for the same
world. Unknown groups grant nothing.

A manager is not a service administrator. World creation and membership
administration still require an administrative credential. Lifecycle operations
also require scope-management authority. See [lifecycle](../lifecycle.md).

Shared membership permits the world-shared audience. Run-shared,
agent-persistent and invocation-private scopes still need explicit live grants.
Parent agents and team members gain no access to another invocation's notes.
The user's private-work world stays separate from every mapped space.

OIDC sessions use live membership. Local agent credentials keep their explicit
grants; broader user membership does not widen them. Delegated credentials stay
within the parent's live authorization and expiry. See [scopes](scopes.md) and
[session security](oidc-security.md).

## Policy changes

| Change | Result after restarting |
| --- | --- |
| Edit a space name or alias | Update presentation; preserve IDs and data |
| Add a space key | Create a new shared world |
| Change a recorded space kind | Reject the change |
| Rename a space key | Create a new space; retain the old world and data |
| Remove a space, profile or group binding | Remove obsolete mapped access; retain data and independent manual memberships |
| Change verifier policy | Revoke affected OIDC sessions and descendants; require fresh tokens |

Startup clears obsolete mapped memberships. The next accepted token applies the
current map without resetting the signed group-state watermark. A changed file
cannot refresh evidence about external groups. An issued JWT can remain valid
until expiry unless the service revokes its session or disables the user.

Deploy the same file to every API replica. A replica with stale verifier or
resolved group policy returns HTTP 503 during OIDC authentication instead of
restoring old memberships. Startup activates its configured policy, so restarting
a pod with an old file can reactivate that policy. Coordinate updates and roll
every replica using the intended configuration.

Manual memberships are independent. Removing configured access does not erase a
deliberate manual grant. With an operator credential, `memberships WORLD_ID`
shows both sources; `membership-set WORLD_ID USER_ID none` removes the manual
grant. See [immediate revocation](oidc-security.md#membership-changes-and-revocation).

## Deployment and publication

Helm uses `organization.existingConfigMap` and `organization.configKey`
(`organization.toml` by default). The selected key mounts read-only at
`/etc/memory-worlds/organization/organization.toml`, with the environment variable
set by the chart. Keep tokens, model keys and database credentials in their
existing Secrets.

ConfigMap updates require an API rollout: StatefulSet for SQLite, Deployment for
PostgreSQL. See [operator commands](../corporate-access.md#2-check-and-deploy) and
[Helm values](helm-values.md).

Organization policy grants destination access. It does not enable automatic
capture, retrieval or publication. Mount `reader`/`publisher` grants govern a
different connection boundary; Pi `private`/`review`/`auto` modes require per-chat
consent. Classifiers never choose destinations or broaden grants. See
[routing](routing.md) and [chat-sharing policy](chat-sharing.md).
