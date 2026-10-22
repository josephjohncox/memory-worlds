# Router configuration and delivery contract

[Documentation](../README.md) · [Guide](../sharing.md)

Each running authority owns its source databases, graph generations, users, and
world permissions. A user's local authority holds personal evidence. A work
authority holds private work contexts and explicit team, company, and ad-hoc
spaces. Credentials and world IDs are meaningful within their issuing authority.
An authority ID pins a router mount to the expected service, including after a
URL is accidentally pointed at a different installation.

For a complete personal-daemon and corporate-service setup, see the
[local and corporate memory guide](../hybrid.md).

## Shared-service identity

Initialize the authority once, then declare identity settings, company/team/ad-hoc
spaces, reusable role profiles and signed group bindings in the
[organization file](organization.md). Follow [company/team setup](../corporate-access.md)
and [Okta provider setup](../oidc-okta.md). No world IDs need copying into group
policy.

A verified issuer/subject receives a stable identity and private-work world.
Group bindings grant only selected shared-world audiences. User memberships,
agent credential grants and delegated scope grants remain separate. Email,
forwarded headers and external administrator claims grant no authority.

Organization policy is read at startup. Mapped-membership changes use accepted
signed token evidence; manual memberships remain independent. See
[policy changes](organization.md#policy-changes) and
[session/revocation behavior](oidc-security.md). Local owners need no identity
provider.

## Mount a remote shared authority

The router runs inside the local HTTP daemon. Set
`MEMORY_WORLDS_MOUNTS_FILE=/private/config/mounts.json` before starting it. The
configuration is an operator-owned file; agent API requests cannot add URLs or
credentials. A minimal configuration is:

```json
{
  "version": 1,
  "mounts": [
    {
      "id": "work",
      "name": "Data team",
      "url": "https://memory.example.test",
      "authority_id": "REMOTE_AUTHORITY_ID",
      "world_ids": ["TEAM_WORLD_ID"],
      "token_file": "work.token",
      "user_grants": {"alice": "publisher"},
      "credential_grants": {},
      "publish": true
    }
  ]
}
```

Obtain the remote authority ID and world ID from its authenticated `me` or
`worlds` response. Store a remote reader/writer credential in the private token
file, never in this JSON or a command argument. Relative token paths resolve
against the configuration file's directory. Credential files must be regular,
bounded files without other-user permissions or group write access. Kubernetes
secret files with mode 0440 are supported. Token files are read for each request,
so operators can rotate the credential without changing the mount.

Use separate remote credentials and mount grants for each local user where user
attribution matters. Shared remote credentials attribute the stored publication
to their remote service identity; the local outbox retains the initiating local
user and agent. Mount grants authorize use of that configured remote credential;
the destination service independently enforces its credential's world grants.

`reader` permits recall. `publisher` permits recall and explicit publication
only when the mount also has `publish: true`. `credential_grants` can override a
user's mount role for one agent credential. Local administrators control mount
configuration and can access configured mounts. Use normal agent credentials in
Pi and MCP clients. Configuration changes require a daemon restart.

Non-loopback HTTP requires explicit `allow_insecure_http: true`; use HTTPS for
ordinary shared deployments. A loopback SSH tunnel is supported. Redirects and
environment HTTP proxies are disabled for routed requests. Remote personal and
private-work worlds are rejected by federation, even if the configured remote
credential could read them. An agent reaches its own private memory directly
through the authenticated API or SSH instead.

## Recall and explicit publication

```sh
memory-worlds --token-file /private/config/editor.token mounts
memory-worlds --token-file /private/config/editor.token \
  route-search 'ceramic bearing' --mount work
memory-worlds --token-file /private/config/editor.token \
  route-publish work TEAM_WORLD_ID --source-id review-42 \
  --at 2026-10-20T12:00:00Z --text 'Reviewed note for the data team.'
memory-worlds --token-file /private/config/editor.token deliveries
memory-worlds --token-file /private/config/editor.token delivery-retry DELIVERY_ID
```

Recall authorizes the entire mount selection before network requests begin.
Results retain mount, authority, world, and source/derived provenance. Each
backend keeps its result order; scores from different authorities are not
treated as comparable. Partial failures are explicit in the response, so an
unavailable work authority cannot silently turn into a complete search result.

A publication request contains selected text, a stable source ID, original
timestamp, and explicit graph-processing choice. It never dereferences local
private evidence or copies a transcript automatically. HTTP 202 acknowledges a
durable local outbox entry, not successful remote delivery. Retrying the same
request returns that entry; changed content under the same source ID conflicts.

The worker verifies the pinned authority, shared destination, mount permissions,
and initiating credential before dispatch. Network failures retry with bounded
backoff. Access/configuration failures block the entry for operator review.
Remote delivery uses a stable publication envelope across restarts; a lost
acknowledgment can replay without creating a second remote episode. The worker
validates the remote receipt before marking it delivered. This relies on the
destination implementing the Memory Worlds idempotent capture contract.

An expired or revoked initiating credential blocks future automatic dispatch.
The same owner can log in with a fresh credential and explicitly retry after
current destination authorization succeeds. Retry rebinds dispatch permission
while retaining the original local user/agent attribution. Other users cannot
retry that entry. Receipt listings omit publication content and token-file paths;
outbox source text remains private durable data under the authority's directory.

Revocation is checked immediately before sending. A remote request already in
flight cannot be recalled. Source publication and graph extraction have separate
receipts: remote source delivery can succeed while extraction is pending or
degraded. There is no automatic whole-session promotion or cross-authority write
transaction.

## Pi and deployment

The [native Pi package](../../packages/pi/README.md) supplies typed tools, slash
commands, evidence rendering, session state, and optional private capture through
Pi's public extension APIs. Publishing is a user slash command, never a model
tool. Private capture is opt-in and stops for contexts exposed to other
authorities. Missing parent history also stops automatic capture, including
in-memory forks and subsequent reloads. No Pi fork is needed for the implemented hooks.

The [Kubernetes guide](../deployments.md) uses the same in-process router and outbox,
mounting operator configuration and token files read-only. SQLite source/control/
outbox storage uses one owning pod and a retained local/block-backed volume in
the local profile. The PostgreSQL profile shares these journals across API
replicas with fenced workers and authority maintenance locks. FalkorDB uses a
separate StatefulSet and PVC. Neptune replaces graph storage only.
