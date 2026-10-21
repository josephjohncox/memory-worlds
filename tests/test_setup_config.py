"""Setup must be inspectable offline and never turn configuration into code."""

import errno
import hashlib
import json
import os
import stat
import subprocess
import sys

import pytest
from pydantic import ValidationError

from memory_worlds.errors import Conflict, MemoryError
from memory_worlds.setup_cli import plan_setup, validate_setup
from memory_worlds.setup_config import SetupConfig, load_config, save_config
from memory_worlds.setup_files import (
    ensure_private_dir,
    load_state,
    private_read,
    read_json,
    setup_lock,
    unlink_private,
    write_private,
)


def configuration(tmp_path, **updates):
    values = {
        "instance": "test-memory",
        "data_dir": tmp_path / "data",
        "state_dir": tmp_path / "private",
        "extraction": {
            "provider": "openai-compatible",
            "model": "extractor",
            "base_url": "http://127.0.0.1:9999/v1",
        },
        "embedding": {
            "provider": "openai-compatible",
            "model": "embedder",
            "base_url": "http://127.0.0.1:9999/v1",
        },
    }
    return SetupConfig(**(values | updates))


def test_offline_roundtrip_and_plan_do_not_initialize_or_contact_provider(tmp_path, monkeypatch):
    from memory_worlds.store import MemoryStore

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not initialize storage")

    monkeypatch.setattr(MemoryStore, "__init__", forbidden)
    config = configuration(tmp_path)
    location = tmp_path / "config" / "setup.toml"
    save_config(config, location)
    assert load_config(location) == config
    assert load_config(location).digest == config.digest
    assert stat.S_IMODE(location.stat().st_mode) == 0o600
    assert validate_setup(config)["valid"]
    plan = plan_setup(config)
    assert plan["models"]["extraction"]["model"] == "extractor"
    assert plan["local"]["journal"] == "SQLite"
    assert not config.data_dir.exists()
    assert not config.state_dir.exists()


def test_relative_paths_are_resolved_at_config_not_process_directory(tmp_path, monkeypatch):
    folder = tmp_path / "config"
    folder.mkdir()
    path = folder / "setup.toml"
    path.write_text("""data_dir = "../data"
state_dir = "../private"
[extraction]
provider = "openai-compatible"
model = "extractor"
base_url = "http://localhost:9999/v1"
[embedding]
provider = "openai-compatible"
model = "embedder"
base_url = "http://localhost:9999/v1"
""")
    # Parent traversal is deliberately rejected, even in relative TOML paths.
    with pytest.raises(MemoryError, match="traversal"):
        load_config(path)
    path.write_text(path.read_text().replace("../data", "data").replace("../private", "private"))
    monkeypatch.chdir("/")
    config = load_config(path)
    assert config.data_dir == folder / "data"
    assert config.state_dir == folder / "private"


@pytest.mark.parametrize(
    "updates",
    [
        {"owner": "alice@example.com"},
        {"instance": "$(touch unwanted)"},
        {"port": 16379},
        {"mode": "hybrid"},
        {"host": "0.0.0.0"},
        {"embedding_dimensions": 0},
        {"migrate": True},
        {"extraction": {"provider": "openai", "model": "chosen-model"}},
        {"extraction": {"provider": "openai-compatible", "model": "chosen-model"}},
        {
            "extraction": {
                "provider": "openai-compatible",
                "model": "chosen-model",
                "base_url": "http://secret@host/v1",
            }
        },
    ],
)
def test_invalid_configuration_fails_before_provisioning(tmp_path, updates):
    with pytest.raises((ValidationError, MemoryError)):
        configuration(tmp_path, **updates)
    assert not (tmp_path / "data").exists()


def test_toml_errors_never_echo_secret_input(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('api_key = "DO-NOT-PRINT-THIS"\n')
    with pytest.raises(MemoryError) as caught:
        load_config(path)
    assert "DO-NOT-PRINT-THIS" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://corp.example",
        "https://u:p@corp.example",
        "https://corp.example/api",
        "https://corp.example?token=secret",
    ],
)
def test_corporate_endpoint_rejects_unsafe_origin(tmp_path, url):
    with pytest.raises(ValidationError):
        configuration(
            tmp_path, mode="hybrid", corporate={"url": url, "token_file": tmp_path / "token"}
        )


def test_hybrid_empty_allowlist_is_valid_and_no_automatic_publication(tmp_path):
    config = configuration(
        tmp_path,
        mode="hybrid",
        corporate={
            "url": "https://memory.example",
            "issuer": "https://id.example/oauth2/default",
            "client_id": "native-client",
            "world_ids": [],
        },
    )
    plan = plan_setup(config)
    assert plan["corporate"]["selected_world_ids"] == []
    assert plan["corporate"]["publication"] is False


def test_private_files_do_not_follow_links_or_overwrite_foreign_content(tmp_path):
    folder = tmp_path / "private"
    ensure_private_dir(folder)
    target = folder / "secret"
    digest = write_private(target, "secret-one\n")
    assert digest == hashlib.sha256(b"secret-one\n").hexdigest()
    with pytest.raises(Conflict, match="unowned"):
        write_private(target, "secret-two\n")
    target.write_text("outside-edit\n")
    with pytest.raises(Conflict, match="changed"):
        write_private(target, "secret-two\n", expected_hash=digest)
    assert target.read_text() == "outside-edit\n"
    link = folder / "link"
    link.symlink_to(target)
    with pytest.raises(MemoryError, match="symbolic"):
        private_read(link)
    parent = tmp_path / "parent-link"
    parent.symlink_to(folder, target_is_directory=True)
    with pytest.raises(MemoryError, match="symbolic"):
        write_private(parent / "new", "secret")


def test_private_read_bounds_and_does_not_block_on_fifo(tmp_path):
    target = tmp_path / "input"
    target.write_bytes(b"x" * 20)
    target.chmod(0o600)
    with pytest.raises(MemoryError, match="bounded"):
        private_read(target, maximum=10)
    target.chmod(0o644)
    with pytest.raises(MemoryError, match="0600"):
        private_read(target)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(MemoryError, match="bounded"):
        private_read(fifo)


@pytest.mark.parametrize("operation", ["read", "read_json", "replace", "unchanged", "unlink"])
def test_managed_files_reject_hard_links_without_changing_either_alias(tmp_path, operation):
    folder = tmp_path / "private"
    target, alias = folder / "managed.token", tmp_path / "retained-token"
    original = b'{"secret":"original"}\n'
    digest = write_private(target, original)
    os.link(target, alias)
    before = target.stat()
    with pytest.raises(MemoryError, match="hard link"):
        if operation == "read":
            private_read(target, managed=True)
        elif operation == "read_json":
            read_json(target)
        elif operation == "unlink":
            unlink_private(target, expected_hash=digest)
        else:
            write_private(
                target,
                original if operation == "unchanged" else b"rotated-token\n",
                expected_hash=digest,
            )
    assert target.read_bytes() == alias.read_bytes() == original
    assert target.stat().st_ino == alias.stat().st_ino == before.st_ino
    assert target.stat().st_nlink == 2


def test_external_private_references_can_have_hard_links(tmp_path):
    target, alias = tmp_path / "provider.key", tmp_path / "provider-key-alias"
    target.write_bytes(b"external-provider-key\n")
    target.chmod(0o600)
    os.link(target, alias)
    assert private_read(alias) == b"external-provider-key\n"


def test_new_private_file_publication_refuses_destination_that_appears(tmp_path, monkeypatch):
    import memory_worlds.setup_files as files

    folder = tmp_path / "private"
    target = folder / "managed.token"
    rename = files._rename_new

    def raced(directory, source, destination):
        target.write_bytes(b"foreign content\n")
        target.chmod(0o600)
        rename(directory, source, destination)

    monkeypatch.setattr(files, "_rename_new", raced)
    with pytest.raises(Conflict, match="appeared"):
        write_private(target, "managed secret\n")
    assert target.read_bytes() == b"foreign content\n"
    assert list(folder.iterdir()) == [target]


def test_process_exit_immediately_after_publication_leaves_one_managed_link(tmp_path):
    folder = tmp_path / "private"
    script = """
import os
import sys
from pathlib import Path
import memory_worlds.setup_files as files
publish = files._rename_new
def exit_after_publication(*args):
    publish(*args)
    os._exit(29)
files._rename_new = exit_after_publication
files.write_private(Path(sys.argv[1]) / "managed.token", "durable secret\\n")
"""
    result = subprocess.run([sys.executable, "-c", script, str(folder)], check=False)
    assert result.returncode == 29
    target = folder / "managed.token"
    assert private_read(target, managed=True) == b"durable secret\n"
    assert target.stat().st_nlink == 1
    assert list(folder.iterdir()) == [target]


@pytest.mark.parametrize("code", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EIO])
def test_atomic_publication_error_preserves_existing_files(tmp_path, monkeypatch, code):
    import memory_worlds.setup_files as files

    folder = tmp_path / "private"
    ensure_private_dir(folder)
    source, destination = folder / "prepared", folder / "foreign"
    source.write_bytes(b"prepared secret")
    destination.write_bytes(b"foreign content")

    class FailingRename:
        def __call__(self, *args):
            files.ctypes.set_errno(code)
            return -1

    class Library:
        renameat2 = FailingRename()
        renameatx_np = FailingRename()

    monkeypatch.setattr(files.ctypes, "CDLL", lambda *args, **kwargs: Library())
    with files._directory(folder) as directory:
        with pytest.raises(OSError if code == errno.EIO else MemoryError):
            files._rename_new(directory, source.name, destination.name)
    assert source.read_bytes() == b"prepared secret"
    assert destination.read_bytes() == b"foreign content"


def test_atomic_publication_rejects_unsupported_platform_without_changes(tmp_path, monkeypatch):
    import memory_worlds.setup_files as files

    monkeypatch.setattr(files.sys, "platform", "unsupported")
    with pytest.raises(MemoryError, match="Linux or macOS"):
        write_private(tmp_path / "private" / "output", "prepared secret")
    assert list((tmp_path / "private").iterdir()) == []


def test_setup_lock_rejects_hard_links_without_modifying_aliased_file(tmp_path):
    config = configuration(tmp_path)
    victim = tmp_path / "unrelated"
    victim.write_bytes(b"do not modify\n")
    victim.chmod(0o600)
    ensure_private_dir(config.state_dir)
    os.link(victim, config.path("setup.lock"))
    with pytest.raises(MemoryError, match="hard link"):
        with setup_lock(config):
            pytest.fail("aliased setup lock must not be acquired")
    assert victim.read_bytes() == config.path("setup.lock").read_bytes() == b"do not modify\n"


def test_lock_exclusion_and_readonly_missing_state(tmp_path):
    config = configuration(tmp_path)
    with pytest.raises(MemoryError):
        load_state(config)
    assert not config.state_dir.exists()
    with setup_lock(config):
        with pytest.raises(Conflict, match="another setup"):
            with setup_lock(config):
                pass
    with setup_lock(config):
        assert stat.S_IMODE(config.state_dir.stat().st_mode) == 0o700


def test_state_digest_stops_unreviewed_configuration_drift(tmp_path):
    config = configuration(tmp_path)
    state = {
        "instance": config.instance,
        "config_digest": config.digest,
        "authority_id": "authority",
        "user_id": config.owner,
        "resources": {},
        "files": {},
        "compose_project": "memory-worlds-test",
    }
    write_private(config.path("state.json"), json.dumps(state))
    assert load_state(config)["authority_id"] == "authority"
    changed = config.model_copy(update={"port": 8788})
    with pytest.raises(Conflict, match="changed"):
        load_state(changed)
    assert load_state(changed, require_digest=False)["authority_id"] == "authority"


def test_config_roundtrip_preserves_shell_syntax_as_data(tmp_path):
    model = "model-$(touch /tmp/unwanted)-`false`"
    config = configuration(tmp_path)
    config.extraction.model = model
    path = tmp_path / "config" / "setup.toml"
    save_config(config, path)
    assert load_config(path).extraction.model == model


def test_private_read_rejects_ancestor_swapped_after_inspection(tmp_path, monkeypatch):
    import memory_worlds.setup_files as files

    original, substituted = tmp_path / "original", tmp_path / "substituted"
    ensure_private_dir(original)
    ensure_private_dir(substituted)
    write_private(original / "secret", "expected")
    write_private(substituted / "secret", "substituted")
    inspect = files.check_path

    def swap(path):
        result = inspect(path)
        if path == original / "secret":
            original.rename(tmp_path / "held")
            original.symlink_to(substituted, target_is_directory=True)
        return result

    monkeypatch.setattr(files, "check_path", swap)
    with pytest.raises(MemoryError):
        private_read(original / "secret")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_daemon_lock_never_truncates_an_aliased_or_special_file(tmp_path, kind):
    from memory_worlds.locking import DaemonLock

    data = tmp_path / "data"
    data.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("unrelated content")
    victim.chmod(0o600)
    lock = data / "daemon.lock"
    if kind == "symlink":
        lock.symlink_to(victim)
    elif kind == "hardlink":
        os.link(victim, lock)
    else:
        os.mkfifo(lock, mode=0o600)
    with pytest.raises(MemoryError):
        with DaemonLock(data):
            pass
    assert victim.read_text() == "unrelated content"
