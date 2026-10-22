# LangGraph adapter implementation plan

1. Pin current LangGraph/LangSmith dependencies in an optional extra and lock them.
2. Implement and test private immutable lifecycle/write records and monotonic exposure.
3. Implement the runtime adapter, exact write replay, live ACL/flow checks and trusted
   worker handoff. Keep clients and credentials outside checkpointable values.
4. Build real LangGraph interrupt/restart tests and a runnable explicit-publication
   example with an independent SQLite checkpointer.
5. Implement explicit LangSmith telemetry policy and validated parent propagation;
   qualify official Pi tracing and Memory Worlds together with synthetic local sinks.
6. Add mise/CI integration and clean user guidance; run security/correctness reviews,
   affected tests then full checks and artifact audit.
7. Commit, push the feature branch and open a PR with implementation, validation and
   remaining operational boundaries. Do not merge or alter the running installation.

Parallel ownership: root owns core adapter/tests and wiring; runtime agent owns
LangGraph dependencies/example/tracing/tests/docs; durability agent owns journal
and adversarial journal tests; Pi agent owns native composition/qualified smokes.
