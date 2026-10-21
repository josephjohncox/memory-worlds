"""Qualify service definitions and lifecycle without registering host services."""

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_worlds import setup_service as service
from memory_worlds.errors import MemoryError
from memory_worlds.setup_config import SetupConfig, save_config
from memory_worlds.setup_files import private_read, read_json, setup_lock
from memory_worlds.setup_provision import apply_setup


@pytest.fixture
def installed_setup(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    home.mkdir(mode=0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret-must-not-enter-service")
    monkeypatch.setenv("MEMORY_WORLDS_ADMIN_TOKEN", "ambient-admin-must-not-enter-service")
    provider = {
        "provider": "openai-compatible",
        "model": "local-model",
        "base_url": "http://127.0.0.1:9/v1",
    }
    config = SetupConfig(
        instance="service-test",
        owner="alice",
        data_dir=tmp_path / "source data",
        state_dir=tmp_path / "private state",
        extraction=provider,
        embedding=provider,
    )
    config_file = tmp_path / "configuration" / "setup file.toml"
    save_config(config, config_file)
    apply_setup(config)
    calls = []
    status = {"loaded": False, "running": False, "enabled": False}

    def manager(arguments, *, allowed=(0,)):
        calls.append(list(arguments))
        output = b""
        code = 0
        if "show" in arguments:
            receipt = read_json(config.path("service.json"))
            output = (
                "LoadState=loaded\n"
                f"ActiveState={'active' if status['running'] else 'inactive'}\n"
                f"UnitFileState={'enabled' if status['enabled'] else 'disabled'}\n"
                f"FragmentPath={receipt['path']}\n"
            ).encode()
        elif "print" in arguments:
            if arguments[-1] == f"gui/{os.getuid()}":
                output = b"GUI login domain\n"
            elif status["loaded"]:
                state = "running" if status["running"] else "exited"
                output = f"service = {{\n state = {state}\n pid = 4321\n}}\n".encode()
            else:
                code = 113
        elif "print-disabled" in arguments:
            receipt = read_json(config.path("service.json"))
            disabled = "false" if status["enabled"] else "true"
            output = f'disabled services = {{\n "{receipt["name"]}" => {disabled}\n}}\n'.encode()
        elif "enable" in arguments:
            status["enabled"] = True
        elif "disable" in arguments:
            status["enabled"] = False
            if "--now" in arguments:
                status["running"] = False
        elif any(value in arguments for value in ("start", "restart", "bootstrap", "kickstart")):
            status.update(loaded=True, running=True)
        elif "stop" in arguments or "bootout" in arguments:
            status.update(loaded=False, running=False)
        if code not in allowed:
            raise MemoryError("synthetic service manager failure")
        return subprocess.CompletedProcess(arguments, code, stdout=output, stderr=b"")

    original_which = shutil.which
    monkeypatch.setattr(
        service.shutil,
        "which",
        lambda name, *args, **kwargs: (
            "/usr/bin/docker" if name == "docker" else original_which(name, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(service, "_manager", manager)
    monkeypatch.setattr(service, "_platform", lambda: "systemd")
    shared = home / ".config" / "systemd" / "user"
    shared.mkdir(parents=True, mode=0o755)
    shared.chmod(0o755)
    return SimpleNamespace(
        config=config,
        config_file=config_file,
        home=home,
        calls=calls,
        status=status,
        manager=manager,
        shared=shared,
    )


def install(setup, *, start=False):
    return service.service_setup(setup.config, setup.config_file, "install", start=start)


def test_uninstall_cancels_install_interrupted_before_publication(installed_setup, monkeypatch):
    setup = installed_setup

    def interrupt(*_args, **_kwargs):
        raise MemoryError("interrupted before publishing service")

    monkeypatch.setattr(service, "publish_definition", interrupt)
    with pytest.raises(MemoryError):
        install(setup)
    receipt = read_json(setup.config.path("service.json"))
    assert receipt["published"] is False
    assert not Path(receipt["path"]).exists()
    assert not setup.calls
    result = service.service_setup(setup.config, setup.config_file, "uninstall")
    assert result["installed"] is False
    assert not setup.config.path("service.json").exists()
    assert not setup.calls
    assert setup.config.path("personal.token").exists()


def test_published_definition_missing_outside_uninstall_is_refused(installed_setup, monkeypatch):
    setup = installed_setup

    def interrupt(*_args, **_kwargs):
        raise MemoryError("interrupted during manager registration")

    monkeypatch.setattr(service, "_manager", interrupt)
    with pytest.raises(MemoryError):
        install(setup)
    receipt = read_json(setup.config.path("service.json"))
    assert receipt["published"] is True
    Path(receipt["path"]).unlink()
    monkeypatch.setattr(service, "_manager", setup.manager)
    for operation in ("install", "uninstall"):
        with pytest.raises(MemoryError):
            service.service_setup(setup.config, setup.config_file, operation)
    assert setup.config.path("service.json").exists()
    assert not setup.calls


def test_install_is_owned_private_and_repeatable_without_copying_ambient_secrets(
    installed_setup, monkeypatch
):
    setup = installed_setup
    install(setup)
    receipt_bytes = private_read(setup.config.path("service.json"))
    receipt = json.loads(receipt_bytes)
    path = Path(receipt["path"])
    definition = path.read_bytes()
    assert b'"-I"' in definition
    assert receipt["phase"] == "installed"
    assert receipt["authority_id"] == read_json(setup.config.path("state.json"))["authority_id"]
    assert receipt["config_file"] == str(setup.config_file)
    assert receipt["definition_sha256"] == hashlib.sha256(definition).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(setup.shared.stat().st_mode) == 0o755
    assert not setup.status["running"]
    for content in (definition, receipt_bytes):
        assert b"ambient-secret" not in content
        assert b"ambient-admin" not in content
        assert private_read(setup.config.path("graph.token")).strip() not in content
        assert private_read(setup.config.path("personal.token")).strip() not in content
    monkeypatch.setenv("PATH", "/different/bin:/usr/bin")
    monkeypatch.setattr(service.sys, "executable", "/different/python")
    install(setup)
    assert path.read_bytes() == definition
    assert private_read(setup.config.path("service.json")) == receipt_bytes


def test_service_preserves_the_virtual_environment_interpreter_path(installed_setup, monkeypatch):
    setup = installed_setup
    executable = setup.home / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(sys.executable)
    monkeypatch.setattr(service.sys, "executable", str(executable))
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    assert receipt["python"] == str(executable)
    assert str(executable) in Path(receipt["path"]).read_text()


def test_installed_interpreter_ignores_python_environment_but_loads_venv_dependencies(
    installed_setup, monkeypatch, tmp_path
):
    setup = installed_setup
    monkeypatch.setattr(service, "_platform", lambda: "launchd")
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    definition = plistlib.loads(Path(receipt["path"]).read_bytes())
    interpreter = definition["ProgramArguments"][:2]
    assert interpreter == [sys.executable, "-I"]
    hostile = tmp_path / "hostile Python environment"
    package = hostile / "memory_worlds"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('raise RuntimeError("ambient Python path was imported")\n')
    script = (
        "import json, memory_worlds, pydantic, sys; "
        "print(json.dumps({'module':memory_worlds.__file__,'isolated':sys.flags.isolated}))"
    )
    result = subprocess.run(
        [*interpreter, "-c", script],
        env={**os.environ, "PYTHONPATH": str(hostile), "PYTHONHOME": str(hostile)},
        cwd=hostile,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    observed = json.loads(result.stdout)
    assert observed["isolated"] == 1
    assert (
        Path(observed["module"]).resolve()
        == Path(service.__file__).with_name("__init__.py").resolve()
    )


def test_changed_definition_is_not_overwritten_or_controlled(installed_setup):
    setup = installed_setup
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    path = Path(receipt["path"])
    path.write_bytes(b"# user changed this service\n")
    setup.calls.clear()
    for operation in ("install", "start", "stop", "restart", "uninstall"):
        with pytest.raises(MemoryError):
            service.service_setup(setup.config, setup.config_file, operation)
        assert path.read_bytes() == b"# user changed this service\n"
        assert not setup.calls


def test_missing_receipt_does_not_authorize_an_existing_definition(installed_setup):
    setup = installed_setup
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    path = Path(receipt["path"])
    before = path.read_bytes()
    setup.config.path("service.json").unlink()
    setup.calls.clear()
    with pytest.raises(MemoryError):
        install(setup)
    assert path.read_bytes() == before
    assert not setup.calls


def test_uninstall_retains_authority_and_graph_data(installed_setup):
    setup = installed_setup
    install(setup, start=True)
    receipt = read_json(setup.config.path("service.json"))
    snapshots = {
        path: path.read_bytes()
        for folder in (setup.config.data_dir, setup.config.state_dir)
        for path in folder.rglob("*")
        if path.is_file() and path.name not in ("service.json", "setup.lock")
    }
    service.service_setup(setup.config, setup.config_file, "uninstall")
    assert not Path(receipt["path"]).exists()
    assert not setup.config.path("service.json").exists()
    assert {path: path.read_bytes() for path in snapshots} == snapshots
    assert not any("docker" in argument for call in setup.calls for argument in call)


def test_failed_manager_install_retains_recoverable_owned_intent(installed_setup, monkeypatch):
    setup = installed_setup

    def fail(arguments, *, allowed=(0,)):
        raise MemoryError("synthetic service manager failure")

    monkeypatch.setattr(service, "_manager", fail)
    with pytest.raises(MemoryError):
        install(setup)
    receipt = read_json(setup.config.path("service.json"))
    path = Path(receipt["path"])
    before = path.read_bytes()
    assert receipt["phase"] == "prepared"
    monkeypatch.setattr(service, "_manager", setup.manager)
    install(setup)
    assert read_json(setup.config.path("service.json"))["phase"] == "installed"
    assert path.read_bytes() == before


def test_interrupted_definition_publication_recovers_the_recorded_intent(
    installed_setup, monkeypatch
):
    setup = installed_setup
    publish = service.publish_definition

    def interrupt(path, content, *, recorded):
        receipt = read_json(setup.config.path("service.json"))
        assert receipt["phase"] == "prepared"
        assert receipt["definition_sha256"] == hashlib.sha256(content).hexdigest()
        assert recorded and not path.exists()
        raise MemoryError("synthetic interruption before definition publication")

    monkeypatch.setattr(service, "publish_definition", interrupt)
    with pytest.raises(MemoryError):
        install(setup)
    receipt = read_json(setup.config.path("service.json"))
    assert not Path(receipt["path"]).exists()
    monkeypatch.setattr(service, "publish_definition", publish)
    monkeypatch.setattr(service.sys, "executable", "/changed/after/interruption/python")
    monkeypatch.setenv("PATH", "/changed/after/interruption")
    install(setup)
    recovered = read_json(setup.config.path("service.json"))
    assert recovered["python"] == receipt["python"]
    assert recovered["definition"] == receipt["definition"]
    assert Path(recovered["path"]).read_text() == receipt["definition"]


def test_pending_install_refuses_a_foreign_definition(installed_setup, monkeypatch):
    setup = installed_setup
    publish = service.publish_definition

    def interrupt(path, content, *, recorded):
        path.write_bytes(b"foreign service definition\n")
        path.chmod(0o600)
        raise MemoryError("synthetic interruption with foreign destination")

    monkeypatch.setattr(service, "publish_definition", interrupt)
    with pytest.raises(MemoryError):
        install(setup)
    receipt = read_json(setup.config.path("service.json"))
    monkeypatch.setattr(service, "publish_definition", publish)
    setup.calls.clear()
    with pytest.raises(MemoryError):
        install(setup)
    assert Path(receipt["path"]).read_bytes() == b"foreign service definition\n"
    assert not setup.calls


def test_failed_manager_uninstall_can_be_retried(installed_setup, monkeypatch):
    setup = installed_setup
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    path = Path(receipt["path"])

    def fail(arguments, *, allowed=(0,)):
        raise MemoryError("synthetic service manager failure")

    monkeypatch.setattr(service, "_manager", fail)
    with pytest.raises(MemoryError):
        service.service_setup(setup.config, setup.config_file, "uninstall")
    assert path.exists()
    assert read_json(setup.config.path("service.json"))["phase"] == "removing"
    monkeypatch.setattr(service, "_manager", setup.manager)
    service.service_setup(setup.config, setup.config_file, "uninstall")
    assert not path.exists()
    assert not setup.config.path("service.json").exists()


def test_uninstall_recovers_after_definition_removal_before_manager_reload(
    installed_setup, monkeypatch
):
    setup = installed_setup
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    path = Path(receipt["path"])

    def manager(arguments, *, allowed=(0,)):
        if "daemon-reload" in arguments and not path.exists():
            raise MemoryError("synthetic interruption after definition removal")
        return setup.manager(arguments, allowed=allowed)

    monkeypatch.setattr(service, "_manager", manager)
    with pytest.raises(MemoryError):
        service.service_setup(setup.config, setup.config_file, "uninstall")
    assert not path.exists()
    assert read_json(setup.config.path("service.json"))["phase"] == "removing"

    def recovery_manager(arguments, *, allowed=(0,)):
        if "disable" in arguments and not path.exists():
            raise MemoryError("cannot disable missing unit definition")
        return setup.manager(arguments, allowed=allowed)

    monkeypatch.setattr(service, "_manager", recovery_manager)
    service.service_setup(setup.config, setup.config_file, "uninstall")
    assert not setup.config.path("service.json").exists()


def test_start_does_not_hold_the_runtime_setup_lock(installed_setup, monkeypatch):
    setup = installed_setup

    def manager(arguments, *, allowed=(0,)):
        if "start" in arguments:
            with setup_lock(setup.config):
                pass
        return setup.manager(arguments, allowed=allowed)

    monkeypatch.setattr(service, "_manager", manager)
    install(setup, start=True)
    assert setup.status["running"]


def test_definition_symlink_is_never_followed(installed_setup, tmp_path):
    setup = installed_setup
    install(setup)
    receipt = read_json(setup.config.path("service.json"))
    path = Path(receipt["path"])
    victim = tmp_path / "unrelated unit"
    victim.write_bytes(b"unrelated unit content\n")
    path.unlink()
    path.symlink_to(victim)
    setup.calls.clear()
    with pytest.raises(MemoryError):
        service.service_setup(setup.config, setup.config_file, "uninstall")
    assert victim.read_bytes() == b"unrelated unit content\n"
    assert not setup.calls


def test_launchd_stop_unregisters_and_start_bootstraps_without_signalling_pids(
    installed_setup, monkeypatch
):
    setup = installed_setup
    monkeypatch.setattr(service, "_platform", lambda: "launchd")
    folder = setup.home / "Library" / "LaunchAgents"
    folder.mkdir(parents=True, mode=0o755)
    folder.chmod(0o755)
    install(setup, start=True)
    receipt = read_json(setup.config.path("service.json"))
    definition = plistlib.loads(Path(receipt["path"]).read_bytes())
    assert definition["RunAtLoad"]
    assert stat.S_IMODE(folder.stat().st_mode) == 0o755
    setup.calls.clear()
    service.service_setup(setup.config, setup.config_file, "stop")
    assert any(call[1] == "bootout" for call in setup.calls)
    assert not any(call[1] in {"stop", "kill", "disable"} for call in setup.calls)
    setup.calls.clear()
    service.service_setup(setup.config, setup.config_file, "start")
    assert any(call[1] == "bootstrap" for call in setup.calls)
    assert not any(call[1] == "kickstart" for call in setup.calls)


def test_launchd_definition_uses_literal_argument_array(installed_setup):
    setup = installed_setup
    command = [
        "/a path/venv/bin/python",
        "-I",
        "-m",
        "memory_worlds.cli",
        "setup",
        "--config",
        '/private/config $HOME %n "literal".toml',
        "run",
    ]
    environment = {"HOME": str(setup.home), "PATH": "/usr/bin:/bin"}
    content = service._render("launchd", "test.memory-worlds", command, environment, setup.config)
    definition = plistlib.loads(content)
    assert definition["ProgramArguments"] == command
    assert definition["EnvironmentVariables"] == environment
    assert definition["Label"] == "test.memory-worlds"


def test_launchd_start_restarts_a_loaded_but_inactive_job(installed_setup, monkeypatch):
    setup = installed_setup
    monkeypatch.setattr(service, "_platform", lambda: "launchd")
    install(setup)
    setup.status.update(loaded=True, running=False)
    setup.calls.clear()
    service.service_setup(setup.config, setup.config_file, "start")
    assert setup.status["running"]
    assert any(call[1] == "kickstart" for call in setup.calls)
    assert not any("-k" in call or "bootstrap" in call for call in setup.calls)


def test_launchd_status_reports_manager_disabled_override(installed_setup, monkeypatch):
    setup = installed_setup
    monkeypatch.setattr(service, "_platform", lambda: "launchd")
    install(setup)
    setup.status["enabled"] = False
    result = service.service_setup(setup.config, setup.config_file, "status")
    assert result["enabled"] is False


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd verifier required")
def test_generated_systemd_unit_passes_the_real_parser_with_literal_paths(
    installed_setup, tmp_path
):
    setup = installed_setup
    command = [
        sys.executable,
        "-I",
        "-m",
        "memory_worlds.cli",
        "setup",
        "--config",
        '/private/config $HOME %n "literal" \\ backslash.toml',
        "run",
    ]
    content = service._render(
        "systemd", "test.memory-worlds", command, {"HOME": str(setup.home)}, setup.config
    )
    assert b"$$HOME" in content
    assert b"%%n" in content
    path = tmp_path / "literal-check.service"
    path.write_bytes(content)
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", "--man=no", str(path)],
        env={**os.environ, "SYSTEMD_UNIT_PATH": str(tmp_path) + ":"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
