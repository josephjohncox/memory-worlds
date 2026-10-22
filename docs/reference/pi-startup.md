# Pi startup audit

[Pi reference](pi.md) · [Pi quickstart](../../packages/pi/README.md)

Memory Worlds reports connection, capture and sharing state in Pi's bottom status
bar. Use `/memory status` for selections, contexts, graph aliases and delivery
counts. The extension does not reserve a widget above the chat input.

## Run an isolated measurement

From the repository root:

```sh
mise install --locked
mise run pi:startup:audit -- --runs 3 --latency-ms 100 --output /tmp/pi-startup.json
```

The task builds the current extension, starts fresh Pi RPC processes, and serves
synthetic responses from a temporary loopback HTTP server. Each process gets a
temporary home, agent directory, configuration and fixture credential. The audit
does not load your Pi profile, resume conversations, invoke a model or contact
your memory service. The report contains timing data and synthetic identifiers.
Choose a new report filename for each run; existing files are not overwritten.

The five scenarios are Pi alone, explicit read/write scopes, a named agent,
repeated read/write aliases, and automatic capture. `--latency-ms` adds the same
response delay to every fixture HTTP request; use `0` to measure local overhead.
`--runs` accepts 1–20, and `--latency-ms` accepts 0–2,000.

The JSON report records tool versions, the extension entry point's SHA-256,
individual samples, medians, request counts and endpoint timelines. Readiness is
the first successful `get_commands` RPC response after Pi has awaited extension
startup. Shutdown requests are excluded. Every measurement starts a new process;
operating system filesystem caches remain warm. There is no absolute timing
threshold.

To compare another built package or installed Pi CLI without loading its user
configuration:

```sh
mise run pi:startup:audit -- --extension /path/to/memory-worlds-pi \
  --pi-cli /path/to/pi-coding-agent/dist/bundle/cli.js \
  --runs 3 --latency-ms 100 --output /tmp/pi-startup-comparison.json
```

## Findings

The October 21, 2026 audit used Node 26.12.0, Pi 1.3.0 and Linux x64. With
100 ms synthetic delay per HTTP response, three fresh processes per scenario
produced these medians:

| Scenario | Before | After | Startup HTTP requests, before → after |
| --- | ---: | ---: | ---: |
| Pi alone | 134 ms | 159 ms | 0 → 0 |
| Explicit scopes | 617 ms | 406 ms | 4 → 2 |
| Named agent | 715 ms | 399 ms | 5 → 3 |
| Repeated read/write aliases | 1,325 ms | 719 ms | 11 → 6 |
| Automatic capture | 1,028 ms | 717 ms | 8 → 6 |

The main cost was a sequence of HTTP requests. Startup fetched world discovery
and delivery counts for the detailed status display, after already verifying the
credential and binding the context. A named agent added another serial request.
Using the same aliases for read and write repeated their resolution.

Startup now resolves identical configured aliases once, resolves independent
selections concurrently, looks up the credential and named agent concurrently,
and reuses the startup identity for spool binding. Detailed world discovery and
delivery counts are fetched by the explicit `/memory status` command. Context,
credential, inherited exposure and sharing-policy checks still finish before
the extension reports readiness; initialization has no detached background task.

Fresh-process JavaScript import measurements found a median 88 ms for the full
extension, 4 ms for the HTTP client and 7 ms for sharing-session code over five
samples. Graphiti extraction and model clients are not imported during extension
startup. Import measurements overlap and must not be added together.

An isolated startup/load check also passed with the locally installed Pi 1.4.0
and Memory Worlds 0.3.0. This checks package loading, startup hooks and RPC
readiness; it does not qualify all tools, model calls or lifecycle behavior on
Pi 1.4.0. The dependency pin remains Pi 1.3.0.

## Interpret slow startup

Compare `--latency-ms 0` with a delayed run. A large difference points to HTTP
round trips; a slow Pi-only result points to the runtime or host. Compare the
isolated audit with your normal profile to identify the cost of other packages.
The audit does not attribute delays in unrelated extensions.

For a real service, measure authenticated metadata requests separately. Do not
benchmark extraction or model inference as startup work: they are separate
operations. Canonical UUIDs avoid alias discovery requests, while aliases trade
those requests for readable configuration. Both use live authorization.
