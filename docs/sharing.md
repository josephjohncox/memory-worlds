# Shared spaces, identity and routing

[Documentation](README.md)

A router mount lets agents on one authority read selected shared worlds on
another. Each authority keeps its own users, grants, source storage and graph.
A mount pins the remote authority ID as well as its URL.

For a personal daemon connected to a corporate service, start with
[hybrid setup](hybrid.md). It creates separate personal and work credentials and
reader mounts for the shared worlds you select.

## Shared-service identity

Users sign in to a shared service with their own OIDC access tokens. Login creates
a stable private-work identity. Team and company access comes from explicit
membership or configured IdP groups. Agent credentials keep their explicit grants;
a user's broader membership does not widen an agent token.

Operators declare company/team/ad-hoc spaces, reusable role profiles and external
groups in one [organization TOML](corporate-access.md). [Okta setup](oidc-okta.md)
covers the provider. The
[routing reference](reference/routing.md#shared-service-identity) records the
membership and session rules.

## Mount a remote shared authority

Mount configuration is an operator-owned JSON file selected by
`MEMORY_WORLDS_MOUNTS_FILE`. It names the remote API, expected authority ID,
selected shared-world IDs, private credential file and local access grants.
Agents cannot add mount URLs or credentials through API requests.

A reader grant permits recall. A publisher grant also permits explicit
publication when the mount enables it. Use a grant for the exact local work
credential when other agents should not inherit access. The remote service
independently enforces its credential's grants.

Mount roles govern the connection. Organization `reader`/`writer`/`manager`
roles govern destination-world access. Per-chat `private`/`review`/`auto` modes
govern user consent to automated excerpt publication. None enables another.

See the [complete mount example](reference/routing.md#mount-a-remote-shared-authority)
and [manual hybrid configuration](reference/hybrid.md). Mounts accept shared
worlds; access private remote memory directly through the API or an
[SSH tunnel](remote-access.md). Use HTTPS outside loopback deployments.

## Recall and explicit publication

With an authorized local agent token:

```sh
mise run cli -- --token-file /private/config/editor.token mounts
mise run cli -- --token-file /private/config/editor.token \
  route-search 'ceramic bearing' --mount work
mise run cli -- --token-file /private/config/editor.token \
  route-publish work TEAM_WORLD_ID --source-id review-42 \
  --at 2026-10-20T12:00:00Z --text 'Reviewed note for the data team.'
mise run cli -- --token-file /private/config/editor.token deliveries
```

Search results retain authority, mount, world and evidence provenance. Partial
failures are explicit; scores from separate authorities are not directly
comparable. Reading never queues a publication.

Publication sends selected text with a stable source ID and timestamp. HTTP 202
means the local outbox entry is durable. Inspect its delivery receipt to determine
whether the remote service accepted it. Retry an unchanged request after a lost
acknowledgment; changed content under the same source ID conflicts.

Dispatch rechecks the initiating credential, current mount grant, pinned authority
and destination. Transient failures retry; access failures block delivery. The
same owner can explicitly retry with a fresh credential after reauthorization.
See [delivery and retry semantics](reference/routing.md#recall-and-explicit-publication).

## Pi and deployment

The [Pi extension](../packages/pi/README.md) keeps automatic private capture separate
from [user-enabled chat sharing](chat-sharing.md). The router itself runs inside
the API daemon in both [local and shared deployments](deployments.md).
