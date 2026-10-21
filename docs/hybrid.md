# Run local and corporate memory together

[Documentation](README.md)

Use a local service for personal memory and an independent corporate service for
work knowledge. The [guided setup](setup.md) creates separate client credentials
and profiles, then mounts only the corporate shared worlds you select.

A mount routes queries to the corporate API. It does not copy a database, sync
worlds or provide an offline replica. Each service keeps its own identity,
permissions, durable journals and Graphiti backend.

## Choose where information belongs

| Information | Store it in | Connect through |
| --- | --- | --- |
| Personal notes and preferences | Local personal world and private scopes | Personal local profile |
| Work notes allowed on your machine | Local private-work world and private scopes | Local work credential |
| Work notes that must remain on corporate infrastructure | Corporate private-work world and private scopes | Direct corporate profile |
| Team procedures, decisions and shared knowledge | Corporate team, company or ad-hoc world | Selected shared mount or direct corporate profile |
| Subagent working notes | Explicitly delegated invocation-private scope | That subagent's credential |

Credentials enforce these boundaries; names and UUIDs do not grant access.
Corporate private-work is private from other ordinary users under the service's
grants. Corporate operators still control its storage. Local storage also does
not mean local model processing: choose extraction, embedding and Pi model
providers appropriate to the information.

## Prepare the corporate service

Your operator supplies a corporate API URL, a public OAuth client configuration
or user API token, and access to the relevant worlds. Normal user discovery and
mounts do not require an administrator token.

For operators, use the [Helm guide](helm.md), [OIDC/Okta configuration](oidc-okta.md)
and [RDS PostgreSQL guide](rds-postgresql.md). Shared journals use PostgreSQL;
Graphiti uses a separate graph backend. No Neptune service or PostgreSQL graph
extension is needed.

## Prepare the local service and work credential

Start [guided setup](setup.md#start-with-the-wizard), select **hybrid**, and sign
in with your corporate identity. Select only the shared worlds needed by local
work agents. None are preselected. Review the model destinations and generated
profiles, apply, then run the local service.

Already operating a manually configured local service? The
[manual hybrid reference](reference/hybrid.md#prepare-the-local-service-and-work-credential)
shows how to create a separate work credential without broadening the personal
credential.

## Mount selected corporate shared worlds locally

Guided setup mounts the selected worlds for the exact local work credential as
readers with publication disabled. Another credential for the same local user
does not inherit that route. Personal profiles have no corporate mounts.

Local discovery lists local scopes and authorized route metadata. Use direct
[corporate discovery](discovery.md) to list remote worlds and private scopes.
Queries through a mount retain remote provenance and report partial failures.
Corporate private-work worlds and private scopes cannot be mounted.

See [manual mount configuration](reference/hybrid.md#mount-selected-corporate-shared-worlds-locally)
for the JSON format, grants and verification commands.

## Pi profiles and mixed sessions

Choose a profile when starting an independent chat:

| Profile | Intended use | Default behavior |
| --- | --- | --- |
| `pi-personal.json` | Personal local memory | Private local write target; no corporate mounts |
| `pi-work.json` | Local work research with selected corporate shared worlds | Reader mounts; no default write target |
| `pi-corporate.json` | Corporate private-work and shared knowledge | Direct corporate endpoint; no local scopes or mounts |

All generated profiles start with automatic capture, retrieval and sharing off.
Use `/memory discover`, `/memory mounts` and explicit `/memory search` to inspect
and query access. See [profile examples and launch commands](reference/hybrid.md#pi-profiles-and-mixed-sessions).

An explicit routed search sends its query to the corporate service. Enabling
automatic retrieval sends the first 2,000 characters of future prompts as queries
to selected mounts. Private sharing mode does not stop outbound retrieval.

Foreign evidence seen through the native extension disables automatic local
capture and automated chat sharing. Forking, compacting, changing branches or
removing a mount does not clear that exposure. Start a fresh independent
local-only chat before enabling automatic local capture. Other tools and
extensions are outside this exposure tracking.

## Direct corporate private-work access

Use `pi-corporate.json` or a client pointed directly at the corporate URL with
your corporate token. Discover readable scopes, then choose the read selection
and write target separately. Capture requires an authorized agent-persistent or
invocation-private scope. The [direct-access reference](reference/hybrid.md#direct-corporate-private-work-access)
shows how to create and select one.

Local delegation does not propagate corporate tokens. Restricted delegated local
subagents cannot use mounts, even when their parent can. Corporate subagent work
needs a separately authorized corporate client and credential.

## Publish selected work knowledge

Reader mounts cannot publish. Publication requires both a local publisher mount
with `publish: true` and corporate write authorization for the destination.
Publish selected text explicitly; an outbox enqueue is distinct from confirmed
remote acceptance and later graph extraction. See [publication commands](reference/hybrid.md#publish-selected-work-knowledge).

[Per-chat sharing](chat-sharing.md) is a separate user-enabled rule with a fixed
destination, classifier and exclusions. Mixed-authority sessions remain
ineligible for automatic sharing; selected explicit publication remains
available.

## Rotation, outages, tunnels and recovery

Keep corporate tokens current, use SSH for transport when needed, and back up the
two services independently. Corporate outages leave local sources available;
routed failures are reported instead of presented as complete empty results.
Mounts do not make offline replicas, although local Pi sessions, tool results and
publication outboxes can contain work text handled on the machine.

See the [operations reference](reference/hybrid.md#rotation-outages-tunnels-and-recovery)
for token rotation, blocked-delivery retries, tunnel ports and backup rules.
