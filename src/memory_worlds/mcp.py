"""Optional stdio MCP facade. The HTTP daemon remains the only storage owner."""

from typing import Any

from .client import Client
from .models import EpisodeInput, Receipt, SearchRequest, World
from .mounts import MountView
from .outbox import Delivery, RouterSearch


def create_mcp(url: str, token: str):
    """Create a native SDK 2 server whose tools delegate to the HTTP authority."""
    try:
        from mcp.server import MCPServer
    except ImportError:
        raise RuntimeError("install memory-worlds[mcp] with MCP SDK 2 for this transport") from None

    mcp = MCPServer("Memory Worlds", version="0.1.0")

    @mcp.tool()
    def memory_worlds() -> list[World]:
        """List the caller's readable memory worlds. Selection does not enable writes."""
        with Client(url, token) as client:
            return client.worlds()

    @mcp.tool()
    def memory_mounts() -> list[MountView]:
        """List operator-configured remote shared spaces accessible to this agent."""
        with Client(url, token) as client:
            return client.mounts()

    @mcp.tool()
    def memory_remote_search(
        mount_ids: list[str], query: str, graph: bool = False, limit: int = 10
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
        world_ids: list[str], query: str, graph: bool = False, limit: int = 10
    ) -> dict[str, Any]:
        """Search explicitly selected authorized worlds, retaining source attribution."""
        with Client(url, token) as client:
            return client.search(
                SearchRequest(
                    world_ids=world_ids,
                    query=query,
                    limit=limit,
                    mode="graph" if graph else "source",
                )
            )

    @mcp.tool()
    def memory_capture(
        world_id: str, source_id: str, content: str, occurred_at: str, project: bool = False
    ) -> Receipt:
        """Capture private evidence. Reuse source_id and timestamp unchanged on retries."""
        with Client(url, token) as client:
            receipt = client.capture(
                world_id,
                EpisodeInput(
                    source_id=source_id, content=content, occurred_at=occurred_at, project=project
                ),
            )
            return receipt

    @mcp.tool()
    def memory_publish(
        world_id: str, source_id: str, content: str, occurred_at: str, project: bool = False
    ) -> Receipt:
        """Explicitly publish selected content to a shared world. Never copy a whole transcript."""
        with Client(url, token) as client:
            return Receipt.model_validate(
                client.request(
                    "POST",
                    f"/v1/worlds/{world_id}/publications",
                    json={
                        "source_id": source_id,
                        "content": content,
                        "occurred_at": occurred_at,
                        "project": project,
                    },
                )
            )

    return mcp


def run_mcp(url: str, token: str):
    create_mcp(url, token).run(transport="stdio")
