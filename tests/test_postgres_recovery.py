import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.schema import DropSchema

from memory_worlds.errors import Conflict
from memory_worlds.lifecycle import EpisodeLifecycle, LifecycleRequest
from memory_worlds.models import DelegateCredential, EpisodeInput
from memory_worlds.recovery import backup_authority, restore_authority
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore


def test_authority_round_trip_sqlite_postgres_sqlite_keeps_private_tombstones_and_replay(tmp_path):
    filename = os.environ.get("MEMORY_WORLDS_TEST_POSTGRES_DSN_FILE")
    if not filename:
        pytest.skip("requires an isolated PostgreSQL database configured through a private file")
    url = Path(filename).read_text().strip()
    local = MemoryStore(tmp_path / "local")
    world, token = local.bootstrap("alice")
    owner = local.authenticate(token)
    delegated = local.delegate_credential(owner, DelegateCredential(private_world_id=world.id))
    child, private = local.authenticate(delegated["token"]), delegated["private_scope"]
    source = EpisodeInput(
        source_id="private",
        content="private retained evidence",
        occurred_at=datetime.now(UTC),
        scope_id=private.id,
    )
    retained = local.capture(child, world.id, source).episode
    erased_input = source.model_copy(
        update={"source_id": "erased", "content": "erased plaintext", "scope_id": None}
    )
    erased = local.capture(owner, world.id, erased_input).episode
    EpisodeLifecycle(local).apply(
        owner,
        world.id,
        world.scope_id,
        LifecycleRequest(operation_id=uuid4(), action="erase", episode_ids=[erased.id]),
    )
    key, archive = tmp_path / "key", tmp_path / "sqlite.mwauthority"
    generate_key(key)
    backup_authority(local, owner, archive, key)
    restore_authority(archive, key, tmp_path / "pg", database_url=url)
    shared = MemoryStore(tmp_path / "pg", url, authority_id=local.authority_id)
    try:
        assert shared.authenticate(token) == owner
        assert (
            shared.get_episode(child, world.id, retained.id, scope_id=private.id).content
            == source.content
        )
        assert shared.capture(child, world.id, source).replayed
        with pytest.raises(Conflict):
            shared.capture(owner, world.id, erased_input)
        extra = shared.capture(child, world.id, source.model_copy(update={"source_id": "next"}))
        assert extra.episode.sequence > retained.sequence
        pg_backup = tmp_path / "postgres.mwauthority"
        backup_authority(shared, owner, pg_backup, key)
        restore_authority(pg_backup, key, tmp_path / "sqlite-restored")
        restored = MemoryStore(tmp_path / "sqlite-restored")
        assert restored.authority_id == local.authority_id
        assert (
            restored.get_episode(child, world.id, extra.episode.id, scope_id=private.id).content
            == source.content
        )
        with pytest.raises(Conflict):
            restored.capture(owner, world.id, erased_input)
        with pytest.raises(Conflict):
            restore_authority(archive, key, tmp_path / "occupied-pg", database_url=url)
    finally:
        with shared.persistence._engine.begin() as db:
            schemas = {shared.persistence.schema("control"), shared.persistence.schema("outbox")}
            schemas.update(
                shared.persistence.schema("source", str(scope.scope_id))
                for scope in shared.projection_worlds()
            )
            for schema in schemas:
                db.execute(DropSchema(schema, cascade=True, if_exists=True))
        shared.persistence.close()
