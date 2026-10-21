# OIDC and Okta shared deployments

Memory Worlds verifies OAuth access tokens itself. Identity is the pair of signed
issuer and subject claims. Each identity receives a private-work world; team,
company, and ad-hoc membership must come from an operator mapping or an explicit
membership grant. Native agent delegation then narrows that access to selected
UUID scopes. An authenticated user does not become an authority administrator.

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

The operator pins issuer, JWKS URL, audience, and accepted signing algorithms.
For the usual custom Okta server, JWKS is
`https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID/v1/keys`. Check it
against that server's `/.well-known/openid-configuration`; tokens cannot choose a
key URL. The verifier checks signature, issuer, audience, subject, expiry, and
configured application/scope policy. See
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
kubectl --context YOUR_CONTEXT -n memory-worlds create secret generic \
  memory-worlds-runtime --from-env-file=/private/operator/runtime.env \
  --dry-run=client -o yaml | kubectl --context YOUR_CONTEXT apply -f -
helm --kube-context YOUR_CONTEXT upgrade --install memory-worlds \
  deploy/helm/memory-worlds --namespace memory-worlds \
  -f deploy/helm/examples/okta.yaml -f /private/operator/installation-values.yaml \
  --wait --timeout 5m
kubectl --context YOUR_CONTEXT -n memory-worlds rollout restart statefulset/memory-worlds
kubectl --context YOUR_CONTEXT -n memory-worlds rollout status statefulset/memory-worlds
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

Changing the trusted issuer/audience/JWKS/signature/client/scope policy revokes
that issuer's existing sessions and their delegated descendants at startup.
Users must obtain fresh tokens. Group-map changes instead reconcile memberships
as described below.

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

The boundaries are independent:

- A shared-world membership permits that world's world-shared audience.
- Run-shared, agent-persistent, and invocation-private scopes still require their
  own explicit live grants. Parent agents and team members receive no implicit
  access to another invocation's scratch memory.
- Every identity's private-work world remains separate. Group maps cannot target
  personal or private-work worlds.
- OIDC sessions use live user membership. Delegated agent credentials have
  selected scopes, check their parent chain, and cannot outlive the issuing SSO
  session. Separately operator-issued local credentials retain only their own
  explicit grants and must be revoked separately.

For a temporary space without a permanent IdP group, create an ad-hoc world and
use `membership-set WORLD_ID USER_ID writer`; remove the manual membership with
`membership-set WORLD_ID USER_ID none`. Manual and mapped memberships are
independent: removing a group does not erase an intentional manual grant.

## Sign in from a workstation or SSH session

Install the repository's pinned tools and dependencies with `mise install
--locked` and `mise run deps`, then run:

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
  --token-file /private/memory-worlds/session.token worlds
mise run cli -- --url https://memory.example.org \
  --token-file /private/memory-worlds/session.token scopes WORLD_ID
mise run cli -- --url https://memory.example.org \
  --token-file /private/memory-worlds/session.token mounts
```

The CLI prints the verification URL and short code to stderr. Complete login in
a browser on any suitable device; an SSH session needs no callback listener.
Polling honors pending/slow-down responses and the device lifetime. Discovery,
token, and verification endpoints must stay on the configured issuer origin;
remote plaintext HTTP and redirects are rejected. Returned access tokens must
pass the target Memory Worlds API before the CLI creates its mode-0600 file.
Existing files are never overwritten. Only nonsecret identity/file metadata is
printed to stdout.

Login keeps neither an ID token nor a refresh token, and rejects requests for
`offline_access`. On expiry, run login again with a fresh output filename and
point the client at that file. Use scoped delegation for agents; do not pass the
user's broad SSO token into a child process. See [scopes.md](scopes.md) and
[sharing.md](sharing.md) for delegation and per-user remote mount credentials.

## Membership changes and revocation

Okta group changes are observed through newly minted, verified access tokens.
The service rejects older signed group states after accepting newer claims and
rejects conflicting group states at the same `iat`. Changing the operator map
clears obsolete mapped memberships at startup; the next accepted token applies
the new map. This does not turn an old token into fresh IdP evidence.

Local signature validation does not query Okta on every request. Removing a user
from an Okta group, disabling them in Okta, or revoking a token in Okta therefore
does not instantly invalidate an already issued JWT here. Bound this window with
short access-token lifetimes. For immediate service-side removal, the operator
can disable the Memory Worlds identity with `user-set USER_ID --disable` or
revoke a known session using `token-revoke CREDENTIAL_ID`. This also blocks its
delegated descendants. Re-enabling a disabled user requires a fresh external
token; revoked credentials stay revoked.

The implementation is covered by signed local JWT/JWKS tests, HTTP authorization
tests, and native device-flow tests. A live Okta tenant has not been provisioned
or exercised by this repository's automated checks.
