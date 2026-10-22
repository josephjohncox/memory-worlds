"""Recall, capture a private draft, then interrupt before selected publication.

Run through ``mise run langgraph:example -- --help``. Clients and credentials
stay in runtime context. The deliberately selected approval payload is retained
in the LangGraph checkpoint, independently of Memory Worlds retention.
"""

import argparse
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict
from uuid import UUID

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from memory_worlds.client import Client
from memory_worlds.langgraph import MemoryWorldsAdapter, WorkflowMemory
from memory_worlds.langgraph_tracing import invoke_workflow
from memory_worlds.models import ScopeSelection
from memory_worlds.setup_files import ensure_private_dir, private_read


class State(TypedDict, total=False):
    status: str
    matches: int
    draft_episode_id: str
    publication_episode_id: str
    proposal_digest: str


@dataclass(frozen=True)
class Context:
    memory: WorkflowMemory = field(repr=False)
    selected_content: str = field(repr=False)
    destination: ScopeSelection
    query: str = field(repr=False)
    project: bool = True


def checkpoint_path(state_dir: Path) -> Path:
    """Reject unsafe existing SQLite files before the upstream saver opens them."""
    path = state_dir / "checkpoints.sqlite"
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        # Check every path component, file type, owner, mode and link count
        # through a bounded nonblocking descriptor read before SQLite opens it.
        private_read(candidate, maximum=64 * 1024 * 1024, managed=True)
    return path


def build_graph(checkpointer):
    def proposal_digest(context):
        return hashlib.sha256(
            json.dumps(
                {
                    "content": context.selected_content,
                    "destination": context.destination.model_dump(mode="json"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def recall_and_capture(state: State, runtime: Runtime[Context]):
        context = runtime.context
        result = context.memory.search(context.query, mode="source")
        receipt = context.memory.capture(
            "selected-draft",
            context.selected_content,
            metadata={"example": "langgraph-selected-publication"},
            project=context.project,
        )
        return {
            "status": "awaiting-approval",
            "matches": len(result["results"]),
            "draft_episode_id": receipt.episode.id,
            "proposal_digest": proposal_digest(context),
        }

    def approve_selected(state: State, runtime: Runtime[Context]):
        context = runtime.context
        if proposal_digest(context) != state["proposal_digest"]:
            raise ValueError("The selected content or destination changed; start a new execution")
        approved = interrupt(
            {
                "action": "publish_selected",
                "destination": context.destination.model_dump(mode="json"),
                "content": context.selected_content,
            }
        )
        if approved is not True:
            return {"status": "rejected"}
        receipt = context.memory.publish_selected(
            "approved-finding",
            context.selected_content,
            destination=context.destination,
            approved=True,
            project=context.project,
        )
        return {"status": "published", "publication_episode_id": receipt.episode.id}

    builder = StateGraph(State, context_schema=Context)
    builder.add_node("recall_and_capture", recall_and_capture)
    builder.add_node("approve_selected", approve_selected)
    builder.add_edge(START, "recall_and_capture")
    builder.add_edge("recall_and_capture", "approve_selected")
    builder.add_edge("approve_selected", END)
    return builder.compile(checkpointer=checkpointer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--execution-key", required=True)
    parser.add_argument("--agent-id", type=UUID, required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--read-scope-id", type=UUID, action="append", required=True)
    parser.add_argument("--write-scope-id", type=UUID, required=True)
    parser.add_argument("--publish-world-id", required=True)
    parser.add_argument("--publish-scope-id", type=UUID, required=True)
    parser.add_argument("--content-file", type=Path, required=True)
    parser.add_argument("--query", default="project decisions")
    parser.add_argument("--decision", choices=("approve", "reject"))
    parser.add_argument("--no-project", action="store_true", help="Keep this source-only")
    args = parser.parse_args()
    # New SQLite checkpoints and sidecars must be private on every invocation.
    os.umask(0o077)
    state_dir = args.state_dir.expanduser().absolute()
    ensure_private_dir(state_dir)
    checkpoints = checkpoint_path(state_dir)
    token = private_read(args.token_file, maximum=16_384).decode().strip()
    selected_content = private_read(args.content_file, maximum=800_000).decode()
    destination = ScopeSelection(world_id=args.publish_world_id, scope_id=args.publish_scope_id)
    with Client(args.url, token) as client:
        adapter = MemoryWorldsAdapter(client, state_dir / "memory")
        memory = adapter.execution(
            args.execution_key,
            agent_id=args.agent_id,
            read_scopes=[
                ScopeSelection(world_id=args.world_id, scope_id=value)
                for value in args.read_scope_id
            ],
            write_scope=ScopeSelection(world_id=args.world_id, scope_id=args.write_scope_id),
        )
        context = Context(
            memory=memory,
            selected_content=selected_content,
            destination=destination,
            query=args.query,
            project=not args.no_project,
        )
        config = {"configurable": {"thread_id": args.execution_key}}
        with SqliteSaver.from_conn_string(str(checkpoints)) as saver:
            graph = build_graph(saver)
            current = graph.get_state(config)
            if args.decision is not None:
                if not current.next:
                    parser.error("This workflow has no pending approval")
                inputs = Command(resume=args.decision == "approve")
            elif current.values:
                if not current.next:
                    print(json.dumps(current.values))
                    return
                # Re-enter the pending node without changing its saved state.
                inputs = None
            else:
                inputs = {"status": "pending"}
            result = invoke_workflow(graph, inputs, context=context, config=config)
            # Do not dump interrupt bodies or private evidence to the terminal.
            visible = {key: value for key, value in result.items() if key != "__interrupt__"}
            if result.get("__interrupt__"):
                visible["approval_pending"] = True
            print(json.dumps(visible))


if __name__ == "__main__":
    main()
