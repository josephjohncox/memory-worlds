"""Explicit local integration: two disposable FalkorDB endpoints, no model calls."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_worlds.graph import GraphConfig, graph_namespace
from memory_worlds.graph_snapshots import FalkorSnapshots
from memory_worlds.models import EpisodeInput
from memory_worlds.recovery import backup_authority, restore_authority
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore


def test_exact_native_graph_recovery_preserves_vectors_indexes_edges_and_provenance(tmp_path):
    settings = os.environ.get("MEMORY_WORLDS_TEST_FALKOR_RECOVERY_FILE")
    if not settings:
        pytest.skip("requires two disposable FalkorDB endpoints in a private configuration file")
    endpoints = json.loads(Path(settings).read_text())
    adapters = [
        FalkorSnapshots(
            GraphConfig(
                llm_provider="openai-compatible",
                llm_model="unused",
                llm_api_key="unused",
                embedding_provider="openai-compatible",
                embedding_model="unused",
                embedding_api_key="unused",
                **endpoint,
            )
        )
        for endpoint in endpoints
    ]
    source_graph, target_graph = adapters
    store = MemoryStore(tmp_path / "authority")
    world, token = store.bootstrap()
    owner = store.authenticate(token)
    source = store.capture(
        owner,
        world.id,
        EpisodeInput(
            source_id="native", content="verified capacity finding", occurred_at=datetime.now(UTC)
        ),
    )
    generation = "native-recovery-proof"
    lease = store.persistence.acquire_lease(store.projection_resource(world.id, world.scope_id))
    store.register_generation(world.id, generation, lease=lease)
    key = graph_namespace(world, generation)
    graph = source_graph.backend.select_graph(key)
    try:
        graph.query(
            "CREATE VECTOR INDEX FOR (n:Entity) ON (n.embedding) "
            "OPTIONS {dimension:3, similarityFunction:'cosine'}"
        )
        graph.query("CREATE FULLTEXT INDEX FOR (n:Entity) ON (n.name)")
        graph.query(
            "CREATE (a:Entity {name:'Capacity',embedding:vecf32([1.0,0.0,0.0])}), "
            "(b:Entity {name:'Factory',embedding:vecf32([0.0,1.0,0.0])}), "
            "(a)-[:FINDING {fact:'Capacity increased',episode_id:$episode}]->(b)",
            params={"episode": source.episode.id},
        )
        store.activate_generation(world.id, generation, [source.episode.id], lease=lease)
        encryption_key, archive = tmp_path / "key", tmp_path / "native.mwauthority"
        generate_key(encryption_key)
        manifest = backup_authority(store, owner, archive, encryption_key, graph=source_graph)
        assert manifest.graph_mode == "native"
        receipt = restore_authority(
            archive, encryption_key, tmp_path / "recovered", graph=target_graph
        )
        assert receipt["restored_graphs"] == 1
        recovered = MemoryStore(tmp_path / "recovered")
        assert recovered.get_world(owner, world.id).generation == generation
        assert (
            recovered.get_episode(owner, world.id, source.episode.id).projection_state
            == "projected"
        )
        restored = target_graph.backend.select_graph(key)
        assert restored.query(
            "MATCH (a)-[r:FINDING]->(b) RETURN a.name,r.fact,r.episode_id,b.name"
        ).result_set == [["Capacity", "Capacity increased", source.episode.id, "Factory"]]
        assert restored.query(
            "CALL db.idx.vector.queryNodes('Entity','embedding',1,"
            "vecf32([1.0,0.0,0.0])) YIELD node RETURN node.name"
        ).result_set == [["Capacity"]]
        assert restored.query(
            "CALL db.idx.fulltext.queryNodes('Entity','Capacity') YIELD node RETURN node.name"
        ).result_set == [["Capacity"]]
        assert target_graph.backend.list_graphs() == [key]
    finally:
        for adapter in adapters:
            if key in adapter.backend.list_graphs():
                adapter.backend.select_graph(key).delete()
            adapter.close()
