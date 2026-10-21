# Production completion execution plan

Goal: complete the approved remaining-work backlog with executable evidence.

| Workstream | Owner | Acceptance boundary | State |
| --- | --- | --- | --- |
| PostgreSQL/RDS and distributed ownership | storage agent | Real SQL backend, public contract, process/fence/failover tests | Implemented; native PostgreSQL tests pass |
| Whole-authority encrypted recovery | root | Complete source/control/outbox/graph recovery on an isolated fresh target | Implemented; native graph and cross-backend recovery pass |
| Lifecycle and retention | Pi agent | Retraction/erasure receipts, non-resurrection, scoped retention and generation cleanup | Implemented; 27 cases pass |
| Named identities and Python client/CLI | root | Owner-visible UUID/name registry, stable explicit identities, restricted-child isolation | Implemented; HTTP/CLI and owner/delegation tests pass |
| Native Pi aliases and import | Pi agent | Live resolution, stable named identity, bounded private planner bridge, actual Pi smoke | 138 tests and all three actual smokes pass |
| Qualification and sharing evaluation | qualification agent | Labeled corpus, local model/provider contracts and local OIDC end-to-end in kind | Implemented; real local IdP, model emulators and Cilium checks pass; receipts bind each final image |
| Metrics, scheduled backups and deployments | Pi agent | Tested optional chart resources and backend-appropriate replicas | Eight profiles and 40 negative checks pass; two-replica native lost-volume recovery and scheduled backups pass |
| Artifact synchronization | root | Immutable completed encrypted-artifact transfers and verified download | Implemented; actual local rclone transport tests pass |
| Validation and durable private release | root | Current receipts, all packages/images, signed source and versioned private artifacts | Signed source and audited versioned packages prepared; exact-commit hosted gates and durable private assets are tracked in release receipts |

Work happens in the primary workspace. The real Git publishing checkout is the
private nested repository; only approved public files are synchronized there.
Unrelated edits, private exports and credentials remain outside build inputs.

## Execution order

1. Agree on storage/session/table, lease and identity/planner interfaces between
   workstreams. Record the current design and acceptance criteria.
2. Implement independent planner/identity APIs, local recovery archive mechanics,
   evaluation datasets and native Pi resolution while the storage layer changes.
3. Integrate lifecycle operations and maintenance coordination with the shared
   persistence contract. Exercise SQLite and real PostgreSQL side by side.
4. Add metrics, scheduled backups, backend-aware Helm topology and artifact
   synchronization. Run actual persistence/recovery/network checks in isolated
   environments and the owned local kind cluster.
5. Run independent adversarial reviews, current-contract suites, real Pi and
   local model/IdP emulation in kind. Fix defects before packaging.
6. Refresh documentation, synchronize approved source, sign and push commits,
   build/audit distributions and containers, and retain a durable private release.

## Qualification steering

The user explicitly replaced live qualification with a local OIDC server and
kind-based testing. No external tenant, production context or paid model run is
required. Initial bounded Luna calls made before that steering are labeled as
such in their local receipt; further qualification uses local emulation.
No provider credentials or live-chat contents enter source files.
