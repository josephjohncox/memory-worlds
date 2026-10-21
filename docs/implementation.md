# Implementation

Approved design: `superpowers/specs/2026-10-20-memory-worlds-design.md`.

1. Implement durable, scoped source storage, per-agent credentials, idempotent
   capture, and daemon ownership. Exercise concurrency and access isolation.
2. Implement versioned portable world export/restore and branch-preserving
   ChatGPT/Pi import with synthetic fixtures. Verify checksum failures and
   transactional conflict handling.
3. Implement an actual Graphiti/FalkorDB adapter and durable worker, with
   conservative interrupted-write handling and isolated generation rebuilds.
4. Connect authenticated HTTP, typed client, CLI, and optional stdio MCP client.
5. Document local launch, SSH forwarding, cloud-folder backup, graph provider
   configuration, and the limits of source-only recovery.
6. Run integration checks against independent HTTP clients and clean restore;
   inspect package contents for private inputs and organization branding.

7. Add operator-configured shared-authority mounts, bounded attributed recall,
   and explicit publication with a durable outbox. Verify outage, lost-ACK,
   restart, source identity, revocation, and owner retry semantics.
8. Add an explicit-issuer OIDC verifier, stable private work contexts, live
   shared memberships, and signed group-state watermarks. Keep agent token
   grants separate from user memberships.
9. Build a native Pi package using the current public SDK: tools, slash commands,
   rendering, branch state, bounded retrieval, and optional private capture.
   Publication remains a user command. Exercise the real Pi runtime.
10. Add Kubernetes StatefulSets/PVCs, current kind tooling, secret injection,
    worker readiness, and pod-replacement checks. Add the optional Neptune
    adapter with offline tests and an explicit experimental deployment overlay.
11. License the Python and Pi packages under Apache-2.0 and audit public package
    artifacts for private inputs. See validation.md for current executed results
    and deployments.md for storage and AWS limits.
