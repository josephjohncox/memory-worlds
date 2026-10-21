# OIDC identity, delegation and revocation

This reference defines the security behavior of the [Okta setup guide](../oidc-okta.md).
It also applies to other configured OIDC providers.

## Identity and verifier policy

Identity is the verified issuer/subject pair. Email and display names do not
select identity. The operator pins issuer, JWKS URL, audience, accepted signing
algorithms and any client/scope policy. Tokens cannot select a key URL. The
verifier checks signature, issuer, audience, subject, expiry and that policy.

Changing the trusted issuer/audience/JWKS/signature/client/scope policy revokes
that issuer's sessions and delegated descendants at startup. Users must obtain
fresh tokens. Group-map changes reconcile memberships separately.

## World membership and scoped delegation

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

## Device login behavior

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
user's broad SSO token into a child process. See [scopes.md](../scopes.md) and
[sharing.md](../sharing.md) for delegation and per-user remote mount credentials.

## Membership changes and revocation

When group mappings are configured, access tokens must include a signed `iat`.
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
