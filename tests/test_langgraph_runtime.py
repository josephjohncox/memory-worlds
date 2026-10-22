"""Exercise LangGraph's real runtime and durable checkpointer with an HTTP authority."""

import json
import os
import runpy
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import httpx
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from memory_worlds.client import Client
from memory_worlds.identity_names import CreateIdentity
from memory_worlds.models import CreateScope, EpisodeInput, ScopeKind, ScopeSelection
from memory_worlds.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def memory_authority(tmp_path):
    data_dir = tmp_path / "authority"
    world, operator = MemoryStore(data_dir).bootstrap("workflow-user")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GRAPHITI_TELEMETRY_ENABLED": "false",
        "MEMORY_WORLDS_GRAPH_BACKEND": "disabled",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "memory_worlds.cli",
            "--data-dir",
            str(data_dir),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(base_url=url, timeout=0.2, trust_env=False) as http:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, "Owned memory authority exited before readiness"
                try:
                    if http.get("/readyz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("Owned memory authority did not become ready")
            team_response = http.post(
                "/v1/worlds",
                headers={"Authorization": f"Bearer {operator}"},
                json={"name": "Workflow team", "kind": "team"},
            )
            assert team_response.status_code == 201, team_response.text
            team = team_response.json()
            response = http.post(
                "/v1/credentials",
                headers={"Authorization": f"Bearer {operator}"},
                json={
                    "user_id": "workflow-user",
                    "agent_id": "langgraph-worker",
                    "grants": {world.id: "manager", team["id"]: "writer"},
                },
            )
            assert response.status_code == 201, response.text
            token = response.json()["token"]
        yield url, token, world, team
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            raise AssertionError("Owned memory authority did not stop") from None


def test_runtime_client_does_not_enter_durable_graph_state(memory_authority, tmp_path):
    url, token, world, _team = memory_authority

    class State(TypedDict):
        worlds: int

    @dataclass
    class Context:
        memory: Client = field(repr=False)

    def discover(state: State, runtime: Runtime[Context]):
        visible = runtime.context.memory.worlds()
        assert world.id in [item.id for item in visible]
        return {"worlds": len(visible)}

    builder = StateGraph(State, context_schema=Context)
    builder.add_node("discover", discover)
    builder.add_edge(START, "discover")
    builder.add_edge("discover", END)
    path = tmp_path / "checkpoints.sqlite"
    config = {"configurable": {"thread_id": "runtime-boundary"}}
    with Client(url, token) as client, SqliteSaver.from_conn_string(str(path)) as saver:
        graph = builder.compile(checkpointer=saver)
        assert graph.invoke({"worlds": 0}, config, context=Context(client)) == {"worlds": 2}
        checkpoint = saver.get_tuple(config)
        encoded = json.dumps(checkpoint.checkpoint)
        assert token not in encoded
        assert url not in encoded
        assert "Authorization" not in encoded
    assert token.encode() not in path.read_bytes()


def _private_agent(client, world):
    agent = client.create_identity("agent", CreateIdentity(id=uuid4(), name="Research worker"))
    scope = client.create_scope(
        world.id,
        CreateScope(
            kind=ScopeKind.AGENT_PERSISTENT,
            name="Research private notes",
            agent_id=agent.id,
        ),
    )
    return agent, ScopeSelection(world_id=world.id, scope_id=scope.id)


def test_graph_lost_ack_restart_exact_replay_then_interrupt_publication(memory_authority, tmp_path):
    from memory_worlds.langgraph import MemoryWorldsAdapter, WorkflowError, WorkflowMemory
    from memory_worlds.langgraph_tracing import invoke_workflow

    url, token, world, team = memory_authority
    source = ScopeSelection(world_id=world.id, scope_id=world.scope_id)
    destination = ScopeSelection(world_id=team["id"], scope_id=team["scope_id"])
    journal = tmp_path / "workflow-journal"
    checkpoints = tmp_path / "workflow-checkpoints.sqlite"
    requests = []
    config = {"configurable": {"thread_id": "durable-research"}}

    class State(TypedDict, total=False):
        matches: int
        draft_episode_id: str
        publication_episode_id: str

    @dataclass(frozen=True)
    class Context:
        memory: WorkflowMemory = field(repr=False)

    def builder(saver):
        def recall(state: State, runtime: Runtime[Context]):
            result = runtime.context.memory.search("source evidence canary", mode="source")
            return {"matches": len(result["results"])}

        def capture(state: State, runtime: Runtime[Context]):
            receipt = runtime.context.memory.capture(
                "private-conclusion",
                "private draft content canary",
                metadata={"bench": "trial-42"},
                project=False,
            )
            return {"draft_episode_id": receipt.episode.id}

        def approve(state: State, runtime: Runtime[Context]):
            approved = interrupt({"action": "publish", "content": "selected finding"})
            receipt = runtime.context.memory.publish_selected(
                "approved-conclusion",
                "selected finding",
                destination=destination,
                approved=approved,
                project=False,
            )
            return {"publication_episode_id": receipt.episode.id}

        graph = StateGraph(State, context_schema=Context)
        graph.add_node("recall", recall)
        graph.add_node("capture", capture)
        graph.add_node("approve", approve)
        graph.add_edge(START, "recall")
        graph.add_edge("recall", "capture")
        graph.add_edge("capture", "approve")
        graph.add_edge("approve", END)
        return graph.compile(checkpointer=saver)

    def record(request):
        if request.method == "POST" and request.url.path.endswith("/episodes"):
            requests.append(json.loads(request.content))

    with Client(url, token) as client:
        agent, private = _private_agent(client, world)
        client.capture(
            world.id,
            EpisodeInput(
                scope_id=world.scope_id,
                source_id="research-baseline",
                content="source evidence canary",
                occurred_at=datetime.now(UTC),
                project=False,
            ),
        )
        first = MemoryWorldsAdapter(client, journal).execution(
            "durable-research", agent_id=agent.id, read_scopes=[source], write_scope=private
        )
        context_id, run_id = first.context.id, first.run_id
        original_capture = client.capture

        def lose_ack(*args, **kwargs):
            original_capture(*args, **kwargs)
            raise httpx.ReadError("synthetic acknowledgement loss")

        client.capture = lose_ack
        client.http.event_hooks["request"] = [record]
        with SqliteSaver.from_conn_string(str(checkpoints)) as saver:
            graph = builder(saver)
            with pytest.raises(WorkflowError, match="Memory workflow operation failed"):
                invoke_workflow(graph, {}, context=Context(first), config=config)
            assert graph.get_state(config).values == {"matches": 1}
    # Reopen both independent durable stores with new runtime/client objects.
    with Client(url, token) as client, SqliteSaver.from_conn_string(str(checkpoints)) as saver:
        resumed = MemoryWorldsAdapter(client, journal).execution(
            "durable-research", agent_id=agent.id, read_scopes=[source], write_scope=private
        )
        assert resumed.context.id == context_id
        assert resumed.run_id == run_id
        client.http.event_hooks["request"] = [record]
        graph = builder(saver)
        pending = invoke_workflow(graph, None, context=Context(resumed), config=config)
        assert pending["__interrupt__"]
        assert len(requests) == 2
        assert requests[0] == requests[1]
        assert requests[0]["context_id"] == str(context_id)
        drafts = client.episodes(world.id, scope_id=private.scope_id)
        assert len(drafts) == 1
        assert drafts[0].agent_uuid == agent.id
        assert client.episodes(team["id"], scope_id=destination.scope_id) == []
        result = invoke_workflow(
            graph, Command(resume=True), context=Context(resumed), config=config
        )
        assert result["draft_episode_id"] == drafts[0].id
        published = client.episodes(team["id"], scope_id=destination.scope_id)
        assert len(published) == 1
        assert published[0].id == result["publication_episode_id"]
        assert published[0].content == "selected finding"
        encoded = json.dumps(saver.get_tuple(config).checkpoint)
        assert token not in encoded
        assert "private draft content canary" not in encoded
        assert "source evidence canary" not in encoded
    assert token.encode() not in checkpoints.read_bytes()


def test_example_checkpoints_across_process_exit_and_binds_approval_to_proposal(
    memory_authority, tmp_path
):
    url, token, world, team = memory_authority
    with Client(url, token) as client:
        agent, private = _private_agent(client, world)
    inputs = tmp_path / "inputs"
    inputs.mkdir(mode=0o700)
    token_file = inputs / "agent.token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    content_file = inputs / "selected.txt"
    content_file.write_text("Approved ceramic bearing finding")
    content_file.chmod(0o600)
    state_dir = tmp_path / "example-state"
    arguments = [
        sys.executable,
        str(ROOT / "examples/langgraph/scoped_workflow.py"),
        "--url",
        url,
        "--token-file",
        str(token_file),
        "--state-dir",
        str(state_dir),
        "--execution-key",
        "process-restart-example",
        "--agent-id",
        str(agent.id),
        "--world-id",
        world.id,
        "--read-scope-id",
        str(world.scope_id),
        "--write-scope-id",
        str(private.scope_id),
        "--publish-world-id",
        team["id"],
        "--publish-scope-id",
        team["scope_id"],
        "--content-file",
        str(content_file),
        "--no-project",
    ]
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GRAPHITI_TELEMETRY_ENABLED": "false",
    }

    def run(*extra):
        return subprocess.run(
            [*arguments, *extra],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    initial = run()
    assert initial.returncode == 0, initial.stderr
    pending = json.loads(initial.stdout)
    assert pending["approval_pending"] is True
    assert pending["status"] == "awaiting-approval"
    assert "Approved ceramic bearing finding" not in initial.stdout
    content_file.write_text("Changed unapproved finding")
    changed = run("--decision", "approve")
    assert changed.returncode != 0
    with Client(url, token) as client:
        assert client.episodes(team["id"], scope_id=team["scope_id"]) == []
    content_file.write_text("Approved ceramic bearing finding")
    approved = run("--decision", "approve")
    assert approved.returncode == 0, approved.stderr
    result = json.loads(approved.stdout)
    assert result["status"] == "published"
    with Client(url, token) as client:
        published = client.episodes(team["id"], scope_id=team["scope_id"])
        assert len(published) == 1
        assert published[0].content == "Approved ceramic bearing finding"
    assert token.encode() not in (state_dir / "checkpoints.sqlite").read_bytes()


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "shared", "fifo"])
def test_example_rejects_unsafe_existing_checkpoints_and_sidecars(
    tmp_path, suffix, unsafe, example_checkpoint_path
):
    from memory_worlds.errors import MemoryError

    state_dir = tmp_path / "workflow"
    state_dir.mkdir(mode=0o700)
    path = state_dir / ("checkpoints.sqlite" + suffix)
    if unsafe in {"symlink", "hardlink"}:
        target = tmp_path / "unrelated-private-file"
        target.write_bytes(b"unrelated private file canary")
        target.chmod(0o600)
        if unsafe == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif unsafe == "shared":
        path.write_bytes(b"unrelated private file canary")
        path.chmod(0o644)
    else:
        os.mkfifo(path, mode=0o600)
    with pytest.raises(MemoryError):
        example_checkpoint_path(state_dir)


def test_example_accepts_private_owned_checkpoint_and_sidecars(tmp_path, example_checkpoint_path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = tmp_path / ("checkpoints.sqlite" + suffix)
        path.write_bytes(b"")
        path.chmod(0o600)
    assert example_checkpoint_path(tmp_path) == tmp_path / "checkpoints.sqlite"


@pytest.fixture(scope="module")
def example_checkpoint_path():
    return runpy.run_path(str(ROOT / "examples/langgraph/scoped_workflow.py"))["checkpoint_path"]
