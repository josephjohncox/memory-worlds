# Setup review fixes

Implement all recommendations from the independent review of commit
`982c81c610fee8e5396d2e9242533bbb0c4a0752`, approved by the user's instruction
to fix them all. Keep the current configuration and authorization contracts.

1. Make first-run configuration recoverable across process termination. Persist a
   private intent tied to the exact output path, validated nonsecret configuration,
   and pending provider-file hashes. Publish provider files and TOML before the
   corporate connection. Resume matching intent only; refuse unrelated state.
2. Resolve the effective adoption-file override before validation. Preserve the
   saved configuration digest and authenticate the selected administrator during
   adoption. Cover CLI and wizard dispatch.
3. Give doctor a separate durable-authority diagnostic. Inspect metadata through
   read-only source descriptors, with bounded private snapshots when WAL recovery
   is needed. Never initialize storage or create source SQLite sidecars. Report
   uncertainty explicitly rather than asserting a valid authority.
4. Require a single hard link for setup-managed files, locks, and authority
   storage. Keep external provider-key and administrator-token references distinct.
   Verify refusal preserves both aliases and never truncates source data.
5. Complete the wizard's redacted OAuth and planned-files review; explain personal,
   local-work, and direct-corporate profile audiences. Offer required key choices
   immediately for OpenAI. Reject corrupt corporate bindings before network use.
6. Exercise a real mise task and controller with a caller-relative configuration
   filename containing spaces and shell metacharacters. Keep these values as data.
7. Run focused regressions, independent reviews, repository checks, and the local
   Docker acceptance flow. Commit and push the fixes, then build and audit private
   artifacts from the committed source. Record exact validation results and any
   unfinished hosted checks.

Parallel ownership: wizard/recovery; doctor/runtime; managed files/provisioning;
root owns CLI overrides, real mise integration, documentation, and publication.
No live corporate identity provider, paid model, AWS resource, or public release
is required.
