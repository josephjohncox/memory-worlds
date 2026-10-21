"""Graphiti projection and conservative delivery of durable source episodes.

Only the service resolves worlds and authorizes callers. This module accepts resolved
World objects; it deliberately exposes neither arbitrary Cypher nor graph namespaces.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from .errors import Conflict, Unavailable
from .models import Episode, World
from .persistence import LeaseLost

if TYPE_CHECKING:
    from .store import MemoryStore


GRAPHITI_VERSION = "0.31.2"
PREFIX = "MEMORY_WORLDS_"


@dataclass(frozen=True)
class GraphConfig:
    llm_provider: str
    llm_model: str
    embedding_provider: str
    embedding_model: str
    llm_api_key: str = field(repr=False)
    embedding_api_key: str = field(repr=False)
    llm_base_url: str | None = None
    embedding_base_url: str | None = None
    embedding_dimensions: int = 1536
    host: str = "127.0.0.1"
    port: int = 6379
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    backend: str = "falkordb"
    neptune_host: str | None = None
    neptune_port: int = 8182
    aoss_host: str | None = None
    aoss_port: int = 443

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GraphConfig | None:
        env = os.environ if environ is None else environ
        backend = env.get(PREFIX + "GRAPH_BACKEND", "falkordb").strip()
        if backend == "disabled":
            return None
        if backend not in {"falkordb", "neptune"}:
            raise ValueError("MEMORY_WORLDS_GRAPH_BACKEND must be falkordb, neptune, or disabled")

        def required(name: str) -> str:
            value = env.get(PREFIX + name, "").strip()
            if not value:
                raise ValueError(f"{PREFIX}{name} must be explicitly configured")
            return value

        def provider_settings(kind: str) -> tuple[str, str, str, str | None]:
            provider = required(kind + "_PROVIDER")
            if provider not in {"openai", "openai-compatible"}:
                raise ValueError(f"{PREFIX}{kind}_PROVIDER must be openai or openai-compatible")
            model = required(kind + "_MODEL")
            url = env.get(PREFIX + kind + "_BASE_URL") or None
            if url:
                parsed = urlparse(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.netloc
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                ):
                    raise ValueError(f"{PREFIX}{kind}_BASE_URL must be an HTTP URL without secrets")
            if provider == "openai-compatible" and not url:
                raise ValueError(f"{PREFIX}{kind}_BASE_URL is required for openai-compatible")
            key = env.get(PREFIX + kind + "_API_KEY")
            if not key and provider == "openai":
                key = env.get("OPENAI_API_KEY")
            if not key:
                # Local compatible endpoints may not authenticate, but selecting that
                # behavior is explicit rather than accidentally using cloud defaults.
                if provider == "openai-compatible":
                    key = "local-no-auth"
                else:
                    raise ValueError(f"{PREFIX}{kind}_API_KEY or OPENAI_API_KEY is required")
            return provider, model, key, url

        lp, lm, lk, lu = provider_settings("LLM")
        ep, em, ek, eu = provider_settings("EMBEDDING")
        port = int(env.get(PREFIX + "FALKORDB_PORT", "6379"))
        dimensions = int(env.get(PREFIX + "EMBEDDING_DIMENSIONS", "1536"))
        if not 1 <= port <= 65535 or dimensions <= 0:
            raise ValueError("invalid graph port or embedding dimensions")
        neptune_host = aoss_host = None
        neptune_port = int(env.get(PREFIX + "NEPTUNE_PORT", "8182"))
        aoss_port = int(env.get(PREFIX + "AOSS_PORT", "443"))
        if backend == "neptune":
            neptune_host = required("NEPTUNE_HOST")
            parsed = urlparse(neptune_host)
            if (
                parsed.scheme not in {"neptune-db", "neptune-graph"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.port is not None
            ):
                raise ValueError("NEPTUNE_HOST requires neptune-db://host or neptune-graph://id")
            aoss_host = required("AOSS_HOST")
            if not all(ch.isalnum() or ch in ".-" for ch in aoss_host):
                raise ValueError("AOSS_HOST must be a bare DNS hostname without a scheme or path")
            if not 1 <= neptune_port <= 65535 or not 1 <= aoss_port <= 65535:
                raise ValueError("invalid Neptune or OpenSearch port")
        return cls(
            llm_provider=lp,
            llm_model=lm,
            embedding_provider=ep,
            embedding_model=em,
            llm_api_key=lk,
            embedding_api_key=ek,
            llm_base_url=lu,
            embedding_base_url=eu,
            embedding_dimensions=dimensions,
            host=env.get(PREFIX + "FALKORDB_HOST", "127.0.0.1"),
            port=port,
            username=env.get(PREFIX + "FALKORDB_USERNAME") or None,
            password=env.get(PREFIX + "FALKORDB_PASSWORD") or None,
            backend=backend,
            neptune_host=neptune_host,
            neptune_port=neptune_port,
            aoss_host=aoss_host,
            aoss_port=aoss_port,
        )

    def metadata(self) -> dict[str, Any]:
        """Configuration provenance suitable for source exports; never credentials."""
        metadata = {
            "backend": self.backend,
            "graphiti_version": GRAPHITI_VERSION,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "embedding_dimensions": self.embedding_dimensions,
            "reranking": "rrf",
        }
        if self.backend == "neptune":
            metadata.update(
                neptune_host=self.neptune_host,
                neptune_port=self.neptune_port,
                aoss_host=self.aoss_host,
                aoss_port=self.aoss_port,
            )
        return metadata


def graph_namespace(world: World, generation: str) -> str:
    """Never pass user-controlled names or path-shaped values to a graph driver."""
    identity = "\0".join((world.authority_id, world.id, str(world.scope_id), generation))
    return "mw_" + hashlib.sha256(identity.encode()).hexdigest()


def graph_episode_id(world: World, generation: str, episode_id: str) -> str:
    identity = graph_namespace(world, generation) + "\0" + episode_id
    return hashlib.sha256(identity.encode()).hexdigest()


class ProjectionAdapter(Protocol):
    async def project(self, world: World, generation: str, episode: Episode) -> None: ...

    async def search(
        self,
        world: World,
        generation: str,
        query: str,
        limit: int,
        include_entities: bool = False,
    ) -> list[dict[str, Any]]: ...

    async def purge_generation(self, world: World, generation: str) -> None: ...

    async def close(self) -> None: ...

    def configuration_metadata(self) -> dict[str, Any]: ...


class GraphitiAdapter:
    """Graphiti extraction and retrieval backend, imported when the daemon starts.

    Graphiti's Neptune implementation performs synchronous AWS/OpenSearch calls
    inside async methods. Give its entire client lifecycle one dedicated event
    loop thread so an AWS stall cannot block source capture on the API loop.
    AWS integration remains experimental until exercised against real services.
    """

    def __init__(self, config: GraphConfig):
        if config is None:
            raise ValueError("graph projection is disabled; configure an explicit backend first")
        self.config = config
        self._clients: dict[str, Any] = {}
        self._aws_loop: asyncio.AbstractEventLoop | None = None
        self._aws_thread: threading.Thread | None = None

    async def _aws_call(self, operation):
        if self._aws_loop is None:
            self._aws_loop = asyncio.new_event_loop()
            self._aws_thread = threading.Thread(
                target=self._aws_loop.run_forever, name="memory-worlds-neptune", daemon=True
            )
            self._aws_thread.start()
        future = asyncio.run_coroutine_threadsafe(operation, self._aws_loop)
        return await asyncio.wrap_future(future)

    def configuration_metadata(self) -> dict[str, Any]:
        return self.config.metadata()

    async def _client(self, world: World, generation: str) -> Any:
        namespace = graph_namespace(world, generation)
        if namespace in self._clients:
            return self._clients[namespace]
        os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
        try:
            from graphiti_core import Graphiti
            from graphiti_core.cross_encoder.client import CrossEncoderClient
            from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
            from graphiti_core.llm_client.config import LLMConfig
            from graphiti_core.llm_client.openai_client import OpenAIClient
            from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
        except ImportError as exc:
            raise Unavailable(
                "Graphiti dependencies are unavailable; reinstall Memory Worlds"
            ) from exc

        class NoExternalReranker(CrossEncoderClient):
            async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
                raise Unavailable("external reranking is disabled; use the RRF search recipe")

        c = self.config
        llm_config = LLMConfig(
            api_key=c.llm_api_key,
            model=c.llm_model,
            small_model=c.llm_model,
            base_url=c.llm_base_url,
        )
        llm = (
            OpenAIClient(config=llm_config)
            if c.llm_provider == "openai"
            else OpenAIGenericClient(config=llm_config)
        )
        embedder = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=c.embedding_api_key,
                embedding_model=c.embedding_model,
                embedding_dim=c.embedding_dimensions,
                base_url=c.embedding_base_url,
            )
        )
        if c.backend == "neptune":
            try:
                from graphiti_core.driver.neptune_driver import NeptuneDriver
            except ImportError as exc:
                raise Unavailable(
                    "install memory-worlds[neptune] to use AWS graph services"
                ) from exc

            class PrivateNeptuneDriver(NeptuneDriver):
                def _run_query(self, query, params):
                    # The upstream exception handler logs complete query parameters,
                    # including source text. Preserve execution without that logging.
                    query = str(self._sanitize_parameters(query, params))
                    return self.client.query(query, params=params), None, None

            # Ambient AWS credentials come from the IAM/SDK chain. No static keys
            # are accepted or copied to exportable projection configuration.
            driver = PrivateNeptuneDriver(
                host=c.neptune_host,
                aoss_host=c.aoss_host,
                port=c.neptune_port,
                aoss_port=c.aoss_port,
            )
        else:
            from falkordb.asyncio import FalkorDB
            from graphiti_core.driver.falkordb_driver import FalkorDriver

            # falkordb-py probes INFO synchronously even for its async constructor.
            backend = await asyncio.to_thread(
                FalkorDB,
                host=c.host,
                port=c.port,
                username=c.username,
                password=c.password,
                socket_connect_timeout=3,
                socket_timeout=15,
            )
            driver = FalkorDriver(falkor_db=backend, database=namespace)
        client = Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=NoExternalReranker(),
            max_coroutines=2,
        )
        try:
            if world.generation == generation and world.projection_state == "ready":
                await self._check_namespace(driver, namespace)
            await client.build_indices_and_constraints()
        except BaseException:
            await client.close()
            raise
        self._clients[namespace] = client
        return client

    async def project(self, world: World, generation: str, episode: Episode) -> None:
        if self.config.backend == "neptune":
            return await self._aws_call(self._project(world, generation, episode))
        return await self._project(world, generation, episode)

    async def _project(self, world: World, generation: str, episode: Episode) -> None:
        if episode.world_id != world.id or episode.scope_id != world.scope_id:
            raise Unavailable("projection source does not belong to the selected scope")
        client = await self._client(world, generation)
        from graphiti_core.errors import NodeNotFoundError
        from graphiti_core.nodes import EpisodeType, EpisodicNode

        namespace = graph_namespace(world, generation)
        episode_uuid = graph_episode_id(world, generation, episode.id)
        source_kind = episode.metadata.get("graphiti_source")
        source = {
            "text": EpisodeType.text,
            "message": EpisodeType.message,
            "json": EpisodeType.json,
        }.get(source_kind if isinstance(source_kind, str) else "text", EpisodeType.text)
        supplied_name = episode.metadata.get("name")
        name = (
            supplied_name
            if isinstance(supplied_name, str) and 0 < len(supplied_name) <= 200
            else episode.source_id
        )
        source_description = (
            f"source episode {episode.id}; imported by {episode.actor_id}; "
            f"source type {episode.source_type}; original role {episode.role or 'unspecified'}; "
            "source evidence only; acceptance of claims is not established"
        )
        previous = await client.retrieve_episodes(
            reference_time=episode.occurred_at,
            last_n=3,
            group_ids=[namespace],
        )
        if any(getattr(node, "group_id", None) != namespace for node in previous):
            raise Unavailable("extraction context is outside the selected scope")
        previous_ids = [node.uuid for node in previous if node.uuid != episode_uuid]
        # Graphiti's add_episode(uuid=...) requires a pre-existing episode.
        # Presence does not imply completed projection; the durable worker owns that receipt.
        try:
            await EpisodicNode.get_by_uuid(client.driver, episode_uuid)
        except NodeNotFoundError:
            node = EpisodicNode(
                uuid=episode_uuid,
                name=name,
                group_id=namespace,
                source=source,
                content=episode.content,
                source_description=source_description,
                created_at=episode.received_at,
                valid_at=episode.occurred_at,
            )
            await node.save(client.driver)
        await client.add_episode(
            name=name,
            episode_body=episode.content,
            source_description=source_description,
            reference_time=episode.occurred_at,
            source=source,
            group_id=namespace,
            uuid=episode_uuid,
            previous_episode_uuids=previous_ids,
            update_communities=False,
            custom_extraction_instructions=(
                "Preserve attribution to the source speaker. Assistant statements are proposals "
                "or claims, not accepted decisions. Repetition does not establish acceptance."
            ),
        )

    async def search(
        self,
        world: World,
        generation: str,
        query: str,
        limit: int,
        include_entities: bool = False,
    ) -> list[dict[str, Any]]:
        if self.config.backend == "neptune":
            return await self._aws_call(
                self._search(world, generation, query, limit, include_entities)
            )
        return await self._search(world, generation, query, limit, include_entities)

    async def _check_namespace(self, driver, namespace: str) -> None:
        if self.config.backend == "falkordb":
            exists = namespace in await driver.client.list_graphs()
        else:
            rows, _, _ = await driver.execute_query(
                "MATCH (n:Episodic {group_id: $group_id}) RETURN count(n) AS count",
                group_id=namespace,
            )
            exists = bool(rows and rows[0]["count"])
        if not exists:
            raise Unavailable("graph namespace is missing; rebuild from durable sources")

    async def _search(
        self,
        world: World,
        generation: str,
        query: str,
        limit: int,
        include_entities: bool = False,
    ) -> list[dict[str, Any]]:
        client = await self._client(world, generation)
        namespace = graph_namespace(world, generation)
        await self._check_namespace(client.driver, namespace)
        nodes = []
        if include_entities:
            from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF

            config = COMBINED_HYBRID_SEARCH_RRF.model_copy(
                deep=True, update={"limit": limit, "episode_config": None, "community_config": None}
            )
            result = await client.search_(query=query, config=config, group_ids=[namespace])
            edges, nodes = result.edges, result.nodes
        else:
            edges = await client.search(query=query, group_ids=[namespace], num_results=limit)
        # A driver returning a different audience must never expand a search.
        if any(getattr(item, "group_id", None) != namespace for item in [*edges, *nodes]):
            raise Unavailable("graph returned evidence outside the selected scope")
        provenance = {
            "world_id": world.id,
            "authority_id": world.authority_id,
            "scope_id": str(world.scope_id),
            "generation": generation,
            "projection_status": "ready",
            "claim_status": "derived-unreviewed",
        }
        facts = [
            {
                **provenance,
                "record_type": "fact",
                "fact_id": edge.uuid,
                "fact": edge.fact,
                "graph_episode_ids": list(edge.episodes),
                "valid_at": edge.valid_at,
                "invalid_at": edge.invalid_at,
            }
            for edge in edges
        ]
        entities = [
            {
                **provenance,
                "record_type": "entity",
                "entity_id": node.uuid,
                "name": node.name,
                "summary": node.summary,
                "content": f"{node.name}: {node.summary}",
                # Entity summaries are scoped derived evidence; do not fabricate
                # a particular source episode for a summary of surrounding edges.
                "graph_episode_ids": [],
            }
            for node in nodes
        ]
        return facts + entities

    async def purge_generation(self, world: World, generation: str) -> None:
        """Delete one exact authority/scope namespace after a drained maintenance barrier."""
        namespace = graph_namespace(world, generation)
        if self.config.backend != "falkordb":
            # Graphiti's scoped Neptune clear_data leaves OpenSearch documents.
            # A partial backend deletion must never become an erasure receipt.
            raise Unavailable("Complete graph erasure requires the FalkorDB backend")
        cached = self._clients.pop(namespace, None)
        if cached is not None:
            backend = cached.driver.client
        else:
            from falkordb.asyncio import FalkorDB

            c = self.config
            backend = await asyncio.to_thread(
                FalkorDB,
                host=c.host,
                port=c.port,
                username=c.username,
                password=c.password,
                socket_connect_timeout=3,
                socket_timeout=15,
            )
        try:
            if namespace in await backend.list_graphs():
                await backend.select_graph(namespace).delete()
            if namespace in await backend.list_graphs():
                raise Unavailable("Selected graph namespace was not removed")
        finally:
            if cached is not None:
                await cached.close()
            else:
                await backend.connection.aclose()

    async def delete_generation(self, world: World, generation: str) -> None:
        await self.purge_generation(world, generation)

    async def close(self) -> None:
        if self._aws_loop is not None:
            loop, thread = self._aws_loop, self._aws_thread
            try:
                await self._aws_call(self._close())
            finally:
                loop.call_soon_threadsafe(loop.stop)
                await asyncio.to_thread(thread.join)
                loop.close()
                self._aws_loop = self._aws_thread = None
        else:
            await self._close()

    async def _close(self) -> None:
        clients, self._clients = self._clients, {}
        for client in clients.values():
            try:
                await client.close()
            finally:
                if self.config.backend == "neptune":
                    client.driver.aoss_client.close()


class ProjectionWorker:
    """Sequential per-scope projection; unknown writes are never replayed."""

    def __init__(self, store: MemoryStore, adapter: ProjectionAdapter):
        self.store = store
        self.adapter = adapter
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _scope(world: World) -> dict:
        return {"scope_id": world.scope_id}

    def _lock(self, world: World) -> asyncio.Lock:
        return self._locks.setdefault(f"{world.id}:{world.scope_id}", asyncio.Lock())

    def _world(self, world_id: str, scope_id) -> World:
        return next(
            world
            for world in self.store.projection_worlds()
            if world.id == world_id and world.scope_id == scope_id
        )

    def _check_configuration(self, world: World) -> None:
        if world.generation and (
            self.store.projection_configuration(world.id, **self._scope(world))
            != self.adapter.configuration_metadata()
        ):
            raise Unavailable("graph configuration changed; rebuild the world before graph use")

    async def _process_world(self, world: World) -> int:
        async with self._lock(world):
            try:
                with self.store.persistence.operation_guard():
                    lease = self.store.persistence.acquire_lease(
                        self.store.projection_resource(world.id, world.scope_id)
                    )
                    if lease is None:
                        return 0
                    try:
                        with self.store.persistence.operation_guard():
                            async with self.store.persistence.maintain_lease(lease) as assert_live:
                                self.store.recover_scope_projection(
                                    world.id, world.scope_id, lease=lease
                                )
                                world = self._world(world.id, world.scope_id)
                                if world.projection_state == "degraded":
                                    return 0
                                try:
                                    self._check_configuration(world)
                                except Unavailable:
                                    self.store.set_projection(
                                        world.id,
                                        world.generation,
                                        "degraded",
                                        **self._scope(world),
                                        lease=lease,
                                    )
                                    return 0
                                pending = self.store.pending_jobs(world.id, **self._scope(world))[
                                    :20
                                ]
                                if not pending:
                                    return 0
                                generation = world.generation or uuid4().hex
                                self.store.register_generation(
                                    world.id, generation, **self._scope(world), lease=lease
                                )
                                count = 0
                                for item in pending:
                                    assert_live()
                                    episode = self.store.mark_processing(
                                        world.id, item.id, **self._scope(world), lease=lease
                                    )
                                    if episode is None:
                                        continue
                                    try:
                                        if not world.generation:
                                            self.store.set_projection_configuration(
                                                world.id,
                                                self.adapter.configuration_metadata(),
                                                **self._scope(world),
                                                lease=lease,
                                            )
                                        self.store.assert_projection_live(
                                            world.id, world.scope_id, lease=lease
                                        )
                                        await self.adapter.project(world, generation, episode)
                                        assert_live()
                                        self.store.mark_projected(
                                            world.id,
                                            episode.id,
                                            generation,
                                            **self._scope(world),
                                            lease=lease,
                                        )
                                        count += 1
                                    except BaseException as error:
                                        # No remote failure proves that Graphiti did not write.
                                        # A lost fence cannot alter the new owner's source state.
                                        try:
                                            self.store.mark_unknown(
                                                world.id,
                                                episode.id,
                                                type(error).__name__,
                                                **self._scope(world),
                                                lease=lease,
                                            )
                                            self.store.fail_generation(
                                                world.id,
                                                generation,
                                                **self._scope(world),
                                                lease=lease,
                                            )
                                        except LeaseLost:
                                            pass
                                        if isinstance(
                                            error,
                                            (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                                        ):
                                            raise
                                        return count
                                return count
                    except (LeaseLost, Unavailable):
                        return 0
                    finally:
                        self.store.persistence.release_lease(lease)

            except Unavailable:
                return 0

    async def run_once(self) -> int:
        semaphore = asyncio.Semaphore(4)

        async def process(world: World) -> int:
            async with semaphore:
                return await self._process_world(world)

        return sum(await asyncio.gather(*(process(w) for w in self.store.projection_worlds())))

    async def run(self, poll_interval: float = 1.0) -> None:
        while True:
            try:
                await self.run_once()
            except Unavailable:
                # Maintenance is a durable pause, not a worker crash or source loss.
                pass
            await asyncio.sleep(poll_interval)

    async def rebuild(self, world: World, include_unprojected: bool = False) -> dict[str, Any]:
        async with self._lock(world):
            with self.store.persistence.operation_guard():
                lease = self.store.persistence.acquire_lease(
                    self.store.projection_resource(world.id, world.scope_id)
                )
                if lease is None:
                    raise Conflict("Another worker owns the selected scope projection")
                generation = uuid4().hex
                registered = False
                try:
                    with self.store.persistence.operation_guard():
                        async with self.store.persistence.maintain_lease(lease) as assert_live:
                            self.store.recover_scope_projection(
                                world.id, world.scope_id, lease=lease
                            )
                            episodes = self.store.projection_episodes(
                                world.id, include_unprojected, **self._scope(world)
                            )
                            self.store.register_generation(
                                world.id, generation, **self._scope(world), lease=lease
                            )
                            registered = True
                            self.store.assert_projection_live(world.id, world.scope_id, lease=lease)
                            for episode in episodes:
                                assert_live()
                                self.store.assert_projection_live(
                                    world.id, world.scope_id, lease=lease
                                )
                                await self.adapter.project(world, generation, episode)
                            assert_live()
                            self.store.activate_generation(
                                world.id,
                                generation,
                                [episode.id for episode in episodes],
                                configuration=self.adapter.configuration_metadata(),
                                **self._scope(world),
                                lease=lease,
                            )
                            return {
                                "world_id": world.id,
                                "scope_id": str(world.scope_id),
                                "generation": generation,
                                "episodes": len(episodes),
                                "source_sequence": max((ep.sequence for ep in episodes), default=0),
                            }
                except BaseException:
                    if registered:
                        try:
                            self.store.fail_generation(
                                world.id, generation, **self._scope(world), lease=lease
                            )
                        except LeaseLost:
                            pass
                    raise
                finally:
                    self.store.persistence.release_lease(lease)

    async def search(
        self,
        worlds: list[World],
        query: str,
        limit: int = 10,
        include_entities: bool = False,
    ) -> list[dict[str, Any]]:
        with self.store.persistence.operation_guard():
            # The caller authorizes the complete selection before invoking this method.
            # Validate every projection before querying any backend.
            resolved = [self._world(world.id, world.scope_id) for world in worlds]
            # A scope with no extraction-eligible evidence has an empty graph. This
            # does not query source text or conceal pending/unknown graph delivery.
            resolved = [
                world
                for world in resolved
                if not (
                    world.projection_state == "disabled"
                    and world.generation is None
                    and not self.store.projection_episodes(world.id, **self._scope(world))
                )
            ]
            if any(w.projection_state != "ready" or not w.generation for w in resolved):
                raise Unavailable(
                    "selected graph projection is unavailable; source search remains usable"
                )
            for world in resolved:
                self._check_configuration(world)
            results: list[dict[str, Any]] = []
            for world in resolved:
                async with self._lock(world):
                    lease = self.store.persistence.acquire_lease(
                        self.store.projection_resource(world.id, world.scope_id)
                    )
                    if lease is None:
                        raise Unavailable("Selected graph projection is being updated")
                    try:
                        async with self.store.persistence.maintain_lease(lease) as assert_live:
                            self.store.recover_scope_projection(
                                world.id, world.scope_id, lease=lease
                            )
                            current = self._world(world.id, world.scope_id)
                            if current.projection_state != "ready" or not current.generation:
                                raise Unavailable("selected graph projection is unavailable")
                            self._check_configuration(current)
                            projected_sources = self.store.projection_episodes(
                                current.id, **self._scope(current)
                            )
                            if not projected_sources:
                                continue
                            try:
                                options = {"include_entities": True} if include_entities else {}
                                facts = await self.adapter.search(
                                    current, current.generation, query, limit, **options
                                )
                            except Exception as exc:
                                raise Unavailable(
                                    "graph retrieval failed; source search remains usable"
                                ) from exc
                            # Join graph provenance to source IDs without a raw UUID lookup.
                            source_ids = {
                                graph_episode_id(
                                    current, current.generation, episode.id
                                ): episode.id
                                for episode in projected_sources
                            }
                            for fact in facts:
                                fact.update(
                                    world_id=current.id,
                                    authority_id=current.authority_id,
                                    scope_id=str(current.scope_id),
                                    generation=current.generation,
                                )
                                graph_ids = fact.pop("graph_episode_ids", [])
                                fact["source_episode_ids"] = [
                                    source_ids[graph_id]
                                    for graph_id in graph_ids
                                    if graph_id in source_ids
                                ]
                            results.extend(facts)
                            assert_live()
                    finally:
                        self.store.persistence.release_lease(lease)
            # Scores from independent worlds are not calibrated. Preserve per-world rank.
            return results

    async def close(self) -> None:
        await self.adapter.close()
