# Guided setup implementation

The approved contract is [the guided setup design](../specs/2026-10-21-guided-setup-design.md).
The implementation uses one TOML schema and the current API and Pi contracts.

1. Implement bounded private file operations and strict local/hybrid configuration.
2. Build local provisioning with durable secrets before credential commits, canonical
   IDs in authority metadata, interrupted-run recovery, and explicit adoption.
3. Build ordinary-user corporate connection and the interactive configuration flow.
   Pin authority and user; mount only explicitly selected shared worlds.
4. Package the graph-only Compose template; implement foreground run, read-only
   inspection, owned-resource shutdown, a standard-library-only Python bootstrap,
   and mise targets. The bootstrap invokes locked mise tools with the original
   arguments and caller directory; the controller and service use managed Python.
5. Integrate CLI dispatch and test configuration, provisioning, connection, resource
   ownership, interruption, credential isolation, and installed-wheel resources.
6. Review the integrated setup boundary, run repository checks, build private
   artifacts, and commit and push the completed feature.

Parallel file ownership: root owns config/files/controller/CLI and integration;
provisioning owns setup_provision, narrow store hooks, and provisioning tests;
interactive owns setup_interactive and its tests; runtime owns setup_runtime,
the packaged template, bootstrap/tasks, runtime tests, and setup documentation.

No live Okta, AWS resources, paid model calls, automatic public release, or existing
corporate deployment changes are required for this deliverable. Normal corporate
authentication can create sessions and reconcile user/group state.

The implementation now includes real local runtime acceptance in
`mise run setup:smoke` and ordinary-user hybrid integration over real HTTP. CI
runs the owned runtime acceptance with the current primary Python. The setup
tests cover process-exit recovery, live lock ownership, adoption, reconfiguration,
normal additional private scopes, token rotation and generated-profile audiences.
Artifact auditing requires the installed graph template as well as the setup
controller; no adjacent deployment checkout is needed by the wheel.
