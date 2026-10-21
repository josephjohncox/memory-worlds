# Discover worlds, scopes and mounts

[Documentation](README.md)

Use your normal user or delegated agent token to list the memory you can access.
You need the service URL and a valid API access token; you do not need an
administrator token or world UUIDs in advance.

## Corporate sign-in and CLI

Your operator supplies the API URL, trusted OAuth issuer, public native client
ID and accepted scopes. Sign in and list accessible worlds:

```sh
umask 077
mkdir -p "$HOME/.config/memory-worlds"
mise run cli -- --url https://memory.example.org login \
  --issuer https://YOUR_OKTA_DOMAIN/oauth2/YOUR_AUTHORIZATION_SERVER_ID \
  --client-id YOUR_NATIVE_APPLICATION_CLIENT_ID \
  --scope openid --scope memory-worlds \
  --token-output "$HOME/.config/memory-worlds/work.token"
mise run cli -- --url https://memory.example.org \
  --token-file "$HOME/.config/memory-worlds/work.token" discover --scopes --json
```

If you already have an API access token, put it in a private token file and run
the discovery command. Browser ID tokens and tokens for another API are not
accepted. Login refuses to overwrite an existing token file. Operator setup is
covered in the [OIDC/Okta guide](oidc-okta.md).

The result contains:

- `worlds`: visible world names, canonical IDs and optional aliases.
- `scopes`: readable scopes keyed by canonical world ID.
- `mounts`: configured remote routes authorized for this credential.

Inspect one world with `discover --world @data-team --json`. If an alias is
ambiguous, select its canonical ID. `me` also shows the current user, credential
and authority identity. Discovery uses the configured service URL; it does not
scan the network or enumerate a remote authority through a mount's token.

## Native Pi and other harnesses

Point the [native Pi extension](../packages/pi/README.md) at the service and your
token file. Keep the token value out of project files and prompts:

```json
{
  "url": "https://memory.example.org",
  "tokenFile": "/absolute/path/to/.config/memory-worlds/work.token"
}
```

Run `/memory discover` or `/memory discover @data-team`. The model can call
`memory_discover` with optional `world` and `include_scopes` arguments. Readable
scopes are included by default; `include_scopes: false` omits them even when a
world is selected.

Discovery only lists access. Select read scopes and a write target separately;
it does not enable automatic retrieval, capture or sharing. See the
[Pi guide](../packages/pi/README.md) for those commands.

Other harnesses can use the exported TypeScript `client.discover()` or Python
`client.discover(include_scopes=True)`, or the authenticated HTTP endpoints:

| Endpoint | Contents |
| --- | --- |
| `GET /v1/me` | Current identity and visible worlds |
| `GET /v1/worlds` | Readable worlds |
| `GET /v1/worlds/{world_id}/scopes` | Readable scopes in one accessible world |
| `GET /v1/mounts` | Authorized configured remote routes |

The [MCP facade](agents.md) exposes the same `memory_discover` tool. Start it with
the user's API token or a scoped agent token:

```sh
mise run cli -- --url https://memory.example.org \
  --token-file "$HOME/.config/memory-worlds/work.token" mcp
```

## Visibility and delegation

An OIDC user sees their private-work world and shared worlds granted through
mapped groups or manual memberships. A world membership does not reveal another
agent's private scopes. Each scope appears only when the current credential can
read it. Discovery returns no credentials, inaccessible names, other-user
directory or membership directory.

A parent harness can discover with the user's token, select authorized scope IDs
and issue a narrower child credential. An isolated Pi subagent discovers with
that child credential: it sees its delegated readable scopes and private
invocation scope, rather than the parent's full world or mount list.

Listings recheck live authorization. Grant removals, session expiry and ancestor
credential revocation affect subsequent requests. External Okta group changes
are observed through newly issued access tokens; the verifier does not poll the
IdP for each listing. See [membership changes and revocation](oidc-okta.md#membership-changes-and-revocation).
Later reads and writes are independently authorized even after discovery.
