"""Offline Neptune adapter contract checks, not AWS integration proof."""

import asyncio
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from memory_worlds.errors import Unavailable
from memory_worlds.graph import GraphConfig, GraphitiAdapter, graph_namespace
from memory_worlds.store import MemoryStore


def environment():
    return {
        "MEMORY_WORLDS_GRAPH_BACKEND": "neptune",
        "MEMORY_WORLDS_NEPTUNE_HOST": "neptune-db://graph.example.test",
        "MEMORY_WORLDS_AOSS_HOST": "search.example.test",
        "MEMORY_WORLDS_LLM_PROVIDER": "openai-compatible",
        "MEMORY_WORLDS_LLM_MODEL": "synthetic-extractor",
        "MEMORY_WORLDS_LLM_BASE_URL": "http://localhost:9000/v1",
        "MEMORY_WORLDS_EMBEDDING_PROVIDER": "openai-compatible",
        "MEMORY_WORLDS_EMBEDDING_MODEL": "synthetic-embedder",
        "MEMORY_WORLDS_EMBEDDING_BASE_URL": "http://localhost:9001/v1",
    }


def test_neptune_configuration_is_explicit_and_never_exports_aws_credentials():
    env = environment()
    env.update(AWS_ACCESS_KEY_ID="private-access", AWS_SECRET_ACCESS_KEY="private-secret")
    config = GraphConfig.from_env(env)
    assert config.backend == "neptune"
    assert config.neptune_port == 8182
    assert config.aoss_port == 443
    assert "private-" not in str(config.metadata())
    assert "private-" not in repr(config)
    env["MEMORY_WORLDS_NEPTUNE_HOST"] = "neptune-graph://synthetic-id"
    assert GraphConfig.from_env(env).neptune_host == "neptune-graph://synthetic-id"
    for name, value in (
        ("NEPTUNE_HOST", "https://graph.example.test"),
        ("NEPTUNE_HOST", "neptune-db://user:secret@graph.example.test"),
        ("NEPTUNE_HOST", "neptune-db://graph.example.test/?token=secret"),
        ("AOSS_HOST", "https://search.example.test/path"),
        ("AOSS_HOST", "search.example.test:443"),
        ("NEPTUNE_PORT", "0"),
    ):
        invalid = env | {"MEMORY_WORLDS_" + name: value}
        with pytest.raises(ValueError):
            GraphConfig.from_env(invalid)
    del env["MEMORY_WORLDS_AOSS_HOST"]
    with pytest.raises(ValueError, match="AOSS_HOST"):
        GraphConfig.from_env(env)


async def test_neptune_driver_wiring_and_namespace_guard(tmp_path, monkeypatch, caplog):
    graphiti = pytest.importorskip("graphiti_core")
    store = MemoryStore(tmp_path / "source")
    world, _ = store.bootstrap()
    kwargs = []
    clients = []
    main_thread = threading.get_ident()

    class Driver:
        def __init__(self, **parameters):
            kwargs.append(parameters)
            assert threading.get_ident() != main_thread
            self.execute_query = AsyncMock(return_value=([{"count": 1}], None, None))
            self.aoss_client = SimpleNamespace(close=Mock())
            self.client = SimpleNamespace(query=Mock(side_effect=RuntimeError("synthetic failure")))

        def _sanitize_parameters(self, query, params):
            return query

    class Client:
        def __init__(self, **parameters):
            self.driver = parameters["graph_driver"]
            self.build_indices_and_constraints = AsyncMock()
            self.close = AsyncMock()
            self.search = AsyncMock(return_value=[])
            clients.append(self)

    fake_module = ModuleType("graphiti_core.driver.neptune_driver")
    fake_module.NeptuneDriver = Driver
    monkeypatch.setitem(sys.modules, fake_module.__name__, fake_module)
    monkeypatch.setattr(graphiti, "Graphiti", Client)
    adapter = GraphitiAdapter(GraphConfig.from_env(environment()))
    try:
        assert await adapter.search(world, "generation-one", "fixture", 3) == []
        assert kwargs == [
            {
                "host": "neptune-db://graph.example.test",
                "aoss_host": "search.example.test",
                "port": 8182,
                "aoss_port": 443,
            }
        ]
        namespace = graph_namespace(world, "generation-one")
        clients[0].driver.execute_query.assert_awaited_with(
            "MATCH (n:Episodic {group_id: $group_id}) RETURN count(n) AS count",
            group_id=namespace,
        )
        clients[0].search.assert_awaited_with(query="fixture", group_ids=[namespace], num_results=3)
        clients[0].driver.execute_query.return_value = ([{"count": 0}], None, None)
        with pytest.raises(Unavailable, match="namespace is missing"):
            await adapter.search(world, "generation-one", "fixture", 3)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            clients[0].driver._run_query("synthetic query", {"content": "private fixture"})
        assert "private fixture" not in caplog.text
    finally:
        await adapter.close()
    clients[0].close.assert_awaited_once()
    clients[0].driver.aoss_client.close.assert_called_once()
    assert adapter._aws_thread is None


async def test_neptune_blocking_sdk_work_does_not_block_api_loop(monkeypatch):
    adapter = GraphitiAdapter(GraphConfig.from_env(environment()))
    entered, completed = threading.Event(), threading.Event()

    async def blocking_projection(*_args):
        entered.set()
        time.sleep(0.15)  # Simulates Graphiti's synchronous SDK methods.
        completed.set()

    monkeypatch.setattr(adapter, "_project", blocking_projection)
    task = asyncio.create_task(adapter.project(SimpleNamespace(), "generation", None))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.01)
        assert not completed.is_set()
        await task
        assert completed.is_set()
    finally:
        await adapter.close()
