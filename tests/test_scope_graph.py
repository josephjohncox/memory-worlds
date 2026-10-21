"""Audience isolation is enforced before graph extraction, not after retrieval."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memory_worlds.errors import Unavailable
from memory_worlds.graph import GraphitiAdapter, ProjectionWorker, graph_episode_id, graph_namespace
from memory_worlds.models import CreateScope, EpisodeInput, ScopeKind
from memory_worlds.store import MemoryStore


@pytest.fixture
def scoped(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap()
    owner = store.authenticate(token)
    scopes = [
        store.create_scope(
            owner,
            world.id,
            CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name=name),
        )
        for name in ("Researcher", "Reviewer")
    ]
    targets = [world, *(store.get_world(owner, world.id, scope_id=scope.id) for scope in scopes)]
    episodes = []
    for target, marker in zip(
        targets, ("SHARED", "RESEARCH-PRIVATE", "REVIEW-PRIVATE"), strict=True
    ):
        episodes.append(
            store.capture(
                owner,
                world.id,
                EpisodeInput(
                    source_id="same-source-identity",
                    content=f"Ada knows {marker}",
                    occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
                    project=True,
                    scope_id=target.scope_id,
                ),
            ).episode
        )
    return store, owner, targets, episodes


class ScopedAdapter:
    def __init__(self):
        self.projected = {}
        self.fail_scope = None

    def configuration_metadata(self):
        return {"backend": "scoped-test-double"}

    async def project(self, world, generation, episode):
        assert world.scope_id == episode.scope_id
        if self.fail_scope is not None and world.scope_id == self.fail_scope:
            raise ConnectionError("Unknown remote write")
        self.projected.setdefault(graph_namespace(world, generation), []).append(episode)

    async def search(self, world, generation, query, limit, include_entities=False):
        return [
            {
                "record_type": "fact",
                "fact": episode.content,
                "graph_episode_ids": [graph_episode_id(world, generation, episode.id)],
            }
            for episode in self.projected[graph_namespace(world, generation)]
        ][:limit]

    async def close(self):
        pass


async def test_same_world_same_source_identity_extracts_in_separate_namespaces(scoped):
    store, owner, targets, episodes = scoped
    adapter = ScopedAdapter()
    worker = ProjectionWorker(store, adapter)
    assert await worker.run_once() == 3
    assert len(adapter.projected) == 3
    for target, episode in zip(targets, episodes, strict=True):
        result = await worker.search([target], "Ada")
        assert len(result) == 1 and result[0]["fact"] == episode.content
        assert result[0]["source_episode_ids"] == [episode.id]
        assert result[0]["scope_id"] == str(target.scope_id)
    result = await worker.search(targets[1:], "Ada")
    assert {row["scope_id"] for row in result} == {str(t.scope_id) for t in targets[1:]}
    assert all("SHARED" not in row["fact"] for row in result)


def test_default_and_explicit_shared_selection_use_identical_graph_namespace(scoped):
    store, owner, targets, _episodes = scoped
    default = targets[0]
    explicit = store.get_world(owner, default.id, scope_id=default.scope_id)
    assert explicit == default
    assert graph_namespace(default, "generation") == graph_namespace(explicit, "generation")
    assert graph_namespace(default, "generation") != graph_namespace(targets[1], "generation")


async def test_unknown_scope_projection_does_not_degrade_sibling_or_shared(scoped):
    store, owner, targets, _episodes = scoped
    adapter = ScopedAdapter()
    adapter.fail_scope = targets[1].scope_id
    worker = ProjectionWorker(store, adapter)
    assert await worker.run_once() == 2
    assert store.get_world(owner, targets[0].id).projection_state == "ready"
    assert (
        store.get_world(owner, targets[1].id, scope_id=targets[1].scope_id).projection_state
        == "degraded"
    )
    sibling = store.get_world(owner, targets[2].id, scope_id=targets[2].scope_id)
    assert sibling.projection_state == "ready"
    with pytest.raises(Unavailable):
        await worker.search(targets[1:], "Ada")
    adapter.fail_scope = None
    await worker.rebuild(targets[1])
    after = store.get_world(owner, sibling.id, scope_id=sibling.scope_id)
    assert after.generation == sibling.generation
    assert len(await worker.search(targets, "Ada")) == 3


async def test_entity_search_uses_copied_rrf_recipe_and_selected_scope(scoped, monkeypatch):
    pytest.importorskip("graphiti_core")
    from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF

    _store, _owner, targets, _episodes = scoped
    world = targets[1]
    namespace = graph_namespace(world, "generation")
    node = SimpleNamespace(
        uuid="node", name="Ada", summary="Scoped entity summary", group_id=namespace
    )
    edge = SimpleNamespace(
        uuid="edge",
        fact="Scoped fact",
        episodes=[],
        valid_at=None,
        invalid_at=None,
        group_id=namespace,
    )
    client = SimpleNamespace(
        driver=object(), search_=AsyncMock(return_value=SimpleNamespace(nodes=[node], edges=[edge]))
    )
    adapter = object.__new__(GraphitiAdapter)
    adapter._client = AsyncMock(return_value=client)
    adapter._check_namespace = AsyncMock()
    rows = await adapter._search(world, "generation", "Ada", 7, include_entities=True)
    assert {row["record_type"] for row in rows} == {"entity", "fact"}
    assert all(row["scope_id"] == str(world.scope_id) for row in rows)
    arguments = client.search_.call_args.kwargs
    assert arguments["group_ids"] == [namespace]
    assert arguments["config"].limit == 7
    assert arguments["config"] is not COMBINED_HYBRID_SEARCH_RRF
    assert arguments["config"].community_config is None
    assert arguments["config"].episode_config is None
    node.group_id = "different-private-scope"
    with pytest.raises(Unavailable, match="outside the selected scope"):
        await adapter._search(world, "generation", "Ada", 7, include_entities=True)
    del node.group_id
    with pytest.raises(Unavailable, match="outside the selected scope"):
        await adapter._search(world, "generation", "Ada", 7, include_entities=True)


async def test_extraction_cannot_use_sibling_episode_or_previous_context(scoped, monkeypatch):
    pytest.importorskip("graphiti_core")
    from graphiti_core.nodes import EpisodeType, EpisodicNode

    _store, _owner, targets, episodes = scoped
    world, episode = targets[1], episodes[1]
    namespace = graph_namespace(world, "generation")
    client = SimpleNamespace(
        driver=object(),
        add_episode=AsyncMock(),
        retrieve_episodes=AsyncMock(
            return_value=[SimpleNamespace(uuid="prior", group_id=namespace)]
        ),
    )
    adapter = object.__new__(GraphitiAdapter)
    adapter._client = AsyncMock(return_value=client)
    monkeypatch.setattr(EpisodicNode, "get_by_uuid", AsyncMock(return_value=object()))
    episode = episode.model_copy(
        update={"metadata": {"name": "Source display name", "graphiti_source": "message"}}
    )
    await adapter._project(world, "generation", episode)
    assert client.retrieve_episodes.call_args.kwargs["group_ids"] == [namespace]
    added = client.add_episode.call_args.kwargs
    assert added["group_id"] == namespace
    assert added["previous_episode_uuids"] == ["prior"]
    assert added["source"] == EpisodeType.message and added["name"] == "Source display name"
    client.retrieve_episodes.return_value = [SimpleNamespace(uuid="sibling", group_id="other")]
    with pytest.raises(Unavailable, match="extraction context"):
        await adapter._project(world, "generation", episode)
    with pytest.raises(Unavailable, match="source does not belong"):
        await adapter._project(targets[2], "generation", episode)
    assert client.add_episode.await_count == 1
