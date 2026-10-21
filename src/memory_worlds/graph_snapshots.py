"""Exact-key FalkorDB snapshots for isolated operator recovery.

Native dumps retain their original namespace key. They are never renamed or
restored over an existing graph. Other authorities' graph keys are not copied.
"""

import hashlib
import os
import re
from pathlib import Path

from .errors import Conflict, Unavailable
from .errors import MemoryError as InvalidRequest
from .graph import GraphConfig, graph_namespace
from .models import World

MAX_GRAPH_SNAPSHOT_BYTES = 128 * 1024 * 1024


class FalkorSnapshots:
    def __init__(self, config: GraphConfig):
        if config.backend != "falkordb":
            raise InvalidRequest("Native authority snapshots currently require FalkorDB")
        from falkordb import FalkorDB

        self.backend = FalkorDB(
            host=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            socket_connect_timeout=3,
            socket_timeout=120,
        )
        self.connection = self.backend.connection

    def close(self):
        self.connection.close()

    def versions(self) -> dict:
        return {
            "redis": self.connection.info("server")["redis_version"],
            "modules": sorted(
                [
                    {"name": item["name"], "version": item["ver"]}
                    for item in self.connection.module_list()
                ],
                key=lambda item: item["name"],
            ),
        }

    def capture(self, worlds: list[World], generations: list[dict], target: Path) -> dict:
        """Call only while all authority writers are quiesced by maintenance."""
        lookup = {(world.id, str(world.scope_id)): world for world in worlds}
        selected = {
            (item["world_id"], str(item["scope_id"]), item["generation"]) for item in generations
        }
        selected.update(
            (world.id, str(world.scope_id), world.generation)
            for world in worlds
            if world.generation
        )
        graphs, seen = [], set(self.backend.list_graphs())
        folder = target / "graph"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for world_id, scope_id, generation in sorted(selected):
            world = lookup.get((world_id, scope_id))
            if world is None:
                raise InvalidRequest("Graph generation has no matching authority scope")
            key = graph_namespace(world, generation)
            entry = {
                "world_id": world_id,
                "scope_id": scope_id,
                "generation": generation,
                "key": key,
                "present": key in seen,
            }
            if key not in seen:
                if world.generation == generation and world.projection_state == "ready":
                    raise Unavailable("An active graph namespace is missing; repair before backup")
                graphs.append(entry)
                continue
            memory = self.connection.memory_usage(key)
            if memory is None or memory > MAX_GRAPH_SNAPSHOT_BYTES:
                raise InvalidRequest("Graph exceeds the native snapshot memory limit")
            payload = self.connection.dump(key)
            if not isinstance(payload, bytes) or len(payload) > MAX_GRAPH_SNAPSHOT_BYTES:
                raise InvalidRequest("Graph exceeds the native snapshot byte limit")
            member = f"graph/{key}.dump"
            descriptor = os.open(target / member, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            graphs.append(
                {
                    **entry,
                    "member": member,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        return {"backend": "falkordb", "versions": self.versions(), "graphs": graphs}

    def restore(
        self, worlds: list[World], generations: list[dict], metadata: dict, source: Path
    ) -> list[str]:
        """Restore original keys into an empty isolated backend, without replacement."""
        if metadata.get("backend") != "falkordb" or metadata.get("versions") != self.versions():
            raise InvalidRequest("Native graph recovery requires identical backend/module versions")
        # A dedicated empty target avoids namespace collisions and unrelated data.
        if self.backend.list_graphs() or self.connection.dbsize():
            raise Conflict("Native graph recovery requires an empty isolated backend")
        lookup = {(world.id, str(world.scope_id)): world for world in worlds}
        expected = {
            (item["world_id"], str(item["scope_id"]), item["generation"]) for item in generations
        }
        expected.update(
            (world.id, str(world.scope_id), world.generation)
            for world in worlds
            if world.generation
        )
        required = {
            (world.id, str(world.scope_id), world.generation)
            for world in worlds
            if world.generation and world.projection_state == "ready"
        }
        entries = metadata.get("graphs")
        if not isinstance(entries, list):
            raise InvalidRequest("Invalid native graph snapshot inventory")
        prepared = []
        seen, identities, present = set(), set(), set()
        for entry in entries:
            if not isinstance(entry, dict) or type(entry.get("present")) is not bool:
                raise InvalidRequest("Invalid native graph snapshot entry")
            world = lookup.get((entry.get("world_id"), entry.get("scope_id")))
            if world is None or not isinstance(entry.get("generation"), str):
                raise InvalidRequest("Graph snapshot has no matching authority scope")
            key = graph_namespace(world, entry["generation"])
            if entry.get("key") != key or key in seen:
                raise InvalidRequest("Graph snapshot namespace identity mismatch")
            seen.add(key)
            identity = world.id, str(world.scope_id), entry["generation"]
            identities.add(identity)
            if not entry.get("present"):
                continue
            present.add(identity)
            if entry.get("member") != f"graph/{key}.dump":
                raise InvalidRequest("Graph snapshot member identity mismatch")
            path = source / entry["member"]
            if path.stat().st_size > MAX_GRAPH_SNAPSHOT_BYTES:
                raise InvalidRequest("Graph snapshot exceeds its byte limit")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    digest.update(chunk)
            if path.stat().st_size != entry.get("bytes") or (
                digest.hexdigest() != entry.get("sha256")
            ):
                raise InvalidRequest("Graph snapshot checksum mismatch")
            prepared.append((key, path))
        if identities != expected or not required.issubset(present):
            raise InvalidRequest("Native graph recovery omits or changes authority namespaces")
        created = []
        try:
            for key, path in prepared:
                # No REPLACE and no key renaming: FalkorDB embeds its key in the dump.
                # Inventory the attempted key before dispatch: a lost Redis ACK
                # can follow an accepted restore. The empty isolated target
                # contract makes this exact key safe to remove on failure.
                created.append(key)
                self.connection.restore(key, 0, path.read_bytes(), replace=False)
                if key not in self.backend.list_graphs():
                    raise Unavailable("Native graph recovery did not register its namespace")
            return created
        except BaseException:
            self.rollback(created)
            raise

    def rollback(self, keys: list[str]) -> None:
        for key in keys:
            if not re.fullmatch(r"mw_[0-9a-f]{64}", key):
                raise InvalidRequest("Invalid authority graph rollback key")
            if key in self.backend.list_graphs():
                self.backend.select_graph(key).delete()
