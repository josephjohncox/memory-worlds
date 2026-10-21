"""Exercise setup through the installed CLI without ambient authority settings."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from types import ModuleType

import pytest

from memory_worlds.cli import parser, run
from memory_worlds.errors import MemoryError
from memory_worlds.models import EpisodeInput
from memory_worlds.setup_cli import validate_setup
from memory_worlds.setup_config import SetupConfig, save_config
from memory_worlds.setup_files import read_json, write_private
from memory_worlds.store import MemoryStore


@pytest.fixture
def service_cli(tmp_path, monkeypatch):
    """Isolate CLI routing from the operating system's user service manager."""
    provider = {
        "provider": "openai-compatible",
        "model": "explicit",
        "base_url": "http://localhost:9999/v1",
    }
    config = SetupConfig(
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        extraction=provider,
        embedding=provider,
    )
    path = tmp_path / "setup.toml"
    save_config(config, path)
    calls = []

    def service_setup(config, config_file, operation, *, start=False):
        calls.append((config, config_file, operation, start))
        return {"operation": operation, "started": start}

    module = ModuleType("memory_worlds.setup_service")
    module.service_setup = service_setup
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return config, path, calls


@pytest.mark.parametrize(
    "operation", ["install", "start", "stop", "restart", "status", "uninstall"]
)
@pytest.mark.parametrize("config_position", ["setup", "service", "action"])
def test_service_dispatch_accepts_config_at_each_level(
    service_cli, config_position, operation, capsys
):
    config, path, calls = service_cli
    arguments = ["setup"]
    if config_position == "setup":
        arguments += ["--config", str(path)]
    arguments += ["service"]
    if config_position == "service":
        arguments += ["--config", str(path)]
    arguments += [operation]
    if config_position == "action":
        arguments += ["--config", str(path)]

    run(parser().parse_args(arguments))

    assert calls == [(config, path, operation, False)]
    assert json.loads(capsys.readouterr().out) == {"operation": operation, "started": False}
    assert not config.data_dir.exists()
    assert not config.state_dir.exists()


def test_service_install_explicit_start_is_forwarded(service_cli, capsys):
    config, path, calls = service_cli
    run(parser().parse_args(["setup", "service", "install", "--start", "--config", str(path)]))
    assert calls == [(config, path, "install", True)]
    assert json.loads(capsys.readouterr().out)["started"] is True


@pytest.mark.parametrize("operation", ["start", "stop", "restart", "status", "uninstall"])
def test_service_start_flag_is_only_valid_for_install(operation):
    with pytest.raises(SystemExit):
        parser().parse_args(["setup", "service", operation, "--start"])


@pytest.mark.parametrize(
    "wizard_arguments",
    [[], ["init"], ["--service"], ["init", "--service"], ["--service", "init"]],
)
@pytest.mark.parametrize("apply_now", [True, False])
def test_wizard_installs_service_only_when_requested_and_applied(
    service_cli, wizard_arguments, apply_now, monkeypatch, capsys
):
    from memory_worlds import setup_interactive, setup_provision
    from memory_worlds.setup_cli import run_setup_command

    config, path, calls = service_cli
    apply_calls = []
    monkeypatch.setattr(setup_interactive, "wizard", lambda selected: config)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes" if apply_now else "no")

    def apply_setup(selected, *, adopt_admin_file):
        assert not calls, "service must not be installed before provisioning succeeds"
        apply_calls.append((selected, adopt_admin_file))
        return {"applied": True}

    monkeypatch.setattr(setup_provision, "apply_setup", apply_setup)
    args = parser().parse_args(["setup", "--config", str(path), *wizard_arguments])
    result = run_setup_command(args)

    requested = "--service" in wizard_arguments
    assert apply_calls == ([(config, None)] if apply_now else [])
    assert calls == ([(config, path, "install", False)] if requested and apply_now else [])
    if requested and apply_now:
        assert result["service"] == {"operation": "install", "started": False}
    else:
        assert "service" not in result
    if not apply_now:
        assert result["next"] == "setup apply"
        if requested:
            assert result["then"] == "setup service install"
    output = capsys.readouterr().out
    if apply_now and not requested:
        assert "mise run setup -- service install --start --config" in output
        assert str(path) in output


def test_wizard_provision_failure_never_installs_service(service_cli, monkeypatch):
    from memory_worlds import setup_interactive, setup_provision
    from memory_worlds.setup_cli import run_setup_command

    config, path, calls = service_cli
    monkeypatch.setattr(setup_interactive, "wizard", lambda selected: config)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")

    def fail_setup(*args, **kwargs):
        raise MemoryError("provisioning failed")

    monkeypatch.setattr(setup_provision, "apply_setup", fail_setup)
    with pytest.raises(MemoryError, match="provisioning failed"):
        run_setup_command(
            parser().parse_args(["setup", "init", "--service", "--config", str(path)])
        )
    assert calls == []


def test_wizard_service_flag_rejects_non_wizard_commands():
    from memory_worlds.setup_cli import run_setup_command

    args = parser().parse_args(["setup", "--service", "plan"])
    with pytest.raises(MemoryError, match="only with setup or setup init"):
        run_setup_command(args)


def test_setup_dispatch_precedes_admin_token_and_local_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORY_WORLDS_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("MEMORY_WORLDS_DATABASE_URL", "postgresql://bad:secret@127.0.0.1:1/db")
    values = {
        "provider": "openai-compatible",
        "model": "explicit",
        "base_url": "http://localhost:9999/v1",
    }
    config = SetupConfig(
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        extraction=values,
        embedding=values,
    )
    path = tmp_path / "config" / "setup.toml"
    save_config(config, path)
    for arguments in (
        ["setup", "plan", "--config", str(path)],
        ["setup", "--config", str(path), "validate"],
    ):
        run(parser().parse_args(arguments))
        result = json.loads(capsys.readouterr().out)
        assert result["instance"] == config.instance
    assert not config.data_dir.exists()
    assert not config.state_dir.exists()


def test_cli_config_error_redacts_input_and_never_initializes(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text('api_key = "PRIVATE-SENTINEL"\n')
    result = subprocess.run(
        [sys.executable, "-m", "memory_worlds.cli", "setup", "validate", "--config", str(path)],
        env={
            key: value for key, value in os.environ.items() if not key.startswith("MEMORY_WORLDS_")
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "PRIVATE-SENTINEL" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "configuration contract" in result.stderr


@pytest.mark.parametrize(
    "command", ["init", "plan", "validate", "connect", "apply", "run", "doctor", "down"]
)
def test_all_setup_commands_expose_config_argument(command):
    args = parser().parse_args(["setup", command, "--config", "/tmp/memory-example.toml"])
    assert str(args.config) == "/tmp/memory-example.toml"
    assert args.setup_command == command


def adoption_fixture(tmp_path, *, stale_kind="missing"):
    provider = {
        "provider": "openai-compatible",
        "model": "explicit",
        "base_url": "http://localhost:9999/v1",
    }
    stale_reference = tmp_path / "moved-admin.token"
    if stale_kind == "symlink":
        target = tmp_path / "unrelated-token"
        write_private(target, "unrelated-data\n")
        stale_reference.symlink_to(target)
    elif stale_kind == "parent-file":
        parent = tmp_path / "former-directory"
        parent.write_text("unrelated parent data")
        stale_reference = parent / "admin.token"
    config = SetupConfig(
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
        adopt_admin_file=stale_reference,
        extraction=provider,
        embedding=provider,
    )
    store = MemoryStore(config.data_dir)
    world, admin = store.bootstrap("original-owner")
    principal = store.authenticate(admin)
    episode = store.capture(
        principal,
        world.id,
        EpisodeInput(
            source_id="existing-source",
            content="Existing source must survive adoption",
            occurred_at=datetime.now(UTC),
            project=False,
        ),
    ).episode
    path = tmp_path / "config" / "setup.toml"
    save_config(config, path)
    override = tmp_path / "valid-admin.token"
    write_private(override, admin + "\n")
    return config, path, override, store, principal, world, episode


@pytest.mark.parametrize("valid_override", [True, False])
@pytest.mark.parametrize("stale_kind", ["missing", "symlink", "parent-file"])
def test_cli_adoption_override_replaces_stale_reference_without_rewriting_config(
    tmp_path, valid_override, stale_kind
):
    config, path, override, store, principal, world, episode = adoption_fixture(
        tmp_path, stale_kind=stale_kind
    )
    original = path.read_bytes()
    if not valid_override:
        override.write_text("mw_" + "a" * 32 + "." + "b" * 43 + "\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "memory_worlds.cli",
            "setup",
            "apply",
            "--config",
            str(path),
            "--adopt-admin-file",
            str(override),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == (0 if valid_override else 1), result.stderr
    assert path.read_bytes() == original
    assert store.get_episode(principal, world.id, episode.id).content == episode.content
    if valid_override:
        receipt = json.loads(result.stdout)
        assert receipt["authority_id"] == store.authority_id
        state = read_json(config.path("state.json"))
        assert state["operator_token_file"] == str(override)
        assert state["config_digest"] == config.digest
    else:
        assert "administrator" in result.stderr
        assert not config.path("state.json").exists()


@pytest.mark.parametrize("stale_kind", ["missing", "symlink", "parent-file"])
def test_standalone_validation_checks_configured_adoption_reference(tmp_path, stale_kind):
    config, *_ = adoption_fixture(tmp_path, stale_kind=stale_kind)
    with pytest.raises(MemoryError):
        validate_setup(config)
    with pytest.raises(MemoryError):
        validate_setup(config, adopt_admin_file=tmp_path / "missing-override.token")


def test_wizard_apply_dispatch_validates_effective_adoption_override(tmp_path, monkeypatch, capsys):
    import memory_worlds.setup_interactive as interactive

    config, path, override, store, *_ = adoption_fixture(tmp_path)
    monkeypatch.setattr(interactive, "wizard", lambda path: config)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    run(
        parser().parse_args(
            ["setup", "init", "--config", str(path), "--adopt-admin-file", str(override)]
        )
    )
    output = capsys.readouterr().out
    assert store.authority_id in output
    assert read_json(config.path("state.json"))["operator_token_file"] == str(override)
