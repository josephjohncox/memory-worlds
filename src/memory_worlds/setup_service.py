"""Explicit per-user supervision of the existing guided-setup foreground runner."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from .errors import Conflict, MemoryError
from .setup_config import SetupConfig
from .setup_files import (
    check_path,
    load_state,
    private_read,
    read_json,
    setup_lock,
    unlink_private,
    write_private,
)
from .setup_service_files import publish_definition, read_definition, remove_definition


def _platform() -> str:
    if sys.platform == "linux":
        return "systemd"
    if sys.platform == "darwin":
        return "launchd"
    raise MemoryError("user services require Linux systemd or macOS launchd")


def _location(config: SetupConfig, manager: str, authority_id: str) -> tuple[str, Path]:
    identity = UUID(authority_id).hex
    if manager == "systemd":
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        if not base.is_absolute():
            raise MemoryError("XDG_CONFIG_HOME must be an absolute path")
        name = f"memory-worlds-{config.instance}-{identity}.service"
        return name, check_path(base / "systemd/user" / name)
    name = f"org.memory-worlds.{config.instance}.{identity}"
    return name, check_path(Path.home() / "Library/LaunchAgents" / f"{name}.plist")


def _unit_quote(value: str, *, command: bool = False) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise MemoryError("service paths and environment must not contain control characters")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def _render(
    manager: str, name: str, command: list[str], environment: dict[str, str], config: SetupConfig
) -> bytes:
    if manager == "systemd":
        lines = [
            "# Managed by Memory Worlds setup service.",
            "[Unit]",
            "Description=Memory Worlds local memory service",
            "StartLimitIntervalSec=0",
            "[Service]",
            "Type=exec",
            "ExecStart=" + " ".join(_unit_quote(value, command=True) for value in command),
            # This directive does not use ExecStart's argument quoting. The
            # actual API child uses state_dir; its supervisor needs no cwd data.
            "WorkingDirectory=/",
            *("Environment=" + _unit_quote(f"{key}={value}") for key, value in environment.items()),
            "Restart=on-failure",
            "RestartSec=30",
            "TimeoutStopSec=45",
            "KillMode=control-group",
            "UMask=0077",
            "[Install]",
            "WantedBy=default.target",
        ]
        return ("\n".join(lines) + "\n").encode()
    return plistlib.dumps(
        {
            "Label": name,
            "ProgramArguments": command,
            "WorkingDirectory": str(config.state_dir),
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "ExitTimeOut": 45,
            "ProcessType": "Background",
            "Umask": 0o077,
            "StandardOutPath": str(config.path("service.stdout.log")),
            "StandardErrorPath": str(config.path("service.stderr.log")),
        },
        sort_keys=True,
    )


def _manager(arguments: list[str], *, allowed: tuple[int, ...] = (0,)):
    # Preserve user-manager transport, not model keys or authority credentials.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "DBUS_SESSION_BUS_ADDRESS",
            "TMPDIR",
        }
    }
    try:
        result = subprocess.run(
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MemoryError("user service manager is unavailable in this login session") from None
    if result.returncode not in allowed or len(result.stdout) > 1_048_576:
        raise MemoryError("user service manager command failed; check the current user session")
    return result


def _launch_target(record: dict) -> str:
    return f"gui/{os.getuid()}/{record['name']}"


def _status(record: dict) -> dict:
    if record["manager"] == "systemd":
        result = _manager(
            [
                "systemctl",
                "--user",
                "show",
                record["name"],
                "--property=LoadState,ActiveState,UnitFileState",
            ]
        )
        values = dict(
            line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line
        )
        return {
            "loaded": values.get("LoadState") == "loaded",
            "running": values.get("ActiveState") == "active",
            "enabled": values.get("UnitFileState") == "enabled",
        }
    # Distinguish a missing job from an unavailable GUI login domain.
    _manager(["launchctl", "print", f"gui/{os.getuid()}"])
    result = _manager(["launchctl", "print", _launch_target(record)], allowed=(0, 3, 113))
    disabled = _manager(["launchctl", "print-disabled", f"gui/{os.getuid()}"])
    return {
        "loaded": result.returncode == 0,
        "running": result.returncode == 0 and b"state = running" in result.stdout,
        "enabled": re.search(
            rf'"{re.escape(record["name"])}"\s*=>\s*true', disabled.stdout.decode()
        )
        is None,
    }


def _control(record: dict, action: str) -> None:
    if record["manager"] == "systemd":
        _manager(["systemctl", "--user", action, record["name"]])
        return
    target = _launch_target(record)
    if action == "enable":
        _manager(["launchctl", "enable", target])
    elif action == "disable":
        _manager(["launchctl", "disable", target])
    elif action in {"start", "stop", "restart"}:
        status = _status(record)
        loaded = status["loaded"]
        if loaded and action in {"stop", "restart"}:
            _manager(["launchctl", "bootout", target])
        if action in {"start", "restart"} and (not loaded or action == "restart"):
            _manager(["launchctl", "enable", target])
            _manager(["launchctl", "bootstrap", f"gui/{os.getuid()}", record["path"]])
        elif action == "start" and not status["running"]:
            _manager(["launchctl", "enable", target])
            _manager(["launchctl", "kickstart", target])


def _encode(record: dict) -> bytes:
    return (json.dumps(record, sort_keys=True, indent=2) + "\n").encode()


def _save(config: SetupConfig, record: dict) -> None:
    path = config.path("service.json")
    expected = (
        hashlib.sha256(private_read(path, managed=True)).hexdigest() if path.exists() else None
    )
    write_private(path, _encode(record), expected_hash=expected)


def _record(config: SetupConfig, config_file: Path) -> dict | None:
    path = config.path("service.json")
    if not os.path.lexists(path):
        return None
    record = read_json(path)
    try:
        manager = _platform()
        name, destination = _location(config, manager, record["authority_id"])
        if (
            record["manager"] != manager
            or record["name"] != name
            or record["path"] != str(destination)
            or record["config_file"] != str(config_file)
            or record["phase"] not in {"prepared", "installed", "removing"}
            or not isinstance(record["published"], bool)
            or hashlib.sha256(record["definition"].encode()).hexdigest()
            != record["definition_sha256"]
        ):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise Conflict("service receipt does not match this installation and platform") from None
    return record


def _verify(record: dict, *, missing: bool = False) -> None:
    content = read_definition(Path(record["path"]))
    if content is None and missing:
        return
    if content != record["definition"].encode():
        raise Conflict("service definition is missing or was changed outside setup")


def _logs(config: SetupConfig) -> None:
    for name in ("service.stdout.log", "service.stderr.log"):
        path = config.path(name)
        if not os.path.lexists(path):
            write_private(path, b"")
        info = check_path(path).lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise Conflict("service logs must be owned private regular files")


def _install(config: SetupConfig, config_file: Path, record: dict | None) -> dict:
    with setup_lock(config):
        from .setup_runtime import _authority, runtime_environment

        state = load_state(config)
        _authority(config, state)
        runtime_environment(config)  # Validate applied files, without Docker or model calls.
        if record is not None:
            if record["authority_id"] != state["authority_id"] or record["phase"] == "removing":
                raise Conflict("service removal must finish before installing this authority")
            _verify(record, missing=record["phase"] == "prepared" and not record["published"])
        else:
            manager = _platform()
            name, destination = _location(config, manager, state["authority_id"])
            if read_definition(destination) is not None:
                raise Conflict("setup refuses to replace an unowned service definition")
            docker = shutil.which("docker")
            if not docker:
                raise MemoryError("install Docker with Compose before installing the user service")
            docker_dir = str(Path(docker).absolute().parent)
            if ":" in docker_dir:
                raise MemoryError("Docker executable directory cannot contain a PATH separator")
            environment = {
                "HOME": str(Path.home()),
                "PATH": os.pathsep.join(dict.fromkeys([docker_dir, *os.defpath.split(os.pathsep)])),
                "PYTHONUTF8": "1",
                "PYTHONNOUSERSITE": "1",
                "GRAPHITI_TELEMETRY_ENABLED": "false",
                "OTEL_SDK_DISABLED": "true",
            }
            # Resolving this symlink would bypass the virtualenv and its dependencies.
            python = str(Path(sys.executable).absolute())
            command = [
                python,
                "-I",
                "-m",
                "memory_worlds.cli",
                "setup",
                "run",
                "--config",
                str(config_file),
            ]
            definition = _render(manager, name, command, environment, config)
            record = {
                "phase": "prepared",
                "published": False,
                "authority_id": state["authority_id"],
                "manager": manager,
                "name": name,
                "path": str(destination),
                "config_file": str(config_file),
                "python": python,
                "definition": definition.decode(),
                "definition_sha256": hashlib.sha256(definition).hexdigest(),
            }
            _save(config, record)
        if record["manager"] == "launchd":
            _logs(config)
        publish_definition(Path(record["path"]), record["definition"].encode(), recorded=True)
        record["published"] = True
        _save(config, record)
    if record["manager"] == "systemd":
        _manager(["systemctl", "--user", "daemon-reload"])
    _control(record, "enable")
    record["phase"] = "installed"
    _save(config, record)
    return record


def service_setup(
    config: SetupConfig, config_file: Path, operation: str, *, start: bool = False
) -> dict:
    """Service files are installed only on explicit request; data is never removed."""
    if operation not in {"install", "start", "stop", "restart", "status", "uninstall"}:
        raise MemoryError("unknown service operation")
    if start and operation != "install":
        raise MemoryError("--start is only valid with service install")
    config_file = check_path(config_file)
    with setup_lock(config, name="service.lock"):
        record = _record(config, config_file)
        if operation == "install":
            record = _install(config, config_file, record)
            if start:
                _control(record, "start")
        elif record is None:
            if operation in {"status", "uninstall"}:
                return {"instance": config.instance, "installed": False}
            raise Conflict("install this user service before managing it")
        elif operation == "uninstall":
            if (
                record["phase"] == "prepared"
                and not record["published"]
                and read_definition(Path(record["path"])) is None
            ):
                # No definition was published and registration never began.
                receipt = config.path("service.json")
                unlink_private(receipt, expected_hash=hashlib.sha256(_encode(record)).hexdigest())
                return {
                    "instance": config.instance,
                    "installed": False,
                    "data": "retained",
                    "graph": "retained",
                }
            _verify(record, missing=record["phase"] == "removing")
            record["phase"] = "removing"
            _save(config, record)
            # Persist progress before deleting the unit. Retrying after deletion
            # must not ask a manager to disable a unit that no longer exists.
            removal = record.setdefault("removal", {})
            for action in ("disable", "stop"):
                if not removal.get(action):
                    _control(record, action)
                    removal[action] = True
                    _save(config, record)
            remove_definition(Path(record["path"]), record["definition_sha256"])
            if record["manager"] == "systemd":
                _manager(["systemctl", "--user", "daemon-reload"])
            receipt = config.path("service.json")
            unlink_private(receipt, expected_hash=hashlib.sha256(_encode(record)).hexdigest())
            return {
                "instance": config.instance,
                "installed": False,
                "data": "retained",
                "graph": "retained",
            }
        else:
            _verify(record)
            if operation in {"start", "restart"}:
                if record["phase"] != "installed":
                    raise Conflict("finish service installation before starting it")
                if record["manager"] == "launchd":
                    _logs(config)
            if operation != "status":
                _control(record, operation)
        result = {
            "instance": config.instance,
            "installed": True,
            "manager": record["manager"],
            "name": record["name"],
            "path": record["path"],
            "phase": record["phase"],
            "operation": operation,
            "config_file": record["config_file"],
            "python": record["python"],
        }
        if operation == "status":
            result.update(_status(record))
        return result
