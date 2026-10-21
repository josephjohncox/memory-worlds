# User and harness discovery

An ordinary OAuth access token issued for the configured Memory Worlds API can
discover the caller's worlds, readable scopes and configured remote mounts.
Discovery requires the service URL and a valid user or delegated agent token.
It does not require an administrator token or advance knowledge of world UUIDs.

## Corporate sign-in and CLI

The operator configures the trusted issuer, API audience, public native client,
accepted OAuth scopes, and group-to-world mappings as described in the
[Okta guide](oidc-okta.md). A normal user signs in using that client:

```sh
umask 077
mkdir -p ~/.config/memory-worlds
mise run cli -- --url https://memory.example.org login \
  --issuer https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID \
  --client-id YOUR_NATIVE_APPLICATION_CLIENT_ID \
  --scope openid --scope memory-worlds \
  --token-output ~/.config/memory-worlds/work.token
mise run cli -- --url https://memory.example.org \
  --token-file ~/.config/memory-worlds/work.token discover --scopes --json
```

An existing valid API access token can be supplied through the same private
token file. The service validates its signature, issuer, audience, expiry and
configured application/scope policy before discovery. Browser ID tokens and
tokens issued for a different API do not satisfy that contract.

The result contains `worlds`, `scopes` keyed by canonical world ID, and `mounts`.
World/scope records include names, IDs and optional aliases. Use
`discover --world @data-team --json` to inspect one visible alias, or select a
canonical world ID from the first result. Ambiguous aliases require an ID.
Mounts are the configured remote routes authorized for this caller; discovery
does not scan the network or enumerate another authority through a broader token.

## Native Pi and other harnesses

Point the native Pi configuration at the shared service and the user's token
file. Do not put the token value in project files or prompts:

```json
{
  "url": "https://memory.example.org",
  "tokenFile": "/absolute/path/to/.config/memory-worlds/work.token"
}
```

Use `/memory discover` or `/memory discover @data-team`. The model can call
`memory_discover` with optional `world` and `include_scopes` arguments. Readable
scopes are included by default; `include_scopes: false` omits scope enumeration,
including when a world is selected. Discovery does not select reads, enable ambient
retrieval, capture or sharing, or set a write destination. The user chooses those
separately. See the [Pi package](../packages/pi/README.md) for installation and
selection commands.

The exported TypeScript client provides `client.discover()` and the Python
client provides `client.discover(include_scopes=True)`. Both use the same
authenticated HTTP listing endpoints. Other harnesses can call them directly:

| Endpoint | Contents |
| --- | --- |
| `GET /v1/me` | Current identity and visible worlds |
| `GET /v1/worlds` | Readable worlds |
| `GET /v1/worlds/{world_id}/scopes` | Readable scopes in one accessible world |
| `GET /v1/mounts` | Authorized configured remote routes |

The MCP facade also exposes `memory_discover`. Start it with the user's API token
file, or a scoped agent token file:

```sh
mise run cli -- --url https://memory.example.org \
  --token-file ~/.config/memory-worlds/work.token mcp
```

## Visibility and delegation

An OIDC user sees their own private-work world and the company, team and ad-hoc
worlds granted through current mapped or manual memberships. Shared-world
membership does not reveal another agent's private scope. A scope appears only
when the current credential can read it. Discovery does not reveal a directory
of other users, their memberships or inaccessible world/scope names.

The parent harness can discover using the user's token, choose authorized scope
IDs, and then issue a narrower child credential. Isolated Pi subagents also have
`memory_discover`, using only that child credential. They see their delegated
scopes and private invocation scope, subject to the actual delegation; they do
not inherit the parent's entire world or mount list. Credentials are never
returned by discovery.

Listings recheck live service authorization. Removed grants, expired sessions
and revoked ancestor credentials are enforced on subsequent requests. External
Okta group changes are observed through newly issued access tokens; the service's
local JWT verifier does not poll the IdP for each listing. The
[OIDC revocation rules](oidc-okta.md#membership-changes-and-revocation) apply.
Subsequent reads and writes are independently authorized even after discovery.
