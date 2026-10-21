"""SQLite authority cuts coordinate authenticated writers in distinct processes."""

import json
import os
import selectors
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from memory_worlds import recovery
from memory_worlds.errors import Conflict, Unavailable
from memory_worlds.models import CreateScope
from memory_worlds.recovery_archive import generate_key
from memory_worlds.store import MemoryStore

CHILD = r"""
import json,sys
from datetime import UTC,datetime
from pathlib import Path
from memory_worlds.errors import MemoryError
from memory_worlds.models import EpisodeInput
from memory_worlds.store import MemoryStore
try:
    store=MemoryStore(Path(sys.argv[1]))
    owner=store.authenticate(Path(sys.argv[2]).read_text())
    def capture():
        store.capture(owner,sys.argv[3],EpisodeInput(
            scope_id=sys.argv[4],source_id=sys.argv[5],
            content='Synthetic cross-process source evidence.',
            occurred_at=datetime(2026,10,20,tzinfo=UTC),project=False))
    if sys.argv[6]=='hold':
        with store.persistence.operation_guard():
            print(json.dumps({'operation_held':True}),flush=True)
            assert sys.stdin.readline()=='capture\n'
            capture()
    else:
        capture()
    print(json.dumps({'status':200}),flush=True)
except MemoryError as error:
    print(json.dumps({'status':error.status}),flush=True)
"""


def child_arguments(store, token_file, world, scope, source, action="capture"):
    return [
        sys.executable,
        "-c",
        CHILD,
        str(store.data_dir),
        str(token_file),
        world.id,
        str(scope.id),
        source,
        action,
    ]


def run_child(arguments, tmp_path):
    result = subprocess.run(arguments, cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert not result.stderr
    return json.loads(result.stdout)


def test_cross_process_maintenance_waits_for_existing_work_blocks_new_writes_and_recovers_cut(
    tmp_path,
    monkeypatch,
):
    store = MemoryStore(tmp_path / "authority")
    world, token = store.bootstrap("owner")
    owner = store.authenticate(token)
    scope = store.create_scope(
        owner, world.id, CreateScope(kind="agent-persistent", name="Private process memory")
    )
    token_file = tmp_path / "owner.token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(token)
    archive, key = tmp_path / "cut.mwauthority", tmp_path / "key"
    generate_key(key)
    planned, release = threading.Event(), threading.Event()
    original_write = recovery.write_backup

    def paused_write(*args, **kwargs):
        planned.set()
        assert release.wait(5)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(recovery, "write_backup", paused_write)
    reader = subprocess.Popen(
        child_arguments(store, token_file, world, scope, "in-flight", "hold"),
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(reader.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5)
            assert json.loads(reader.stdout.readline()) == {"operation_held": True}
        with ThreadPoolExecutor(max_workers=1) as executor:
            backup = executor.submit(recovery.backup_authority, store, owner, archive, key)
            # The exclusive cut cannot start while the child retains shared ownership.
            assert not planned.wait(0.1)
            assert not backup.done()
            # An already admitted operation may finish before the maintenance cut.
            reader.stdin.write("capture\n")
            reader.stdin.flush()
            output, diagnostics = reader.communicate(timeout=5)
            assert reader.returncode == 0 and not diagnostics
            assert json.loads(output) == {"status": 200}
            assert planned.wait(5)
            try:
                # Source snapshots are already staged; both other processes and
                # new local requests must be prevented from changing the cut.
                blocked = run_child(
                    child_arguments(store, token_file, world, scope, "must-not-enter-cut"), tmp_path
                )
                assert blocked == {"status": 503}
                with pytest.raises(Unavailable):
                    store.list_episodes(owner, world.id, scope.id)
            finally:
                release.set()
            assert backup.result(timeout=5).graph_mode == "rebuild"
        assert run_child(
            child_arguments(store, token_file, world, scope, "after-cut"), tmp_path
        ) == {"status": 200}
        assert {item.source_id for item in store.list_episodes(owner, world.id, scope.id)} == {
            "in-flight",
            "after-cut",
        }
        target = tmp_path / "recovered"
        recovery.restore_authority(archive, key, target)
        recovered = MemoryStore(target)
        try:
            restored_owner = recovered.authenticate(token)
            records = recovered.list_episodes(restored_owner, world.id, scope.id)
            assert [item.source_id for item in records] == ["in-flight"]
            assert records[0].projection_state == "disabled"
            lock_file = target / ".authority-maintenance.lock"
            assert lock_file.is_file() and lock_file.stat().st_mode & 0o077 == 0
        finally:
            recovered.persistence.close()
    finally:
        release.set()
        if reader.poll() is None:
            reader.kill()
            reader.communicate(timeout=5)
        store.persistence.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_sqlite_authority_maintenance_rejects_nonprivate_lock_objects(tmp_path, kind):
    folder = tmp_path / "authority"
    folder.mkdir(mode=0o700)
    target = tmp_path / "unrelated"
    target.write_bytes(b"unrelated fixture")
    target.chmod(0o600)
    lock = folder / ".authority-maintenance.lock"
    if kind == "symlink":
        lock.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, lock)
    else:
        os.mkfifo(lock, 0o600)
    with pytest.raises(Conflict, match="private regular lock file"):
        MemoryStore(folder)
    assert target.read_bytes() == b"unrelated fixture"
