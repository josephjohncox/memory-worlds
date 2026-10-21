#!/usr/bin/env python3
"""Exercise real Graphiti/FalkorDB with deterministic loopback model HTTP endpoints.

The fixture proves extraction, retrieval and provenance contracts. It makes no
claim about an external model's semantic accuracy. Only freshly created graph
namespaces are removed; the server database is never wiped.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import sys
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from memory_worlds.graph import GRAPHITI_VERSION, GraphConfig, GraphitiAdapter, ProjectionWorker
from memory_worlds.models import CreateScope, EpisodeInput, ScopeKind
from memory_worlds.store import MemoryStore

CORPUS = [
    {
        "id": "aster-cobalt",
        "subject": "Aster",
        "object": "CobaltQueue",
        "relation": "USES",
        "fact": "Aster uses the CobaltQueue.",
        "query": "Aster queue",
        "forbidden": ["JadeQueue"],
    },
    {
        "id": "aster-jade",
        "subject": "Aster",
        "object": "JadeQueue",
        "relation": "USES",
        "fact": "Aster uses the JadeQueue.",
        "query": "Aster queue",
        "forbidden": ["CobaltQueue"],
    },
    {
        "id": "beryl-ivory",
        "subject": "Beryl",
        "object": "IvoryLathe",
        "relation": "OWNS",
        "fact": "Beryl owns the IvoryLathe.",
        "query": "Beryl lathe",
        "forbidden": ["OnyxLathe"],
    },
    {
        "id": "beryl-onyx",
        "subject": "Beryl",
        "object": "OnyxLathe",
        "relation": "OWNS",
        "fact": "Beryl owns the OnyxLathe.",
        "query": "Beryl lathe",
        "forbidden": ["IvoryLathe"],
    },
]


def model_response(schema: str, text: str) -> dict:
    fixture = next((item for item in CORPUS if f"QFIX-{item['id']}" in text), None)
    if fixture is None:
        raise ValueError("No synthetic fixture marker")
    if schema == "ExtractedEntities":
        return {
            "extracted_entities": [
                {"name": fixture[key], "entity_type_id": 0} for key in ("subject", "object")
            ]
        }
    if schema == "ExtractedEdges":
        return {
            "edges": [
                {
                    "source_entity_name": fixture["subject"],
                    "target_entity_name": fixture["object"],
                    "relation_type": fixture["relation"],
                    "fact": fixture["fact"],
                    "valid_at": "2026-10-20T12:00:00Z",
                    "invalid_at": None,
                }
            ]
        }
    if schema == "NodeResolutions":
        return {"entity_resolutions": []}
    if schema == "SummarizedEntities":
        return {
            "summaries": [
                {"name": fixture[key], "summary": fixture["fact"]} for key in ("subject", "object")
            ]
        }
    if schema == "EdgeDuplicate":
        return {"duplicate_facts": [], "contradicted_facts": []}
    if schema == "EdgeTimestamps":
        return {"valid_at": "2026-10-20T12:00:00Z", "invalid_at": None}
    raise ValueError("Unknown Graphiti response schema")


class ModelHandler(BaseHTTPRequestHandler):
    operations = {"chat": 0, "embedding": 0}
    lock = threading.Lock()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_048_576:
                raise ValueError("Bounded request required")
            request = json.loads(self.rfile.read(size))
            if self.path == "/v1/chat/completions":
                with self.lock:
                    self.operations["chat"] += 1
                    if self.operations["chat"] > 100:
                        raise ValueError("Local operation budget exhausted")
                schema = request["response_format"]["json_schema"]["name"]
                content = model_response(
                    schema,
                    " ".join(str(message.get("content", "")) for message in request["messages"]),
                )
                response = {
                    "id": "local-graphiti-fixture",
                    "object": "chat.completion",
                    "created": 1_761_048_000,
                    "model": "local-scripted-extractor",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": json.dumps(content)},
                        }
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            elif self.path == "/v1/embeddings":
                with self.lock:
                    self.operations["embedding"] += 1
                    if self.operations["embedding"] > 200:
                        raise ValueError("Local operation budget exhausted")
                values = (
                    request["input"] if isinstance(request["input"], list) else [request["input"]]
                )
                if len(values) > 1000:
                    raise ValueError("Embedding batch exceeds fixture budget")
                response = {
                    "object": "list",
                    "model": "local-scripted-embedder",
                    "data": [
                        {"object": "embedding", "index": index, "embedding": [1.0] + [0.0] * 15}
                        for index, _ in enumerate(values)
                    ],
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                }
            else:
                raise ValueError("Unknown local endpoint")
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (KeyError, ValueError, TypeError):
            self.send_response(400)
            self.end_headers()


def score_retrieval(fixture: dict, hits: list[dict], scope_id: str, episode_id: str) -> dict:
    text = " ".join(str(hit.get(key, "")) for hit in hits for key in ("fact", "name", "summary"))
    source_ids = {source for hit in hits for source in hit.get("source_episode_ids", [])}
    correct_scope = bool(hits) and all(hit.get("scope_id") == scope_id for hit in hits)
    contains_fact = all(fixture[key].casefold() in text.casefold() for key in ("subject", "object"))
    isolation = all(term.casefold() not in text.casefold() for term in fixture["forbidden"])
    provenance = episode_id in source_ids
    return {
        "id": fixture["id"],
        "hits": len(hits),
        "correct_scope": correct_scope,
        "contains_expected_fact": contains_fact,
        "excludes_other_scope_fact": isolation,
        "source_provenance": provenance,
        "passed": correct_scope and contains_fact and isolation and provenance,
    }


async def exercise(host: str, port: int, password: str | None, endpoint: str) -> dict:
    config = GraphConfig(
        llm_provider="openai-compatible",
        llm_model="local-scripted-extractor",
        embedding_provider="openai-compatible",
        embedding_model="local-scripted-embedder",
        llm_api_key="local-synthetic-fixture",
        embedding_api_key="local-synthetic-fixture",
        llm_base_url=endpoint,
        embedding_base_url=endpoint,
        embedding_dimensions=16,
        host=host,
        port=port,
        password=password,
    )
    with tempfile.TemporaryDirectory(prefix="memory-worlds-graph-qualification-") as directory:
        store = MemoryStore(Path(directory))
        world, token = store.bootstrap("synthetic-graph-operator")
        principal = store.authenticate(token)
        targets, episodes = [], []
        for fixture in CORPUS:
            scope = store.create_scope(
                principal,
                world.id,
                CreateScope(kind=ScopeKind.INVOCATION_PRIVATE, name=fixture["id"]),
            )
            targets.append(store.get_world(principal, world.id, scope_id=scope.id))
            episodes.append(
                store.capture(
                    principal,
                    world.id,
                    EpisodeInput(
                        scope_id=scope.id,
                        source_id=fixture["id"],
                        content=f"QFIX-{fixture['id']}: {fixture['fact']}",
                        occurred_at=datetime(2026, 10, 20, 12, tzinfo=UTC),
                        project=True,
                    ),
                ).episode
            )
        adapter = GraphitiAdapter(config)
        worker = ProjectionWorker(store, adapter)
        try:
            await worker.run_once()
            results = []
            for fixture, target, episode in zip(CORPUS, targets, episodes, strict=True):
                hits = await worker.search([target], fixture["query"], include_entities=True)
                results.append(score_retrieval(fixture, hits, str(target.scope_id), episode.id))
            return {
                "kind": "graphiti-local-model-emulation",
                "passed": all(item["passed"] for item in results),
                "graphiti_version": GRAPHITI_VERSION,
                "cases": results,
                "model_operations": dict(ModelHandler.operations),
                "external_model_calls": 0,
                "dataset_sha256": hashlib.sha256(
                    json.dumps(CORPUS, sort_keys=True).encode()
                ).hexdigest(),
                "limitation": (
                    "Real Graphiti and graph storage; scripted extraction and embeddings. "
                    "External model quality is assumed, not measured."
                ),
            }
        finally:
            # Adapter clients were created solely for this fresh authority's scopes.
            for namespace, client in adapter._clients.items():
                with contextlib.suppress(Exception):
                    await client.driver.client.select_graph(namespace).delete()
            await worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--falkordb-host", default="127.0.0.1")
    parser.add_argument("--falkordb-port", type=int, default=16379)
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not ipaddress.ip_address(args.falkordb_host).is_loopback:
        parser.error("Only an explicit loopback graph test backend is accepted")
    if not 1024 <= args.falkordb_port <= 65535 or not 1 <= args.timeout <= 600:
        parser.error("Invalid local qualification bounds")
    if args.output.exists():
        parser.error("Output already exists")
    password = args.password_file.read_text().strip() if args.password_file else None
    ModelHandler.operations = {"chat": 0, "embedding": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def bounded():
        async with asyncio.timeout(args.timeout):
            return await exercise(
                args.falkordb_host,
                args.falkordb_port,
                password,
                f"http://127.0.0.1:{server.server_address[1]}/v1",
            )

    try:
        report = asyncio.run(bounded())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps(report))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Local graph qualification failed; verify the isolated loopback graph backend.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
