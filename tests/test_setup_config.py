"""Setup must be inspectable offline and never turn configuration into code."""

import hashlib
import json
import os
import stat

import pytest
from pydantic import ValidationError

from memory_worlds.errors import Conflict, MemoryError
from memory_worlds.setup import plan_setup, validate_setup
from memory_worlds.setup_config import SetupConfig, load_config, save_config
from memory_worlds.setup_files import (
    ensure_private_dir,
    load_state,
    private_read,
    setup_lock,
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
