"""Projection state-machine tests; injected adapters do not claim model/backend proof."""

import asyncio
import os
from datetime import UTC, datetime

import pytest

from memory_worlds.errors import Unavailable
from memory_worlds.graph import (
    GraphConfig,
    GraphitiAdapter,
    ProjectionWorker,
    graph_episode_id,
    graph_namespace,
)
from memory_worlds.models import (
    CreateScope,
    CreateWorld,
    EpisodeInput,
    Publication,
    ScopeKind,
    SearchRequest,
)
from memory_worlds.store import MemoryStore


@pytest.fixture
def local(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap()
    return store, world, store.authenticate(token)


def capture(store, world, principal, source_id="entry-1", project=True):
    return store.capture(
        principal,
        world.id,
        EpisodeInput(
            source_id=source_id,
            content=f"Evidence for {source_id}",
            occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
            project=project,
        ),
    ).episode


def test_normal_capture_queues_graphiti_and_source_only_is_explicit(local):
    store, world, principal = local
    receipt = store.capture(
        principal,
        world.id,
        EpisodeInput(
            source_id="default-graph-memory",
            content="Ada operates the lathe",
            occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
        ),
    )
    assert receipt.episode.projection_state == "pending"
    assert store.pending_jobs(world.id) == [receipt.episode]
    assert SearchRequest(world_ids=[world.id], query="Ada").mode == "graph"
    assert Publication(
        source_id="published",
        content="Selected fact",
        occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
    ).project
    explicit_source = capture(store, world, principal, "maintenance-source", project=False)
    assert explicit_source.projection_state == "disabled"
    assert store.pending_jobs(world.id) == [receipt.episode]


class RecordingAdapter:
    def __init__(self, callback=None):
        self.deliveries = []
        self.searches = []
        self.callback = callback

    def configuration_metadata(self):
        return {"backend": "test-double", "model": "no-model-used"}

    async def project(self, world, generation, episode):
        self.deliveries.append((world.id, generation, episode.id))
        if self.callback:
            await self.callback(world, generation, episode)

    async def search(self, world, generation, query, limit):
        self.searches.append((world.id, generation, query))
        episode_ids = [entry[2] for entry in self.deliveries if entry[0] == world.id]
        return [
            {
                "record_type": "fact",
                "world_id": world.id,
                "generation": generation,
                "fact": "A synthetic result from an injected adapter",
                "graph_episode_ids": [
                    graph_episode_id(world, generation, ep) for ep in episode_ids
                ],
            }
        ]

    async def close(self):
        pass


def local_graph_environment():
    return {
        "MEMORY_WORLDS_GRAPH_BACKEND": "falkordb",
        "MEMORY_WORLDS_LLM_PROVIDER": "openai-compatible",
        "MEMORY_WORLDS_LLM_MODEL": "explicit-extractor",
        "MEMORY_WORLDS_LLM_BASE_URL": "http://127.0.0.1:9000/v1",
        "MEMORY_WORLDS_EMBEDDING_PROVIDER": "openai-compatible",
        "MEMORY_WORLDS_EMBEDDING_MODEL": "explicit-embedder",
        "MEMORY_WORLDS_EMBEDDING_BASE_URL": "http://127.0.0.1:9001/v1",
    }


def test_graph_requires_explicit_configuration_and_excludes_secrets():
    assert GraphConfig.from_env({"MEMORY_WORLDS_GRAPH_BACKEND": "disabled"}) is None
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        GraphConfig.from_env({"OPENAI_API_KEY": "not-enough-to-configure"})
    implicit_backend = local_graph_environment()
    implicit_backend.pop("MEMORY_WORLDS_GRAPH_BACKEND")
    assert GraphConfig.from_env(implicit_backend).backend == "falkordb"
    with pytest.raises(ValueError, match="GRAPH_BACKEND"):
        GraphConfig.from_env({"MEMORY_WORLDS_GRAPH_BACKEND": ""})
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        GraphConfig.from_env({"MEMORY_WORLDS_GRAPH_BACKEND": "falkordb"})
    env = local_graph_environment()
    env["MEMORY_WORLDS_LLM_API_KEY"] = "private-extraction-key"
    env["MEMORY_WORLDS_EMBEDDING_API_KEY"] = "private-embedding-key"
    env["MEMORY_WORLDS_FALKORDB_PASSWORD"] = "private-database-key"
    config = GraphConfig.from_env(env)
    assert config.llm_model == "explicit-extractor"
    assert all(
        secret not in repr(config)
        for secret in ["private-extraction-key", "private-embedding-key", "private-database-key"]
    )
    assert "private-" not in str(config.metadata())
    env["MEMORY_WORLDS_LLM_BASE_URL"] = "https://secret@example.test/v1?token=secret"
    with pytest.raises(ValueError, match="without secrets"):
        GraphConfig.from_env(env)


def test_server_namespaces_separate_authorities_worlds_and_generations(local):
    _, world, _ = local
    first = graph_namespace(world, "one")
    assert first == graph_namespace(world, "one")
    assert first != graph_namespace(world, "two")
    assert first != graph_namespace(world.model_copy(update={"authority_id": "other"}), "one")
    assert first != graph_namespace(world.model_copy(update={"id": "other"}), "one")
    assert first.startswith("mw_") and first[3:].isalnum()


async def test_empty_scope_graph_search_needs_no_backend_or_source_fallback(local):
    store, world, principal = local
    adapter = RecordingAdapter()
    worker = ProjectionWorker(store, adapter)
    assert await worker.search([world], "Ada") == []
    capture(store, world, principal, "source-maintenance", project=False)
    assert await worker.search([world], "Ada") == []
    assert adapter.searches == []
    capture(store, world, principal, "pending-extraction")
    with pytest.raises(Unavailable):
        await worker.search([world], "Ada")
    assert adapter.searches == []


async def test_capture_receipt_precedes_projection_and_repeated_worker_is_idempotent(local):
    store, world, principal = local
    episode = capture(store, world, principal)
    assert episode.projection_state == "pending"

    async def check_claim(_world, _generation, _episode):
        persisted = store.get_episode(principal, world.id, episode.id)
        assert persisted.projection_state == "processing"

    adapter = RecordingAdapter(check_claim)
    worker = ProjectionWorker(store, adapter)
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert len(adapter.deliveries) == 1
    assert store.get_episode(principal, world.id, episode.id).projection_state == "projected"
    assert store.get_world(principal, world.id).projection_state == "ready"
    assert store.projection_configuration(world.id)["backend"] == "test-double"


async def test_ambiguous_failure_blocks_graph_but_preserves_sources_and_new_capture(local):
    store, world, principal = local
    first = capture(store, world, principal)
    second = capture(store, world, principal, "entry-2")

    async def partial_write(*_):
        raise OSError("the injected backend mutated before connection loss")

    adapter = RecordingAdapter(partial_write)
    worker = ProjectionWorker(store, adapter)
    assert await worker.run_once() == 0
    assert store.get_episode(principal, world.id, first.id).projection_state == "unknown"
    assert store.get_episode(principal, world.id, second.id).projection_state == "pending"
    assert store.get_world(principal, world.id).projection_state == "degraded"
    capture(store, world, principal, "entry-3")
    assert await worker.run_once() == 0
    assert len(adapter.deliveries) == 1
    assert len(store.search(principal, [world.id], "Evidence")) == 3
    with pytest.raises(Unavailable):
        await worker.search([world], "Evidence")
    assert adapter.searches == []


async def test_restart_does_not_blindly_retry_an_interrupted_delivery(local):
    store, world, principal = local
    episode = capture(store, world, principal)
    lease = store.persistence.acquire_lease(store.projection_resource(world.id))
    store.mark_processing(world.id, episode.id, lease=lease)
    adapter = RecordingAdapter()
    restarted = ProjectionWorker(MemoryStore(store.data_dir), adapter)
    assert await restarted.run_once() == 0
    assert store.get_episode(principal, world.id, episode.id).projection_state == "processing"
    store.persistence.release_lease(lease)
    assert await restarted.run_once() == 0
    assert adapter.deliveries == []
    assert store.get_episode(principal, world.id, episode.id).projection_state == "unknown"
    assert store.get_world(principal, world.id).projection_state == "degraded"


async def test_concurrent_worker_ticks_serialize_same_world(local):
    store, world, principal = local
    for number in range(3):
        capture(store, world, principal, f"entry-{number}")
    active = 0
    maximum = 0

    async def check_overlap(*_):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1

    adapter = RecordingAdapter(check_overlap)
    worker = ProjectionWorker(store, adapter)
    counts = await asyncio.gather(worker.run_once(), worker.run_once())
    assert sum(counts) == 3 and maximum == 1
    assert len({delivery[2] for delivery in adapter.deliveries}) == 3


async def test_independent_worlds_can_project_concurrently(local):
    store, first, principal = local
    second = store.create_world(principal, CreateWorld(name="Another private context"))
    capture(store, first, principal)
    capture(store, second, principal)
    entered = set()
    both = asyncio.Event()

    async def barrier(world, *_):
        entered.add(world.id)
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=2)

    adapter = RecordingAdapter(barrier)
    worker = ProjectionWorker(store, adapter)
    assert await worker.run_once() == 2
    assert entered == {first.id, second.id}


async def test_rebuild_isolated_generation_preserves_capture_after_snapshot(local):
    store, world, principal = local
    original = capture(store, world, principal)
    disabled = capture(store, world, principal, "source-only", project=False)
    adapter = RecordingAdapter()
    worker = ProjectionWorker(store, adapter)
    await worker.run_once()
    old_generation = store.get_world(principal, world.id).generation

    async def capture_during_rebuild(*_):
        capture(store, world, principal, "arrived-during-rebuild")

    adapter.callback = capture_during_rebuild
    rebuilt = await worker.rebuild(world)
    assert rebuilt["generation"] != old_generation
    assert rebuilt["episodes"] == 1
    assert store.get_episode(principal, world.id, disabled.id).projection_state == "disabled"
    pending = store.pending_jobs(world.id)
    assert [episode.source_id for episode in pending] == ["arrived-during-rebuild"]
    adapter.callback = None
    assert await worker.run_once() == 1
    assert adapter.deliveries[-1][1] == rebuilt["generation"]
    assert adapter.deliveries[1][2] == original.id
    opted_in = await worker.rebuild(world, include_unprojected=True)
    assert opted_in["episodes"] == 3
    assert disabled.id in {episode.id for episode in store.projection_episodes(world.id)}


async def test_rebuild_failure_does_not_activate_partial_generation(local):
    store, world, principal = local
    capture(store, world, principal)
    adapter = RecordingAdapter()
    worker = ProjectionWorker(store, adapter)
    await worker.run_once()
    old = store.get_world(principal, world.id)

    async def fail_replacement(*_):
        raise ConnectionError("replacement not complete")

    adapter.callback = fail_replacement
    with pytest.raises(ConnectionError):
        await worker.rebuild(old)
    current = store.get_world(principal, world.id)
    assert current.generation == old.generation
    assert current.projection_state == "ready"
    assert adapter.deliveries[-1][1] != current.generation


async def test_configuration_changes_require_rebuild_and_failed_rebuild_preserves_binding(local):
    store, world, principal = local
    capture(store, world, principal)
    adapter = RecordingAdapter()
    worker = ProjectionWorker(store, adapter)
    await worker.run_once()
    old_configuration = store.projection_configuration(world.id)
    next_episode = capture(store, world, principal, "waiting-for-new-model")
    adapter.configuration_metadata = lambda: {"backend": "test-double", "model": "changed"}
    with pytest.raises(Unavailable, match="configuration changed"):
        await worker.search([world], "Evidence")
    assert await worker.run_once() == 0
    assert store.get_episode(principal, world.id, next_episode.id).projection_state == "pending"
    assert store.get_world(principal, world.id).projection_state == "degraded"

    async def fail_new_configuration(*_):
        raise OSError("replacement failed")

    adapter.callback = fail_new_configuration
    with pytest.raises(OSError):
        await worker.rebuild(world)
    assert store.projection_configuration(world.id) == old_configuration
    adapter.callback = None
    await worker.rebuild(world)
    assert store.projection_configuration(world.id)["model"] == "changed"
    assert store.get_world(principal, world.id).projection_state == "ready"


async def test_search_joins_provenance_and_validates_all_worlds_before_query(local):
    store, world, principal = local
    episode = capture(store, world, principal)
    adapter = RecordingAdapter()
    worker = ProjectionWorker(store, adapter)
    await worker.run_once()
    result = await worker.search([world], "Evidence")
    assert result[0]["source_episode_ids"] == [episode.id]
    assert "graph_episode_ids" not in result[0]
    empty = store.create_world(principal, CreateWorld(name="Empty graph"))
    assert len(await worker.search([world, empty], "Evidence")) == 1
    unavailable = store.create_world(principal, CreateWorld(name="Not projected"))
    capture(store, unavailable, principal, "pending-extraction")
    adapter.searches.clear()
    with pytest.raises(Unavailable):
        await worker.search([world, unavailable], "Evidence")
    assert adapter.searches == []


async def test_graph_preserves_tentative_source_qualifications_and_rejects_missing_sources(local):
    store, world, principal = local
    tentative = store.capture(
        principal,
        world.id,
        EpisodeInput(
            source_id="worker-proposal",
            content="The operating limit may be 40",
            occurred_at=datetime(2026, 10, 22, tzinfo=UTC),
            role="assistant",
            metadata={
                "claim_status": "tentative",
                "unresolved_questions": ["Verify the limit"],
                "contradictions": ["A prior source reports 30"],
            },
        ),
    ).episode
    evidence = capture(store, world, principal, "ordinary-evidence")

    class QualifiedAdapter(RecordingAdapter):
        async def search(self, current, generation, query, limit, **options):
            return [
                {
                    "record_type": "fact",
                    "fact": "tentative limit",
                    "graph_episode_ids": [graph_episode_id(current, generation, tentative.id)],
                },
                {
                    "record_type": "fact",
                    "fact": "ordinary evidence",
                    "graph_episode_ids": [graph_episode_id(current, generation, evidence.id)],
                },
                {
                    "record_type": "fact",
                    "fact": "orphaned output",
                    "graph_episode_ids": [graph_episode_id(current, generation, "missing-source")],
                },
                {
                    "record_type": "entity",
                    "name": "limit",
                    "summary": "Possible limit",
                    "graph_episode_ids": [],
                },
            ]

    worker = ProjectionWorker(store, QualifiedAdapter())
    await worker.run_once()
    results = await worker.search([world], "limit", include_entities=True)
    assert len(results) == 3
    assert results[0]["claim_status"] == "tentative"
    assert results[0]["source_qualifications"][0]["unresolved_questions"] == ["Verify the limit"]
    assert results[0]["source_qualifications"][0]["contradictions"] == ["A prior source reports 30"]
    assert results[1]["claim_status"] == "derived-unreviewed"
    assert results[2]["claim_status"] == "tentative"
    assert results[2]["source_episode_ids"] == []


async def test_actual_graphiti_clients_use_explicit_models_and_namespace(local, monkeypatch):
    pytest.importorskip("graphiti_core")
    import importlib

    from graphiti_core.driver.falkordb_driver import FalkorDriver

    _, world, _ = local
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GRAPHITI_TELEMETRY_ENABLED", "false")

    # This wiring test performs no socket operations. Avoid thread wake-up sockets
    # too, so the ordinary suite runs inside a network-disabled sandbox.
    async def immediate_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("memory_worlds.graph.asyncio.to_thread", immediate_thread)
    falkor_module = importlib.import_module("falkordb.asyncio.falkordb")
    monkeypatch.setattr(falkor_module, "Is_Cluster", lambda _connection: False)
    built = []

    async def no_backend_build(driver, delete_existing=False):
        built.append(driver._database)

    monkeypatch.setattr(FalkorDriver, "build_indices_and_constraints", no_backend_build)
    config = GraphConfig.from_env(local_graph_environment())
    adapter = GraphitiAdapter(config)
    client = await adapter._client(world, "generation")
    assert client.driver._database == graph_namespace(world, "generation")
    assert client.llm_client.model == "explicit-extractor"
    assert client.embedder.config.embedding_model == "explicit-embedder"
    assert client.embedder.config.embedding_dim == 1536
    assert str(client.llm_client.client.base_url).startswith("http://127.0.0.1:9000")
    assert str(client.embedder.client.base_url).startswith("http://127.0.0.1:9001")
    assert os.environ["GRAPHITI_TELEMETRY_ENABLED"] == "false"
    assert built
    with pytest.raises(Unavailable, match="external reranking is disabled"):
        await client.cross_encoder.rank("query", ["passage"])
    await adapter.close()


@pytest.mark.skipif(
    os.environ.get("MEMORY_WORLDS_RUN_FALKORDB_TEST") != "1",
    reason="requires an explicitly enabled live FalkorDB test instance",
)
async def test_live_falkordb_ingestion_scope_provenance_and_rebuild(local, monkeypatch):
    """Real graph storage/retrieval; deterministic extraction is not a model-quality test."""
    pytest.importorskip("graphiti_core")
    import importlib

    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient

    class DeterministicLLM(LLMClient):
        async def _generate_response(self, messages, response_model=None, *args, **kwargs):
            text = " ".join(message.content for message in messages)
            marker = next(
                (item for item in ("PRIVATE-COPPER", "SCOPED-ORANGE") if item in text),
                "SHARED-TEAL",
            )
            schema = response_model.__name__
            if schema == "ExtractedEntities":
                return {
                    "extracted_entities": [
                        {"name": "Ada", "entity_type_id": 0},
                        {"name": "Lathe", "entity_type_id": 0},
                    ]
                }
            if schema == "ExtractedEdges":
                return {
                    "edges": [
                        {
                            "source_entity_name": "Ada",
                            "target_entity_name": "Lathe",
                            "relation_type": "OWNS",
                            "fact": f"Ada owns the {marker} lathe.",
                            "valid_at": "2026-10-20T00:00:00Z",
                            "invalid_at": None,
                        }
                    ]
                }
            if schema == "NodeResolutions":
                return {"entity_resolutions": []}
            if schema == "SummarizedEntities":
                return {
                    "summaries": [
                        {"name": "Ada", "summary": f"Ada owns the {marker} lathe."},
                        {"name": "Lathe", "summary": f"The {marker} lathe is owned by Ada."},
                    ]
                }
            if schema == "EdgeDuplicate":
                return {"duplicate_facts": [], "contradicted_facts": []}
            if schema == "EdgeTimestamps":
                return {"valid_at": "2026-10-20T00:00:00Z", "invalid_at": None}
            raise AssertionError(f"Unexpected Graphiti extraction schema: {schema}")

    class DeterministicEmbedder(EmbedderClient):
        def __init__(self, config):
            self.config = config

        async def create(self, input_data):
            return [1.0] + [0.0] * (self.config.embedding_dim - 1)

        async def create_batch(self, input_data_list):
            return [await self.create(text) for text in input_data_list]

    monkeypatch.setenv("GRAPHITI_TELEMETRY_ENABLED", "false")
    llm_module = importlib.import_module("graphiti_core.llm_client.openai_generic_client")
    embedding_module = importlib.import_module("graphiti_core.embedder.openai")
    monkeypatch.setattr(llm_module, "OpenAIGenericClient", DeterministicLLM)
    monkeypatch.setattr(embedding_module, "OpenAIEmbedder", DeterministicEmbedder)
    env = local_graph_environment()
    env["MEMORY_WORLDS_FALKORDB_PORT"] = os.environ.get("MEMORY_WORLDS_TEST_FALKORDB_PORT", "16379")
    env["MEMORY_WORLDS_EMBEDDING_DIMENSIONS"] = "16"
    store, private, principal = local
    other = store.create_world(principal, CreateWorld(name="Separate context"))
    scope = store.create_scope(
        principal,
        private.id,
        CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name="Within-world private"),
    )
    scoped = store.get_world(principal, private.id, scope_id=scope.id)
    receipts = []
    for world, marker in [
        (private, "PRIVATE-COPPER"),
        (other, "SHARED-TEAL"),
        (scoped, "SCOPED-ORANGE"),
    ]:
        receipts.append(
            store.capture(
                principal,
                world.id,
                EpisodeInput(
                    scope_id=world.scope_id,
                    source_id=marker,
                    content=f"Ada owns the {marker} lathe.",
                    occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
                    project=True,
                ),
            ).episode
        )
    adapter = GraphitiAdapter(GraphConfig.from_env(env))
    worker = ProjectionWorker(store, adapter)
    try:
        assert await worker.run_once() == 3
        results = await worker.search([private], "Ada lathe")
        assert results and any("PRIVATE-COPPER" in result["fact"] for result in results)
        assert all("SHARED-TEAL" not in result["fact"] for result in results)
        assert all(result["world_id"] == private.id for result in results)
        assert receipts[0].id in results[0]["source_episode_ids"]
        combined = await worker.search([private, other], "Ada lathe")
        assert {result["world_id"] for result in combined} == {private.id, other.id}
        scoped_results = await worker.search([scoped], "Ada lathe", include_entities=True)
        assert {result["record_type"] for result in scoped_results} == {"entity", "fact"}
        assert all(result["scope_id"] == str(scope.id) for result in scoped_results)
        scoped_text = str(scoped_results)
        assert "SCOPED-ORANGE" in scoped_text
        assert "PRIVATE-COPPER" not in scoped_text and "SHARED-TEAL" not in scoped_text
        shared_entities = await worker.search([private], "Ada lathe", include_entities=True)
        assert "SCOPED-ORANGE" not in str(shared_entities)
        previous = store.get_world(principal, private.id).generation
        rebuilt = await worker.rebuild(private)
        assert rebuilt["generation"] != previous
        after = await worker.search([private], "Ada lathe")
        assert after and receipts[0].id in after[0]["source_episode_ids"]
    finally:
        # Remove only test-created graph namespaces, never an entire server database.
        for namespace, client in adapter._clients.items():
            await client.driver.client.select_graph(namespace).delete()
        await worker.close()
