# Configure company and team access

[Documentation](README.md) · [Configuration reference](reference/organization.md)

Declare shared spaces, reusable role profiles and identity-provider groups in one
organization TOML file. The service creates the configured shared worlds and
maps verified groups to their world-shared audiences. Users retain their own
private-work worlds. Sign-in and group membership grant no administration.

This is corporate service configuration. The [local/hybrid setup](setup.md)
connects an ordinary user to that service and has its own TOML file.

## 1. Define the organization

Copy the [Okta example](../deploy/organization/okta.toml) to an operator-managed
location. Configure your provider using [the Okta guide](oidc-okta.md), then edit
the identity settings and the spaces, profiles and groups:

```toml
[identity]
issuer = "https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID"
audience = "api://memory-worlds"
jwks_url = "https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID/v1/keys"
client_ids = ["YOUR_NATIVE_APPLICATION_CLIENT_ID"]
required_scopes = ["memory-worlds"]

[spaces.company]
name = "Company"
kind = "company"

[spaces.data]
name = "Data"
kind = "team"

[spaces.incident]
name = "Incident 42"
kind = "ad-hoc"
alias = "incident-42"

[roles.employee]
worlds = { company = "reader" }

[roles.data-member]
worlds = { data = "writer" }

[roles.data-lead]
worlds = { data = "manager" }

[roles.incident-member]
worlds = { incident = "writer" }

[groups]
mw-company = ["employee"]
mw-data-team = ["employee", "data-member"]
mw-data-leads = ["employee", "data-lead"]
mw-incident-42 = ["incident-member"]
```

Space keys such as `data` stay stable. Their names and aliases can change without
changing world IDs. Aliases default to the space key; this example exposes
`@company`, `@data` and `@incident-42`. No world IDs need copying into policy.

A data lead receives company read access and data management access. If matched
profiles target one world, the strongest role wins. Unmapped groups grant
nothing. [World managers](reference/organization.md#roles-and-access-boundaries)
remain distinct from service administrators.

## 2. Check and deploy

Validate and inspect the expanded policy without a token, database or service:

```sh
mise run cli -- organization validate --config /private/operator/organization.toml
mise run cli -- organization plan --config /private/operator/organization.toml
```

Organization configuration never issues an administrator credential. After
deploying a new service, initialize its authority once using the normal
[Helm](helm.md#3-initialize-the-authority) or [manual setup](manual-setup.md)
procedure. For a host service, select the file in the daemon's environment:

```sh
MEMORY_WORLDS_ORGANIZATION_FILE=/private/operator/organization.toml mise run cli -- serve
```

Keep provider settings and credentials in the existing runtime configuration.
Organization policy contains public identity settings and access assignments,
with no tokens, client secrets or model keys.

For Helm, create a ConfigMap and reference it from your installation values:

```sh
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create configmap \
  memory-worlds-organization --from-file=organization.toml=/private/operator/organization.toml \
  --dry-run=client -o yaml | mise run kubectl -- --context YOUR_CONTEXT apply -f -
```

```yaml
organization:
  existingConfigMap: memory-worlds-organization
  configKey: organization.toml
```

Apply the values using [the Helm guide](helm.md). The chart mounts the file
read-only and sets `MEMORY_WORLDS_ORGANIZATION_FILE`. Restart an existing API
after changing the ConfigMap:

```sh
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds rollout restart statefulset/memory-worlds
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds rollout status statefulset/memory-worlds
```

Use `deployment/memory-worlds` for PostgreSQL. The file is read at startup, without
hot reload. A new deployment may load the complete file from the start; shared
spaces need no manual creation or intermediate empty policy.
For raw manifests, use the [organization Kustomize overlay](reference/kustomize.md).

## 3. Verify as an ordinary user

Use [sign-in and discovery](discovery.md#corporate-sign-in-and-cli) with a normal
user token. A user in `mw-data-team` sees their private-work world plus
`@company` and `@data`, without an administrator token. Shared membership reveals
no other user's private world or worker's private invocation scope.

## Access, routing and publication

| Configuration | Values | Controls |
| --- | --- | --- |
| Organization role profiles | `reader`, `writer`, `manager` per world | Shared-world access from verified groups |
| Local router mount grants | `reader`, `publisher` | Use of a configured remote credential and route |
| Per-chat sharing mode | `private`, `review`, `auto` | User consent to classify and publish selected future excerpts |

Group policy does not enable capture, retrieval or automatic publication. A
writer can publish selected evidence to an authorized destination; per-chat
automation still needs user consent, a fixed destination, exclusions and
scope-flow checks. See [chat sharing](chat-sharing.md) and [hybrid routing](hybrid.md).

Policy changes remove obsolete mapped access at startup. Accepted tokens apply
the current map; external group changes need newly issued tokens. Removed spaces
retain data and independent manual memberships. Keep space keys stable and read
[the change rules](reference/organization.md#policy-changes) before removing entries.
