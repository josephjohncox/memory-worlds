# Scoped agent memory implementation

The owner approved the architecture in the scoped-agent-memory design.

1. Record the upstream Pi Graphiti release, API, license, and compatibility matrix.
2. Add scope/run/invocation/context models, isolated scope journals, and UUID
   identity. Preserve world-shared legacy calls and replay identities.
3. Add scope authorization, attenuated delegation, live ancestor revocation and
   expiry, scoped operations, explicit publication, and adversarial API tests.
4. Partition Graphiti projection, extraction context, provenance, recovery, and
   generation rebuilds by scope. Verify with synthetic adapters and a real
   FalkorDB/Graphiti fixture.
5. Extend Python, MCP, CLI, router, export/restore, and native Pi clients. Bind
   native agent contexts and preserve exposure through lifecycle events.
6. Implement and test the bounded Pi Graphiti compatibility surface; document
   feature differences explicitly.
7. Run independent authorization and graph-boundary review, then required
   Python/Pi/Helm/Kubernetes and packaging checks. Update the checked API schema,
   publish the reviewed private repository commit, and build private artifacts.

Work is partitioned by file ownership. The service owns models/store/API, native
Pi owns packages/pi, and the integration owner handles graph/router/portability
and the remaining clients. Interface changes are communicated before dependent
implementation. The publishing checkout is synchronized only after validation.
