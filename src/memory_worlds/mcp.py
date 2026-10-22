"""Optional stdio MCP facade. The HTTP daemon remains the only storage owner."""

from typing import Annotated, Any
from uuid import UUID

from pydantic import Field

from . import __version__
from .client import Client
from .maintenance_models import MaintenanceReceipt, MaintenanceRequest
from .models import (
    AgentContext,
    BindContext,
    Episode,
    EpisodeInput,
    Publication,
    Receipt,
    Scope,
    ScopeFlow,
    ScopeSelection,
    SearchRequest,
    World,
)
from .mounts import MountView
from .outbox import Delivery, RouterSearch
from .thread_models import Thread


def create_mcp(url: str, token: str):
    """Create a native SDK 2 server whose tools delegate to the HTTP authority."""
    try:
        from mcp.server import MCPServer
    except ImportError:
        raise RuntimeError("install memory-worlds[mcp] with MCP SDK 2 for this transport") from None

    mcp = MCPServer("Memory Worlds", version=__version__)

    @mcp.tool()
    def memory_discover(world: str | None = None, include_scopes: bool = True) -> dict[str, Any]:
        """Discover this caller's readable worlds, scopes and configured mounts.

        Accepts an ordinary API access token or delegated agent credential, without
        administrator privileges. Optional world selection accepts an ID or @alias.
        Discovery does not select reads, enable capture, or change write destinations.
        """
        try:
            with Client(url, token) as client:
                return client.discover(world=world, include_scopes=include_scopes)
        except Exception:
            # SDK tool results are masked, but its server logger includes the
            # exception chain. Validation errors can quote upstream private data.
            raise RuntimeError(
                "Memory discovery failed; verify service, credential and world selection"
            ) from None

    @mcp.tool()
    def memory_threads(world_id: str | None = None, include_expired: bool = False) -> list[Thread]:
        """Discover this caller's private threads without expanding reads or destinations."""
        try:
            with Client(url, token) as client:
                return client.threads(world_id=world_id, include_expired=include_expired)
        except Exception:
            raise RuntimeError("Thread discovery failed; verify service and credential") from None

    @mcp.tool()
    def memory_thread(thread_id: UUID) -> Thread:
        """Inspect one authorized thread's revision, retention state and private scope."""
        try:
            with Client(url, token) as client:
                return client.thread(thread_id)
        except Exception:
            raise RuntimeError(
                "Thread inspection failed; verify credential and thread selection"
            ) from None

    @mcp.tool()
    def memory_context_bundle(thread_id: UUID, bundle_id: UUID) -> dict[str, Any]:
        """Recover archived thread context. Its contents retain their private source audience."""
        try:
            with Client(url, token) as client:
                return client.context_bundle(thread_id, bundle_id).model_dump(mode="json")
        except Exception:
            raise RuntimeError(
                "Context recovery failed; verify credential and bundle selection"
            ) from None

    @mcp.tool()
    def memory_maintenance_submit(request: MaintenanceRequest) -> MaintenanceReceipt:
        """Explicitly summarize or assess enrichment for selected private context bundles.

        The operator configures model profiles. Workers propose derived knowledge;
        this tool does not publish, purge sources, change retention or grant access.
        Reuse the complete original request and operation UUID unchanged on retries.
        """
        try:
            with Client(url, token) as client:
                return client.submit_maintenance(request)
        except Exception:
            raise RuntimeError(
                "Maintenance submission failed; verify credential and job input"
            ) from None

    @mcp.tool()
    def memory_maintenance_status(job_id: UUID) -> MaintenanceReceipt:
        """Read an authorized job receipt without starting additional model work."""
        try:
            with Client(url, token) as client:
                return client.maintenance_job(job_id)
        except Exception:
            raise RuntimeError(
                "Maintenance inspection failed; verify credential and job selection"
            ) from None

    @mcp.tool()
    def memory_maintenance_wait(
        job_id: UUID,
        timeout_seconds: Annotated[
            float, Field(ge=0, le=60, allow_inf_nan=False, strict=True)
        ] = 30,
    ) -> MaintenanceReceipt:
        """Wait up to 60 seconds for explicit durable work; timeout does not cancel it."""
        try:
            with Client(url, token) as client:
                return client.wait_maintenance(job_id, timeout_seconds=timeout_seconds)
        except Exception:
            raise RuntimeError(
                "Maintenance wait failed; verify credential and job selection"
            ) from None

    @mcp.tool()
    def memory_maintenance_cancel(job_id: UUID) -> MaintenanceReceipt:
        """Explicitly request cancellation. The receipt reports unresolved running effects."""
        try:
            with Client(url, token) as client:
                return client.cancel_maintenance(job_id)
        except Exception:
            raise RuntimeError(
                "Maintenance cancellation failed; verify credential and job selection"
            ) from None

    @mcp.tool()
    def memory_maintenance_artifact(artifact_id: UUID) -> dict[str, Any]:
        """Read a derived artifact only while the caller can still read its sources."""
        try:
            with Client(url, token) as client:
                return client.maintenance_artifact(artifact_id).model_dump(mode="json")
        except Exception:
            raise RuntimeError(
                "Artifact recovery failed; verify credential and source access"
            ) from None

    @mcp.tool()
    def memory_worlds() -> list[World]:
        """List the caller's readable memory worlds. Selection does not enable writes."""
        with Client(url, token) as client:
            return client.worlds()

    @mcp.tool()
    def memory_scopes(world_id: str) -> list[Scope]:
        """List readable scopes. A scope UUID identifies an audience; it does not grant access."""
        with Client(url, token) as client:
            return client.scopes(world_id)

    @mcp.tool()
    def memory_scope_status(world_id: str, scope_id: str | None = None) -> dict[str, Any]:
        """Inspect an authorized scope's source counts and graph projection state."""
        with Client(url, token) as client:
            return client.status(world_id, scope_id=scope_id)

    @mcp.tool()
    def memory_context(context_id: str) -> AgentContext:
        """Inspect this caller's immutable invocation context; credentials are never returned."""
        with Client(url, token) as client:
            return client.get_context(UUID(context_id))

    @mcp.tool()
    def memory_bind_context(
        read_scopes: list[ScopeSelection],
        write_scope: ScopeSelection | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> AgentContext:
        """Bind authorized reads and one write target. This does not grant or delegate access."""
        with Client(url, token) as client:
            return client.bind_context(
                BindContext(
                    read_scopes=read_scopes,
                    write_scope=write_scope,
                    agent_id=agent_id,
                    run_id=run_id,
                )
            )

    @mcp.tool()
    def memory_scope_flow(
        read_scopes: Annotated[list[ScopeSelection], Field(max_length=50)],
        write_scope: ScopeSelection,
    ) -> bool:
        """Check live audience containment for selected reads and one write target.

        This grants no access, records no session exposure and does not authorize
        publication. A user must separately authorize selected shared information.
        """
        try:
            with Client(url, token) as client:
                return client.can_flow(ScopeFlow(read_scopes=read_scopes, write_scope=write_scope))
        except Exception:
            raise RuntimeError(
                "Memory scope flow check failed; verify credential and scope selections"
            ) from None

    @mcp.tool()
    def memory_episodes(
        world_id: str, scope_id: str | None = None, limit: int = 20
    ) -> list[Episode]:
        """List recent source episodes in one authorized scope, including unprojected evidence."""
        with Client(url, token) as client:
            return client.episodes(world_id, scope_id=scope_id, limit=limit)

    @mcp.tool()
    def memory_mounts() -> list[MountView]:
        """List operator-configured remote shared spaces accessible to this agent."""
        with Client(url, token) as client:
            return client.mounts()

    @mcp.tool()
    def memory_remote_search(
        mount_ids: list[str], query: str, graph: bool = True, limit: int = 10
    ) -> dict[str, Any]:
        """Read explicit shared mounts. Report partial failures alongside attributed evidence."""
        with Client(url, token) as client:
            return client.routed_search(
                RouterSearch(
                    mount_ids=mount_ids,
                    query=query,
                    mode="graph" if graph else "source",
                    limit=limit,
                )
            )

    @mcp.tool()
    def memory_deliveries() -> list[Delivery]:
        """Inspect the caller's queued publication receipts without creating publications."""
        with Client(url, token) as client:
            return client.deliveries()

    @mcp.tool()
    def memory_search(
        world_ids: list[str],
        query: str,
        graph: bool = True,
        limit: int = 10,
        scope_id: str | None = None,
        scopes: list[ScopeSelection] | None = None,
        include_entities: bool = False,
    ) -> dict[str, Any]:
        """Search explicitly selected authorized worlds, retaining source attribution."""
        with Client(url, token) as client:
            return client.search(
                SearchRequest(
                    world_ids=world_ids,
                    scope_id=scope_id,
                    scopes=scopes or [],
                    include_entities=include_entities,
                    query=query,
                    limit=limit,
                    mode="graph" if graph else "source",
                )
            )

    @mcp.tool()
    def memory_capture(
        world_id: str,
        source_id: str,
        content: str,
        occurred_at: str,
        project: bool = True,
        scope_id: str | None = None,
        context_id: str | None = None,
    ) -> Receipt:
        """Capture private evidence. Reuse source_id and timestamp unchanged on retries."""
        with Client(url, token) as client:
            receipt = client.capture(
                world_id,
                EpisodeInput(
                    scope_id=scope_id,
                    context_id=context_id,
                    source_id=source_id,
                    content=content,
                    occurred_at=occurred_at,
                    project=project,
                ),
            )
            return receipt

    @mcp.tool()
    def memory_publish(
        world_id: str,
        source_id: str,
        content: str,
        occurred_at: str,
        project: bool = True,
        scope_id: str | None = None,
        context_id: str | None = None,
    ) -> Receipt:
        """Explicitly publish selected content to a shared world. Never copy a whole transcript."""
        with Client(url, token) as client:
            return client.publish(
                world_id,
                Publication(
                    source_id=source_id,
                    content=content,
                    occurred_at=occurred_at,
                    project=project,
                    context_id=context_id,
                ),
                scope_id=scope_id,
            )

    return mcp


def run_mcp(url: str, token: str):
    create_mcp(url, token).run(transport="stdio")
