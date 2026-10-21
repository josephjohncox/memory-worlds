"""Optional stdio MCP facade. The HTTP daemon remains the only storage owner."""

from typing import Any
from uuid import UUID

from . import __version__
from .client import Client
from .models import (
    AgentContext,
    BindContext,
    Episode,
    EpisodeInput,
    Publication,
    Receipt,
    Scope,
    ScopeSelection,
    SearchRequest,
    World,
)
from .mounts import MountView
from .outbox import Delivery, RouterSearch


def create_mcp(url: str, token: str):
    """Create a native SDK 2 server whose tools delegate to the HTTP authority."""
    try:
        from mcp.server import MCPServer
    except ImportError:
        raise RuntimeError("install memory-worlds[mcp] with MCP SDK 2 for this transport") from None

    mcp = MCPServer("Memory Worlds", version=__version__)

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
