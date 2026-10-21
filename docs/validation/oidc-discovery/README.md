# Ordinary-user discovery qualification receipts

These public-safe receipts record local Keycloak/kind qualification of normal
user discovery. All 17 checks passed against the image identified in
`qualification-manifest.json`. Per-file hashes bind its 29 installed Python
modules and the host CLI/client/qualification harness to the tested source.
The receipt also binds the actual Kubernetes image manifest to its Docker image
configuration. No Git commit label is inferred from a workspace build.

`oidc-discovery-final.json` records ordinary device-flow login, company/team
memberships, the user's own private world, world aliases, omitted private and
sibling metadata, delegated discovery limits and group removal. The
`cluster-prerequisites.json` and `cleanup.json` files retain setup observations,
the bounded temporary host setting and restoration, and cleanup of owned
resources. Credentials and private runtime paths are omitted.

The new MCP and native Pi discovery tools were verified separately by the
current [Python/Pi checks](../../validation.md#ordinary-user-discovery), including
actual SDK tool execution and private diagnostic canaries. A local identity
emulator establishes the protocol boundary exercised here; this run made no
live identity-provider or model calls. Reproduce it using the
[qualification instructions](../../qualification.md#isolated-kind-cluster-and-oidc).
