# Ordinary-user discovery

An ordinary OIDC access token issued for the configured Memory Worlds API must
discover the caller's readable worlds, scopes and configured remote mounts.
Authority administration is not required. The user supplies the service URL and
signs in through the configured public native OAuth application.

The current HTTP API already implements the authorization boundary:
`GET /v1/worlds`, `GET /v1/worlds/{world_id}/scopes`, and `GET /v1/mounts` validate
the current credential. OIDC identities use their current manual and mapped
memberships. Scope grants and delegated credentials further narrow visibility.
Private worlds and ungranted scopes remain absent from discovery. Mapping changes,
newly observed signed group changes, expiry and revocation follow the same rules
as source reads. Discovery does not acquire new permissions.

Keep these existing endpoints as the authority. Do not introduce an administrator
catalog, a service credential, a second authorization implementation, or a
credential exchange solely to list accessible objects. The Python client and CLI
already compose these endpoints. Add equivalent typed discovery to the native
Pi client, `/memory discover [WORLD]`, and a `memory_discover` tool in the Pi
extension, isolated Pi SDK child sessions, and MCP facade. Tools include readable
scopes by default and accept an optional world ID or visible `@alias`.

Return canonical world/scope IDs, names, aliases and the existing mount views.
Never return bearer tokens or credential file paths. Child discovery uses only
the child's delegated credential; it cannot use a parent token to enumerate a
broader catalog. Discovery does not change read selection, enable retrieval,
capture or sharing, bind a context, or choose a write destination. Subsequent
operations still perform their own live authorization.

Verify ordinary signed OIDC users across HTTP/Python/CLI, mapped company/team and
manual memberships, omitted sibling/private scopes, delegated discovery and
membership removal. Verify Pi/MCP tool surfaces, read-only requests, credential
pinning, failure propagation and unchanged selections. Strengthen the local
Keycloak qualification with the actual device-flow token and discovery command.
