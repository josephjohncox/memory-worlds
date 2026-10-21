from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_worlds.errors import Conflict, Forbidden, NotFound, Unauthorized, Unavailable
from memory_worlds.errors import MemoryError as InvalidRequest
from memory_worlds.models import CreateToken, CreateWorld, EpisodeInput, Role, WorldKind
from memory_worlds.store import MemoryStore


@pytest.fixture
def setup(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    world, token = store.bootstrap("alice")
    return store, world, store.authenticate(token)


def episode(source_id="one", content="Capacity scheduling evidence", **kwargs):
    return EpisodeInput(
        source_id=source_id,
        content=content,
        occurred_at=datetime(2026, 10, 20, tzinfo=UTC),
        **kwargs,
    )


def client(store, admin, world, user="alice", role=Role.WRITER, agent="agent"):
    token = store.issue_token(
        admin,
        CreateToken(
            user_id=user,
            agent_id=agent,
            grants={world.id: role},
        ),
    )
    return store.authenticate(token)


def test_agents_share_durable_records_with_attribution_and_live_revocation(setup):
    store, world, admin = setup
    writer = client(store, admin, world, agent="writer")
    reader = client(store, admin, world, role=Role.READER, agent="reader")
    receipt = store.capture(writer, world.id, episode())
    assert receipt.episode.actor_id == "alice"
    assert receipt.episode.agent_id == "writer"
    assert store.get_episode(reader, world.id, receipt.episode.id) == receipt.episode
    assert store.search(reader, [world.id], "capacity scheduling") == [receipt.episode]
    with pytest.raises(Forbidden):
        store.capture(reader, world.id, episode("denied"))
    store.revoke_token(admin, reader.credential_id)
    with pytest.raises(Unauthorized):
        store.search(reader, [world.id], "capacity")
    restarted = MemoryStore(store.data_dir)
    assert restarted.get_episode(writer, world.id, receipt.episode.id) == receipt.episode
    with pytest.raises(Conflict):
        restarted.bootstrap("other")


def test_parallel_capture_and_retries_lose_no_sources(setup):
    store, world, admin = setup
    first = client(store, admin, world, agent="first")
    second = client(store, admin, world, agent="second")
    inputs = [episode(str(index)) for index in range(40)]

    def capture(index):
        return store.capture([first, second][index % 2], world.id, inputs[index % 40])

    with ThreadPoolExecutor(max_workers=12) as pool:
        receipts = list(pool.map(capture, range(120)))
    assert sum(not receipt.replayed for receipt in receipts) == 40
    assert len(store.list_episodes(first, world.id)) == 40
    assert {receipt.episode.sequence for receipt in receipts} == set(range(1, 41))
    for source in inputs:
        retries = [r for r in receipts if r.episode.source_id == source.source_id]
        assert len({r.episode.id for r in retries}) == 1
        assert len({r.episode.digest for r in retries}) == 1
    with pytest.raises(Conflict):
        store.capture(first, world.id, episode("1", metadata={"changed": True}))


def test_private_owner_and_complete_search_authorization(setup):
    store, alice, admin = setup
    bob = store.create_world(
        admin,
        CreateWorld(
            name="Bob work",
            kind=WorldKind.PRIVATE_WORK,
            owner_id="bob",
        ),
    )
    with pytest.raises(Forbidden):
        client(store, admin, bob, user="alice")
    alice_agent = client(store, admin, alice)
    bob_agent = client(store, admin, bob, user="bob")
    secret = store.capture(bob_agent, bob.id, episode(content="Private secret"))
    assert store.list_worlds(alice_agent) == [alice]
    for action in (
        lambda: store.get_world(alice_agent, bob.id),
        lambda: store.get_episode(alice_agent, bob.id, secret.episode.id),
        lambda: store.snapshot(alice_agent, bob.id),
        lambda: store.search(alice_agent, [alice.id, bob.id], "secret"),
        lambda: store.capture(alice_agent, bob.id, episode()),
    ):
        with pytest.raises(Forbidden):
            action()
    assert store.search(alice_agent, [], "secret") == []
    forged = alice_agent.model_copy(update={"admin": True})
    with pytest.raises(Unauthorized):
        store.get_world(forged, bob.id)


def test_shared_publication_is_explicit_and_read_only_members_cannot_publish(setup):
    store, private, admin = setup
    team = store.create_world(admin, CreateWorld(name="Research", kind=WorldKind.TEAM))
    writer = client(store, admin, team)
    reader = client(store, admin, team, user="bob", role=Role.READER)
    with pytest.raises(Forbidden):
        store.capture(writer, team.id, episode())
    with pytest.raises(Forbidden):
        store.capture(admin, private.id, episode(), shared=True)
    published = store.capture(writer, team.id, episode(source_type="publication"), shared=True)
    assert store.search(reader, [team.id], "capacity") == [published.episode]
    with pytest.raises(Forbidden):
        store.capture(reader, team.id, episode("blocked"), shared=True)


def test_restore_conflicts_roll_back_entire_batch_and_retries_are_idempotent(setup):
    store, world, admin = setup
    store.capture(admin, world.id, episode("existing"))
    with pytest.raises(Conflict):
        store.restore(
            admin,
            world.id,
            [
                episode("new"),
                episode("existing", content="conflicting body"),
            ],
        )
    assert [e.source_id for e in store.list_episodes(admin, world.id)] == ["existing"]
    assert store.restore(admin, world.id, [episode("new"), episode("existing")]) == {
        "imported": 1,
        "replayed": 1,
    }
    assert store.restore(admin, world.id, [episode("new")]) == {"imported": 0, "replayed": 1}
    agent = client(store, admin, world)
    with pytest.raises(Forbidden):
        store.restore(agent, world.id, [episode("no-admin")])


def test_nonfinite_metadata_is_rejected_without_partial_import(setup):
    store, world, admin = setup
    with pytest.raises(InvalidRequest):
        store.restore(
            admin,
            world.id,
            [
                episode("valid"),
                episode("invalid", metadata={"confidence": float("nan")}),
            ],
        )
    assert store.list_episodes(admin, world.id) == []


def test_projection_ambiguity_is_durable_and_rebuild_preserves_later_captures(setup):
    store, world, admin = setup
    first = store.capture(admin, world.id, episode("one", project=True)).episode
    assert [e.id for e in store.pending_jobs(world.id)] == [first.id]
    store.mark_processing(world.id, first.id)
    restarted = MemoryStore(store.data_dir)
    assert restarted.recover_interrupted_projections() == 1
    assert restarted.get_world(admin, world.id).projection_state == "degraded"
    assert restarted.get_episode(admin, world.id, first.id).projection_state == "unknown"
    later = restarted.capture(admin, world.id, episode("later", project=True)).episode
    with pytest.raises(Unavailable):
        restarted.mark_processing(world.id, later.id)
    restarted.activate_generation(world.id, "generation-two", [first.id])
    assert restarted.get_world(admin, world.id).generation == "generation-two"
    assert restarted.get_episode(admin, world.id, later.id).projection_state == "pending"
    assert restarted.mark_processing(world.id, later.id).id == later.id
    restarted.mark_projected(world.id, later.id, "generation-two")
    assert restarted.pending_jobs(world.id) == []
    assert restarted.capture(admin, world.id, episode("one", project=True)).replayed


def test_explicit_projection_does_not_change_source_identity(setup):
    store, world, admin = setup
    original = episode(project=False)
    captured = store.capture(admin, world.id, original).episode
    assert store.projection_episodes(world.id) == []
    store.activate_generation(world.id, "consented", [captured.id])
    assert [e.id for e in store.projection_episodes(world.id)] == [captured.id]
    assert store.capture(admin, world.id, original).replayed
    store.set_projection_configuration(world.id, {"backend": "fake", "model": "test"})
    snapshot = store.snapshot(admin, world.id)
    assert snapshot["projection_configuration"] == {"backend": "fake", "model": "test"}


def test_storage_permissions_and_paths_are_restricted(setup):
    store, world, admin = setup
    store.capture(admin, world.id, episode())
    for path in store.data_dir.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0, path
    with pytest.raises(NotFound):
        store.get_world(admin, "../../outside")
    assert store.snapshot(admin, world.id)["world"]["authority_id"] == store.authority_id


def test_symlink_storage_is_rejected(tmp_path: Path):
    actual = tmp_path / "actual"
    actual.mkdir()
    symlink = tmp_path / "alias"
    symlink.symlink_to(actual, target_is_directory=True)
    with pytest.raises(Conflict):
        MemoryStore(symlink)
