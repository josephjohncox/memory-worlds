"""Exercise Graphiti's actual embedding client against a single-input HTTP API."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from graphiti_core.embedder.openai import OpenAIEmbedderConfig
from openai import AsyncOpenAI
from pydantic import ValidationError

from memory_worlds.embedding import BatchingOpenAIEmbedder
from memory_worlds.graph import GraphConfig
from memory_worlds.setup_config import SetupConfig, load_config, save_config
from memory_worlds.setup_provision import apply_setup
from memory_worlds.setup_runtime import runtime_environment


@pytest.fixture
def endpoint():
    state = {"requests": [], "max_inputs": 1, "malformed": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/v1/embeddings"
            assert self.headers["Authorization"] == "Bearer synthetic-test-key"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            inputs = body["input"]
            if len(inputs) > state["max_inputs"]:
                status = 400
                response = {"error": {"message": "This endpoint accepts one input per request"}}
            else:
                status = 200
                rows = [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": [float(value.split(":")[-1]), 10.0, 20.0, 30.0],
                    }
                    for index, value in enumerate(inputs)
                ]
                if state["malformed"] == "missing":
                    rows = []
                elif state["malformed"] == "duplicate":
                    rows = rows * 2
                elif state["malformed"] == "short":
                    rows[0]["embedding"] = [0.0]
                response = {
                    "object": "list",
                    "model": body["model"],
                    "data": rows[::-1],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def embedder(url, *, batch_size=1):
    client = AsyncOpenAI(api_key="synthetic-test-key", base_url=url, max_retries=0)
    config = OpenAIEmbedderConfig(embedding_model="synthetic-embedding", embedding_dim=3)
    return BatchingOpenAIEmbedder(config, batch_size=batch_size, client=client)


async def test_default_batching_works_with_a_real_single_input_embedding_endpoint(endpoint):
    url, state = endpoint
    client = embedder(url)
    try:
        result = await client.create_batch(["node:0", "node:1", "node:2", "node:3"])
        assert result == [[float(index), 10.0, 20.0] for index in range(4)]
        assert [body["input"] for body in state["requests"]] == [
            ["node:0"],
            ["node:1"],
            ["node:2"],
            ["node:3"],
        ]
        assert await client.create_batch([]) == []
        assert len(state["requests"]) == 4
    finally:
        await client.client.close()


async def test_configured_batches_preserve_input_order_and_dimensions(endpoint):
    url, state = endpoint
    state["max_inputs"] = 2
    client = embedder(url, batch_size=2)
    try:
        # The endpoint deliberately returns each batch in reverse index order.
        result = await client.create_batch([f"node:{index}" for index in range(5)])
        assert result == [[float(index), 10.0, 20.0] for index in range(5)]
        assert [len(body["input"]) for body in state["requests"]] == [2, 2, 1]
    finally:
        await client.client.close()


@pytest.mark.parametrize("malformed", ["missing", "duplicate", "short"])
async def test_bad_batch_results_cannot_silently_change_vector_alignment(endpoint, malformed):
    url, state = endpoint
    state["malformed"] = malformed
    client = embedder(url)
    try:
        with pytest.raises(ValueError, match="embedding"):
            await client.create_batch(["node:1"])
    finally:
        await client.client.close()


@pytest.mark.parametrize("value", [0, -1, 2049, True, 1.5])
def test_embedding_batch_size_must_be_a_bounded_integer(value):
    with pytest.raises(ValueError, match="batch size"):
        BatchingOpenAIEmbedder(OpenAIEmbedderConfig(), batch_size=value)


def graph_environment():
    return {
        "MEMORY_WORLDS_LLM_PROVIDER": "openai-compatible",
        "MEMORY_WORLDS_LLM_MODEL": "synthetic-extractor",
        "MEMORY_WORLDS_LLM_BASE_URL": "http://127.0.0.1:9000/v1",
        "MEMORY_WORLDS_EMBEDDING_PROVIDER": "openai-compatible",
        "MEMORY_WORLDS_EMBEDDING_MODEL": "synthetic-embedder",
        "MEMORY_WORLDS_EMBEDDING_BASE_URL": "http://127.0.0.1:9001/v1",
    }


def test_batching_configuration_does_not_change_graph_semantics():
    config = GraphConfig.from_env(graph_environment())
    assert config.embedding_batch_size == 1
    changed = GraphConfig.from_env(
        graph_environment() | {"MEMORY_WORLDS_EMBEDDING_BATCH_SIZE": "8"}
    )
    assert changed.embedding_batch_size == 8
    assert (
        changed.metadata()
        == config.metadata()
        == replace(config, embedding_batch_size=2).metadata()
    )
    for invalid in ("0", "2049", "not-a-number"):
        with pytest.raises(ValueError):
            GraphConfig.from_env(
                graph_environment() | {"MEMORY_WORLDS_EMBEDDING_BATCH_SIZE": invalid}
            )


def test_setup_roundtrips_and_exports_embedding_batch_policy(tmp_path):
    provider = {
        "provider": "openai-compatible",
        "model": "synthetic",
        "base_url": "http://127.0.0.1:9000/v1",
    }
    config = SetupConfig(
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        extraction=provider,
        embedding=provider | {"batch_size": 2},
    )
    path = tmp_path / "setup.toml"
    save_config(config, path)
    assert load_config(path).embedding.batch_size == 2
    apply_setup(config)
    assert runtime_environment(config)["MEMORY_WORLDS_EMBEDDING_BATCH_SIZE"] == "2"
    with pytest.raises(ValidationError):
        SetupConfig(extraction=provider | {"batch_size": 2}, embedding=provider)


@pytest.mark.parametrize("batch_size", [0, 2049, True, "1"])
def test_setup_rejects_invalid_embedding_batch_policy(batch_size):
    provider = {
        "provider": "openai-compatible",
        "model": "synthetic",
        "base_url": "http://127.0.0.1:9000/v1",
    }
    with pytest.raises(ValidationError):
        SetupConfig(extraction=provider, embedding=provider | {"batch_size": batch_size})
