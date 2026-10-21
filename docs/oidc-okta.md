# OIDC and Okta shared deployments

Use Okta to sign users into a shared Memory Worlds service and map selected
groups to team, company or ad-hoc worlds. Each signed-in identity also receives
a private-work world. Authentication does not grant administration.

This guide covers operator setup and a user sign-in check. For the exact token,
delegation and revocation rules, see [OIDC security](reference/oidc-security.md).
For a personal daemon connected to this service, use [hybrid setup](hybrid.md).

## Configure the Okta authorization server

Use a **custom authorization server**, including Okta's `default` custom server,
with issuer `https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID`.
Okta's org authorization server issues tokens for Okta APIs and is unsuitable for
this API. Custom servers require the appropriate Okta API Access Management
entitlement in production. See [Okta authorization servers](https://developer.okta.com/docs/concepts/auth-servers/).

Configure these values in Security → API → your authorization server:

| Setting | Example | Purpose |
| --- | --- | --- |
| Audience | `api://memory-worlds` | Identifies this API; must differ from client IDs |
| Custom scope | `memory-worlds` | Explicit permission to request an API token |
| Access token lifetime | A short operator-selected lifetime | Bounds stale external identity and group evidence |
| Subject | An immutable user identifier | Keeps private-work identity stable when email/login changes |
| Groups claim | `groups`, included in **Access Token** | Supplies only the shared-space groups needed by this service |

Configure the subject before onboarding users and inspect a token issued by the
actual policy. Memory Worlds does not use email as an identity fallback. Okta
provides expressions for its internal user ID in the
[Expression Language reference](https://developer.okta.com/docs/reference/okta-expression-language/).

In Claims, add `groups`, choose the Groups value type and an allowlist/filter
covering the intended sharing groups. The example uses `mw-company`,
`mw-data-team`, `mw-data-leads`, and `mw-incident-42`. For Okta-sourced groups a
filter such as `^mw-.*$` avoids returning unrelated memberships. Directory-sourced
groups may require a supported expression. Include the claim in the access token;
putting it only in an ID token does not authorize this API. See
[Okta's groups-claim instructions](https://developer.okta.com/docs/guides/customize-tokens-groups-claim/main/).

Create an OIDC **Native Application** with public-client authentication and the
Device Authorization grant. Assign the intended users or groups. In the custom
server's access policy, permit this client, its intended users, Device
Authorization, and the requested `openid` and `memory-worlds` scopes. No client
secret belongs in a CLI or Pi extension. See
[Okta Device Authorization setup](https://developer.okta.com/docs/guides/device-authorization-grant/main/).

## Configure the deployment

Configure issuer, audience and JWKS URL together. For a custom Okta server,
JWKS is usually
`https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID/v1/keys`; check it
against that server's `/.well-known/openid-configuration`. Also configure the
accepted signing algorithms and client/scope policy. See
[Okta access-token validation](https://developer.okta.com/docs/guides/validate-access-tokens/main/).

Start with `MEMORY_WORLDS_OIDC_GROUP_MAPPINGS={}`. Initialize the authority and
create shared worlds using the operator credential before enabling mappings:

```sh
mise run cli -- --url https://memory.example.org --token-file /private/operator.token \
  world-create Company --kind company
mise run cli -- --url https://memory.example.org --token-file /private/operator.token \
  world-create Data --kind team
mise run cli -- --url https://memory.example.org --token-file /private/operator.token \
  world-create Incident-42 --kind ad-hoc
mise run cli -- --url https://memory.example.org --token-file /private/operator.token worlds
```

Record the returned world IDs. Display names and CLI aliases are discovery
conveniences; authorization mappings use canonical world IDs. A mapping to an
absent or private world fails daemon startup, including groups not yet presented
by a user.

Copy [the Okta environment example](../deploy/k8s/okta.env.example) and
[the provider environment example](../deploy/k8s/runtime.env.example) into a
private operator directory. Replace their issuer, client, model, and world-ID
placeholders, then combine them into the runtime Secret. Keep model configuration
explicit: the service uses Graphiti, and login configuration does not select a
paid provider.

```sh
umask 077
cat /private/operator/providers.env /private/operator/okta.env \
  > /private/operator/runtime.env
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-runtime --from-env-file=/private/operator/runtime.env \
  --dry-run=client -o yaml | mise run kubectl -- --context YOUR_CONTEXT apply -f -
mise run helm -- --kube-context YOUR_CONTEXT upgrade --install memory-worlds \
  deploy/helm/memory-worlds --namespace memory-worlds \
  -f deploy/helm/examples/okta.yaml -f /private/operator/installation-values.yaml \
  --wait --timeout 5m
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds rollout restart statefulset/memory-worlds
mise run kubectl -- --context YOUR_CONTEXT -n memory-worlds rollout status statefulset/memory-worlds
```

The [Helm guide](helm.md) covers image digests, bootstrap, storage, FalkorDB
credentials, TLS certificates, and registry access. The Okta values example
assumes an ingress-nginx gateway; change its NetworkPolicy selectors to match
your gateway. JWKS retrieval needs outbound HTTPS to Okta; graph extraction also
needs the configured model endpoints. Use an enforcing CNI and provider-aware
egress controls where hostname restrictions are required. Standard NetworkPolicy
CIDRs do not follow Okta DNS changes.

Updating an environment Secret alone does not restart pods. The explicit restart
above applies changed group mappings and verifier configuration. During initial
setup, use an empty map until the operator has created the target worlds; apply
the final map in a second rollout.

Use `deployment/memory-worlds` in the rollout commands for the PostgreSQL profile.
Verifier-policy changes revoke existing sessions; [the security reference](reference/oidc-security.md#identity-and-verifier-policy)
describes that boundary.

## Map company, team, and ad-hoc sharing

```json
{
  "mw-company": {"COMPANY_WORLD_ID": "reader"},
  "mw-data-team": {"DATA_TEAM_WORLD_ID": "writer"},
  "mw-data-leads": {"DATA_TEAM_WORLD_ID": "manager"},
  "mw-incident-42": {"INCIDENT_WORLD_ID": "writer"}
}
```

Roles are `reader`, `writer`, and `manager`. When several matched groups target
one world, the strongest role wins. Unknown groups grant nothing. Only the
operator's configured map selects destinations; token group strings cannot name
arbitrary worlds. A writer publishes selected evidence through an explicit
publication action. Group mapping does not copy a private transcript.

Shared-world memberships grant the world-shared audience. Run, agent and
invocation scopes still need explicit live grants; group maps cannot target
personal or private-work worlds. Manual memberships remain independent of
mapped memberships. See [membership and delegation rules](reference/oidc-security.md#world-membership-and-scoped-delegation).

## Sign in from a workstation or SSH session

With the [repository tools](development.md) installed, run:

```sh
umask 077
mkdir -p /private/memory-worlds
mise run cli -- --url https://memory.example.org login \
  --issuer https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID \
  --client-id YOUR_NATIVE_APPLICATION_CLIENT_ID \
  --scope openid --scope memory-worlds \
  --token-output /private/memory-worlds/session.token
mise run cli -- --url https://memory.example.org \
  --token-file /private/memory-worlds/session.token me
mise run cli -- --url https://memory.example.org \
  --token-file /private/memory-worlds/session.token discover --scopes --json
```

Complete the printed verification URL/code in a browser; an SSH session needs
no callback listener. The returned access token must pass this API before the
CLI creates its private file. Discovery uses the user's token and returns only
authorized worlds/scopes; no administrator token is needed. Pi and MCP expose
the same catalog through `memory_discover`. See [discovery](discovery.md).

Login stores neither ID nor refresh tokens and rejects `offline_access`. When
the token expires, sign in to a fresh output filename and point the client at it.
Use scoped delegation for agents rather than passing the user's SSO token into
a child process. See [device-flow details](reference/oidc-security.md#device-login-behavior).

## Membership changes and revocation

New verified access tokens carry updated Okta group evidence. An already issued
JWT remains valid until expiry unless this service disables the user or revokes
the session. Use short access-token lifetimes and the service's immediate
revocation controls; see [revocation behavior](reference/oidc-security.md#membership-changes-and-revocation).

Signed local JWT/JWKS, authorization and device-flow tests cover this integration.
The automated checks do not provision or exercise a live Okta tenant.
