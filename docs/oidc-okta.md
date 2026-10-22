# Connect Okta identities and groups

[Documentation](README.md) · [Company and team access](corporate-access.md)

Configure Okta to issue user access tokens for Memory Worlds. The service's
[organization TOML](reference/organization.md) pins the verifier and maps groups
to shared spaces. Each verified identity receives its own private-work world.
Authentication grants no administration.

## Configure the authorization server

Use a custom authorization server, including Okta's `default` custom server,
with issuer `https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID`.
Okta's org authorization server issues tokens for Okta APIs and is unsuitable
for this API. Custom servers require the appropriate API Access Management
entitlement in production. See [Okta authorization servers](https://developer.okta.com/docs/concepts/auth-servers/).

Configure Security → API → your authorization server:

| Setting | Example | Purpose |
| --- | --- | --- |
| Audience | `api://memory-worlds` | Identifies this API; must differ from client IDs |
| Custom scope | `memory-worlds` | Permission to request a token for the API |
| Access-token lifetime | A short operator-selected lifetime | Bounds stale external identity and group evidence |
| Subject | An immutable user identifier | Keeps identity stable when email or login changes |
| Groups claim | `groups`, included in Access Token | Supplies this service's sharing groups |

Set the subject before onboarding users and inspect a token from the actual
policy. Memory Worlds does not use email as an identity fallback. Okta provides
internal user-ID expressions in its [Expression Language reference](https://developer.okta.com/docs/reference/okta-expression-language/).

In Claims, add `groups` with an allowlist/filter covering configured group names.
The [organization example](../deploy/organization/okta.toml) uses `mw-company`,
`mw-data-team`, `mw-data-leads` and `mw-incident-42`. A filter such as `^mw-.*$`
works for matching Okta-sourced groups; directory-sourced groups may need an
expression. Include the claim in access tokens. An ID-token-only claim does not
authorize this API. See [Okta's groups-claim instructions](https://developer.okta.com/docs/guides/customize-tokens-groups-claim/main/).

## Create the workstation client

Create an OIDC Native Application with public-client authentication and the
Device Authorization grant. Assign intended users or groups. In the custom
server's access policy, allow that client, its users, Device Authorization and
the requested `openid` and `memory-worlds` scopes. CLI and Pi clients use the
public client ID; they receive no client secret. See [Okta Device Authorization setup](https://developer.okta.com/docs/guides/device-authorization-grant/main/).

## Configure the corporate service

Copy [the organization example](../deploy/organization/okta.toml). Set its
`[identity]` issuer, audience, JWKS URL, allowed `client_ids` and
`required_scopes`, then configure spaces, profiles and groups using
[the company/team guide](corporate-access.md).

For a custom Okta server, the JWKS URL is usually
`https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID/v1/keys`. Check it
against that server's `/.well-known/openid-configuration`. Keep accepted
algorithms and client/scope policy explicit. See [Okta access-token validation](https://developer.okta.com/docs/guides/validate-access-tokens/main/).

Select the organization file with `MEMORY_WORLDS_ORGANIZATION_FILE`. For
Kubernetes, mount it from an existing ConfigMap; keep provider/model credentials
in the runtime Secret. [Deployment instructions](corporate-access.md#2-check-and-deploy)
cover validation and restarts. JWKS retrieval needs HTTPS egress to Okta;
Graphiti also needs its model endpoints. Standard NetworkPolicy CIDRs do not
follow DNS changes. Use [Helm networking settings](reference/helm-values.md#shared-identity-router-and-ingress)
for the actual gateway and provider network.

## Sign in and discover access

Use [normal-user login commands](discovery.md#corporate-sign-in-and-cli) with the
API URL, issuer, public native client ID and required scopes. Device login prints
a verification URL/code; an SSH session needs no callback listener. The CLI
checks the returned access token against Memory Worlds before saving a new
private token file.

`discover --scopes --json`, Pi's `/memory discover` and `memory_discover` return
only the caller's authorized worlds and scopes. No administrator token is needed.
For a personal daemon connected to corporate memory, use [hybrid setup](hybrid.md).

Login retains neither ID nor refresh tokens and rejects `offline_access`.
After expiry, sign in to a fresh output filename and point the client at it.
Give agents delegated credentials instead of the user's broad SSO token.

## Membership changes and revocation

Newly issued tokens carry updated group evidence. An issued JWT remains valid
until expiry unless this service revokes its session or disables its identity.
Organization policy changes take effect at startup; mapped memberships are
recomputed from accepted token evidence. Manual memberships remain independent.
See [OIDC security](reference/oidc-security.md#membership-changes-and-revocation)
for group freshness and immediate revocation controls.

Signed local JWT/JWKS, authorization and device-flow tests cover the integration.
Automated checks do not provision or exercise a live Okta tenant.
