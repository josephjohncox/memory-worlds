import hashlib
from types import SimpleNamespace

import pytest

from memory_worlds.graph import graph_namespace
from memory_worlds.graph_snapshots import FalkorSnapshots
from memory_worlds.store import MemoryStore


def test_accepted_restore_with_lost_ack_cleans_attempted_exact_key(tmp_path):
    store = MemoryStore(tmp_path / "authority")
    world, _ = store.bootstrap()
    key = graph_namespace(world, "generation")
    member = f"graph/{key}.dump"
    (tmp_path / "graph").mkdir()
    payload = b"authenticated native dump fixture"
    (tmp_path / member).write_bytes(payload)
    existing = set()

    def accepted_then_disconnect(name, _ttl, _payload, *, replace):
        assert name == key and not replace
        existing.add(name)
        raise ConnectionError("lost acknowledgment after backend commit")

    snapshot = object.__new__(FalkorSnapshots)
    snapshot.connection = SimpleNamespace(dbsize=lambda: 0, restore=accepted_then_disconnect)
    snapshot.versions = lambda: {"version": "fixture"}
    snapshot.backend = SimpleNamespace(
        list_graphs=lambda: list(existing),
        select_graph=lambda name: SimpleNamespace(delete=lambda: existing.remove(name)),
    )
    metadata = {
        "backend": "falkordb",
        "versions": snapshot.versions(),
        "graphs": [
            {
                "world_id": world.id,
                "scope_id": str(world.scope_id),
                "generation": "generation",
                "key": key,
                "present": True,
                "member": member,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    with pytest.raises(ConnectionError, match="acknowledgment"):
        snapshot.restore(
            [world],
            [{"world_id": world.id, "scope_id": str(world.scope_id), "generation": "generation"}],
            metadata,
            tmp_path,
        )
    assert not existing


def test_ready_active_namespace_cannot_be_omitted_from_native_recovery(tmp_path):
    store = MemoryStore(tmp_path / "authority")
    world, _ = store.bootstrap()
    world = world.model_copy(update={"generation": "ready", "projection_state": "ready"})
    snapshot = object.__new__(FalkorSnapshots)
    snapshot.connection = SimpleNamespace(dbsize=lambda: 0)
    snapshot.backend = SimpleNamespace(list_graphs=lambda: [])
    snapshot.versions = lambda: {"version": "fixture"}
    from memory_worlds.errors import MemoryError

    with pytest.raises(MemoryError, match="omits"):
        snapshot.restore(
            [world],
            [],
            {"backend": "falkordb", "versions": snapshot.versions(), "graphs": []},
            tmp_path,
        )
