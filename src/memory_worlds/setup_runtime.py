"""Owned foreground runtime and bounded private-storage diagnostics."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import UUID

import httpx

from .errors import Conflict
from .errors import MemoryError as InvalidRequest
from .graph import GraphConfig
from .setup_files import _directory, load_state, private_read, setup_lock

if TYPE_CHECKING:
    from collections.abc import Callable

    from .setup_config import SetupConfig

_OS_ENVIRONMENT = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}
_LABELS = {
    "instance": "memory-worlds.setup.instance",
    "authority_id": "memory-worlds.setup.authority",
    "compose_project": "memory-worlds.setup.project",
}
_COMPOSE_FILE = "compose.graph.yaml"
_DOCTOR_AUTHORITY_MAXIMUM = 64 * 1024 * 1024


def graph_compose_bytes(config: SetupConfig, state: dict) -> bytes:
    """The same packaged resource is used from wheels and source installations."""
    if state.get("instance") != config.instance:
        raise Conflict("Setup state belongs to another installation")
    return files("memory_worlds").joinpath("templates/setup-graph.compose.yaml").read_bytes()


def _state(config: SetupConfig) -> dict:
    state = load_state(config)
    try:
        UUID(str(state["authority_id"]))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", state["compose_project"]):
            raise ValueError
        if not isinstance(state["user_id"], str) or not state["user_id"]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise InvalidRequest("Setup state has invalid ownership metadata") from None
    return state


def _secret(path: Path, *, maximum: int = 16_386, expected_hash: str | None = None) -> str:
    try:
        content = private_read(path, maximum=maximum, managed=expected_hash is not None)
        if expected_hash is not None and hashlib.sha256(content).hexdigest() != expected_hash:
            raise Conflict("A setup-managed credential was changed outside setup")
        value = content.decode("ascii").strip()
    except (OSError, UnicodeError, ValueError):
        raise InvalidRequest("A setup credential file cannot be read safely") from None
    if not value or len(value) > maximum - 2 or any(character.isspace() for character in value):
        raise InvalidRequest("A setup credential file contains an invalid secret")
    return value


def _managed_hash(state: dict, path: Path) -> str:
    expected = state["files"].get(str(path))
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise Conflict("Setup file has no recorded ownership digest")
    return expected


def _verify_files(state: dict) -> None:
    if len(state["files"]) > 100:
        raise InvalidRequest("Setup state contains too many managed files")
    for name in state["files"]:
        if not isinstance(name, str) or not Path(name).is_absolute():
            raise InvalidRequest("Setup managed file paths must be absolute")
        path = Path(name)
        if hashlib.sha256(private_read(path, managed=True)).hexdigest() != _managed_hash(
            state, path
        ):
            raise Conflict("A setup-managed file was changed outside setup")


def _graph_environment(config: SetupConfig, state: dict, password: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in _OS_ENVIRONMENT}
    environment.update(
        MEMORY_WORLDS_SETUP_INSTANCE=config.instance,
        MEMORY_WORLDS_SETUP_AUTHORITY=str(state["authority_id"]),
        MEMORY_WORLDS_SETUP_PROJECT=state["compose_project"],
        MEMORY_WORLDS_FALKORDB_PORT=str(config.graph_port),
        MEMORY_WORLDS_FALKORDB_PASSWORD=password,
    )
    return environment


def runtime_environment(config: SetupConfig) -> dict[str, str]:
    """Build explicit local settings; inherited database/provider credentials are discarded."""
    state = _state(config)
    _verify_files(state)
    password_path = config.path("graph.token")
    password = _secret(password_path, maximum=66, expected_hash=_managed_hash(state, password_path))
    if not re.fullmatch(r"[0-9a-fA-F]{64}", password):
        raise InvalidRequest("Setup graph credential must contain 64 hexadecimal characters")
    environment = _graph_environment(config, state, password)
    environment.update(
        PYTHONUTF8="1",
        PYTHONNOUSERSITE="1",
        GRAPHITI_TELEMETRY_ENABLED="false",
        OTEL_SDK_DISABLED="true",
        MEMORY_WORLDS_STORAGE_BACKEND="sqlite",
        MEMORY_WORLDS_DATA_DIR=str(config.data_dir),
        MEMORY_WORLDS_AUTHORITY_ID=str(state["authority_id"]),
        MEMORY_WORLDS_GRAPH_BACKEND="falkordb",
        MEMORY_WORLDS_FALKORDB_HOST="127.0.0.1",
        MEMORY_WORLDS_EMBEDDING_DIMENSIONS=str(config.embedding_dimensions),
        MEMORY_WORLDS_EMBEDDING_BATCH_SIZE=str(config.embedding.batch_size),
    )
    for prefix, provider in (("LLM", config.extraction), ("EMBEDDING", config.embedding)):
        environment[f"MEMORY_WORLDS_{prefix}_PROVIDER"] = provider.provider
        environment[f"MEMORY_WORLDS_{prefix}_MODEL"] = provider.model
        if provider.base_url:
            environment[f"MEMORY_WORLDS_{prefix}_BASE_URL"] = provider.base_url
        if provider.api_key_file is not None:
            environment[f"MEMORY_WORLDS_{prefix}_API_KEY"] = _secret(provider.api_key_file)
    if config.mode == "hybrid":
        from .setup_provision import _corporate

        if _corporate(config) != state.get("corporate"):
            raise Conflict("Corporate connection no longer matches the provisioned identity")
        path = config.path("mounts.json")
        if hashlib.sha256(private_read(path, managed=True)).hexdigest() != _managed_hash(
            state, path
        ):
            raise Conflict("Setup mount configuration was changed outside setup")
        environment["MEMORY_WORLDS_MOUNTS_FILE"] = str(path)
    GraphConfig.from_env(environment)
    return environment


def _template(config: SetupConfig, state: dict) -> Path:
    path = config.path(_COMPOSE_FILE)
    content = private_read(path, managed=True)
    digest = hashlib.sha256(content).hexdigest()
    if state.get("files", {}).get(str(path)) != digest or content != graph_compose_bytes(
        config, state
    ):
        raise Conflict("Setup graph definition is missing ownership or differs from its template")
    return path


def _command(arguments: list[str], environment: dict[str, str], *, timeout: float = 30) -> bytes:
    try:
        result = subprocess.run(
            arguments,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InvalidRequest("Setup Docker command could not complete") from None
    if result.returncode or len(result.stdout) > 1_048_576:
        raise InvalidRequest("Setup Docker command failed; verify the local Docker engine")
    return result.stdout


def _docker(environment: dict[str, str]) -> list[str]:
    """Pin the selected local context so concurrent context changes cannot redirect operations."""
    try:
        contexts = json.loads(_command(["docker", "context", "inspect"], environment))
        context = contexts[0]
        if len(contexts) != 1 or not context["Endpoints"]["docker"]["Host"].startswith("unix://"):
            raise ValueError
        name = context["Name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError):
        raise InvalidRequest("Setup requires a selected local Unix Docker context") from None
    return ["docker", "--context", name]


def _compose(docker: list[str], path: Path, state: dict) -> list[str]:
    return [
        *docker,
        "compose",
        "--env-file",
        os.devnull,
        "--file",
        str(path),
        "--project-name",
        state["compose_project"],
    ]


def _owned_graph(docker: list[str], state: dict, environment: dict[str, str]) -> bool:
    """Inspect every project resource before any Compose mutation, including retained volumes."""
    found = False
    selector = f"label=com.docker.compose.project={state['compose_project']}"
    for kind, listing, output, field, expected in (
        ("container", ["ps", "--all"], "{{.ID}}", ".Config.Labels", None),
        (
            "volume",
            ["volume", "ls"],
            "{{.Name}}",
            ".Labels",
            f"{state['compose_project']}_graph-data",
        ),
        (
            "network",
            ["network", "ls"],
            "{{.Name}}",
            ".Labels",
            f"{state['compose_project']}_default",
        ),
    ):
        names = _command([*docker, *listing, "--filter", selector, "--format", output], environment)
        try:
            identifiers = set(names.decode("ascii").splitlines())
            if expected is not None:
                candidates = (
                    _command(
                        [*docker, *listing, "--filter", f"name={expected}", "--format", output],
                        environment,
                    )
                    .decode("ascii")
                    .splitlines()
                )
                if expected in candidates:
                    identifiers.add(expected)
        except UnicodeError:
            raise InvalidRequest("Docker returned invalid setup resource identifiers") from None
        if len(identifiers) > 100:
            raise Conflict("Setup Docker project contains too many resources")
        for identifier in sorted(identifiers):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", identifier):
                raise InvalidRequest("Docker returned an invalid setup resource identifier")
            try:
                labels = json.loads(
                    _command(
                        [*docker, kind, "inspect", "--format", f"{{{{json {field}}}}}", identifier],
                        environment,
                    )
                )
            except (TypeError, ValueError):
                raise Conflict("Setup Docker resource ownership cannot be verified") from None
            if not isinstance(labels, dict) or any(
                labels.get(label) != state[key] for key, label in _LABELS.items()
            ):
                raise Conflict("Docker project contains resources owned by another installation")
            if kind == "container":
                found = True
    return found


@contextmanager
def _stopped(config: SetupConfig):
    """Hold an existing daemon lock without truncating it or creating an authority directory."""
    try:
        descriptor = os.open(
            config.data_dir / "daemon.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except FileNotFoundError:
        raise InvalidRequest(
            "Setup authority has no daemon ownership lock; run setup apply"
        ) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InvalidRequest("Setup daemon ownership lock must be a regular file with one link")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Conflict("Stop the foreground daemon before this setup operation") from None
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _running(config: SetupConfig) -> bool:
    if not config.data_dir.exists() or not (config.data_dir / "daemon.lock").exists():
        return False
    try:
        with _stopped(config):
            return False
    except Conflict:
        return True


def _authority(config: SetupConfig, state: dict) -> None:
    """Validate existing control metadata; never initialize a store to inspect it."""
    path = config.data_dir / "control.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise Conflict("Setup authority is absent or is not a regular SQLite authority")
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            info = Path(str(path) + suffix).lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Conflict("Setup authority storage must be a regular file with one hard link")
    try:
        with closing(
            sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode=ro", uri=True)
        ) as connection:
            rows = dict(
                connection.execute(
                    "SELECT key, value FROM metadata WHERE key IN (?, ?, ?)",
                    ("authority_id", "bootstrapped", f"setup:{config.instance}"),
                )
            )
        _validate_authority_metadata(config, state, rows)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise Conflict("Setup authority does not match its provisioned ownership") from None


def _validate_authority_metadata(config: SetupConfig, state: dict, rows: dict) -> None:
    if rows.get("authority_id") != state["authority_id"] or rows.get("bootstrapped") != "1":
        raise ValueError
    ownership = json.loads(rows[f"setup:{config.instance}"])
    if not isinstance(ownership, dict) or any(
        state.get(key) != value for key, value in ownership.items()
    ):
        raise ValueError
    for key, expected in (
        ("instance", config.instance),
        ("config_digest", config.digest),
        ("data_dir", str(config.data_dir)),
        ("state_dir", str(config.state_dir)),
    ):
        if ownership.get(key) != expected:
            raise ValueError


def _file_identity(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def _authority_snapshot(config: SetupConfig, destination: Path, deadline: float) -> str | None:
    """Copy stable control/WAL bytes; SQLite never opens any original authority file.

    Immutable mode ignores a WAL, while ordinary read-only SQLite can create or
    update its shared-memory sidecar. Let SQLite recover a private temporary copy
    instead. A changing, oversized, or busy source is explicitly unavailable.
    """
    with _directory(config.data_dir) as directory, ExitStack() as opened:
        captured = {}
        for suffix in ("", "-wal", "-shm", "-journal"):
            name = "control.sqlite3" + suffix
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                if not suffix:
                    return "missing"
                captured[name] = None
                continue
            opened.callback(os.close, fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                return "mismatch"
            captured[name] = (fd, _file_identity(info))
            if suffix == "-journal" or info.st_size > _DOCTOR_AUTHORITY_MAXIMUM:
                return "unavailable"
        total = sum(item[1][2] for item in captured.values() if item is not None)
        if total > _DOCTOR_AUTHORITY_MAXIMUM:
            return "unavailable"
        for name in ("control.sqlite3", "control.sqlite3-wal"):
            item = captured[name]
            if item is None:
                continue
            descriptor, identity = item
            with (destination / name).open("xb") as target:
                os.chmod(target.fileno(), 0o600)
                remaining = identity[2]
                while remaining:
                    if time.monotonic() >= deadline:
                        return "unavailable"
                    block = os.read(descriptor, min(remaining, 1_048_576))
                    if not block:
                        return "unavailable"
                    target.write(block)
                    remaining -= len(block)
        for name, item in captured.items():
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                if item is not None:
                    return "unavailable"
                continue
            if item is None or _file_identity(current) != item[1]:
                return "unavailable"
            if _file_identity(os.fstat(item[0])) != item[1]:
                return "unavailable"
    return None


def _doctor_personal_credential(db: sqlite3.Connection, state: dict, token: str) -> str:
    """Check the setup personal credential without sending it to a network peer."""
    try:
        personal = state["resources"]["personal"]
        row = db.execute(
            "SELECT id, token_hash, user_id, agent_id, admin, revoked, "
            "EXISTS(SELECT 1 FROM credential_contexts WHERE credential_id=credentials.id), "
            "EXISTS(SELECT 1 FROM identity_sessions WHERE credential_id=credentials.id) "
            "FROM credentials WHERE id=?",
            (personal["credential_id"],),
        ).fetchone()
        if row is None:
            return "missing"
        if row[5] == 1:
            return "revoked"
        expected = (
            personal["credential_id"],
            hashlib.sha256(token.encode()).hexdigest(),
            state["user_id"],
            personal["agent_id"],
            0,
            0,
            0,
            0,
        )
        return "valid" if row == expected else "mismatch"
    except sqlite3.Error:
        return "unavailable"
    except (KeyError, TypeError, ValueError):
        return "mismatch"


def _doctor_inspect(config: SetupConfig, state: dict, token: str | None = None) -> dict[str, str]:
    """Inspect authority and optional credential within the same private snapshot."""
    result = {"authority": "unavailable", "personal_credential": "not_checked"}
    if not config.data_dir.exists():
        return {**result, "authority": "missing"}
    deadline = time.monotonic() + 2
    try:
        for _ in range(2):
            with tempfile.TemporaryDirectory(prefix="memory-worlds-doctor-") as temporary:
                path = Path(temporary)
                status = _authority_snapshot(config, path, deadline)
                if status is not None:
                    if status != "unavailable" or time.monotonic() >= deadline:
                        return {**result, "authority": status}
                    continue
                with closing(
                    sqlite3.connect((path / "control.sqlite3").as_uri() + "?mode=ro", uri=True)
                ) as db:
                    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_048_576)
                    db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    rows = dict(
                        db.execute(
                            "SELECT key, value FROM metadata WHERE key IN (?, ?, ?)",
                            ("authority_id", "bootstrapped", f"setup:{config.instance}"),
                        )
                    )
                    _validate_authority_metadata(config, state, rows)
                    result["authority"] = "valid"
                    if token is not None:
                        result["personal_credential"] = _doctor_personal_credential(
                            db, state, token
                        )
                    return result
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return {**result, "authority": "mismatch"}
    except (InvalidRequest, OSError):
        return result
    return result


def _available(host: str, port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError:
        raise Conflict("A configured setup port is already occupied") from None


def _graph_health(docker: list[str], compose: list[str], environment: dict[str, str]) -> str:
    try:
        identifiers = (
            _command([*compose, "ps", "--quiet", "falkordb"], environment)
            .decode("ascii")
            .splitlines()
        )
    except UnicodeError:
        raise InvalidRequest("Docker returned invalid setup graph metadata") from None
    if not identifiers:
        return "stopped"
    if len(identifiers) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", identifiers[0]):
        raise Conflict("Setup graph has an unexpected container identity")
    status = _command(
        [*docker, "container", "inspect", "--format", "{{.State.Health.Status}}", identifiers[0]],
        environment,
    ).strip()
    return {b"healthy": "healthy", b"unhealthy": "unhealthy", b"starting": "starting"}.get(
        status, "unknown"
    )


def _health(docker: list[str], compose: list[str], environment: dict[str, str]) -> None:
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        status = _graph_health(docker, compose, environment)
        if status == "healthy":
            return
        if status in {"unhealthy", "unknown"}:
            raise InvalidRequest("Setup graph health check failed")
        time.sleep(0.2)
    raise InvalidRequest("Setup graph did not become healthy before its startup deadline")


def _wait_for_daemon(config: SetupConfig, child) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise InvalidRequest("Setup daemon exited before acquiring its authority lock")
        if _running(config) and private_read(
            config.data_dir / "daemon.lock", maximum=32, managed=True
        ) == str(child.pid).encode("ascii"):
            return
        time.sleep(0.05)
    raise InvalidRequest("Setup daemon did not acquire its authority lock before startup deadline")


def _foreground(
    config: SetupConfig,
    environment: dict[str, str],
    *,
    on_started: Callable[[], None] | None = None,
) -> None:
    child = None
    previous = {}
    stopped = False

    def stop(signum, _frame):
        nonlocal stopped
        stopped = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    try:
        child = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-m",
                "memory_worlds.cli",
                "--data-dir",
                str(config.data_dir),
                "serve",
                "--host",
                config.host,
                "--port",
                str(config.port),
            ],
            env=environment,
            cwd=config.state_dir,
            start_new_session=True,
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        if on_started is not None:
            _wait_for_daemon(config, child)
            on_started()
        code = child.wait()
        if code and not stopped:
            raise InvalidRequest("Setup daemon exited unsuccessfully; verify its configuration")
    except OSError:
        raise InvalidRequest("Setup daemon could not start") from None
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def run_setup(config: SetupConfig) -> None:
    state = _state(config)
    with ExitStack() as startup:
        startup.enter_context(setup_lock(config))
        template = _template(config, state)
        environment = runtime_environment(config)
        with _stopped(config):
            _authority(config, state)
            _available(config.host, config.port)
            docker = _docker(environment)
            if not _owned_graph(docker, state, environment):
                _available(config.host, config.graph_port)
            compose = _compose(docker, template, state)
            _command(
                [*compose, "up", "--detach", "--wait", "--wait-timeout", "75"],
                environment,
                timeout=120,
            )
            _owned_graph(docker, state, environment)
            _health(docker, compose, environment)
        # The real daemon takes its own lifetime lock; setup never double-locks it.
        # Once it holds that lock, connect can rotate its separate corporate files.
        _foreground(config, environment, on_started=startup.close)


def down_setup(config: SetupConfig) -> dict:
    if _running(config):
        raise Conflict("Stop the foreground daemon before stopping its graph")
    state = _state(config)
    with setup_lock(config), _stopped(config):
        _authority(config, state)
        template = _template(config, state)
        # Down only needs interpolation values, never model or client credentials.
        # Its dummy password is parsed by Compose but never sent to the database.
        environment = _graph_environment(config, state, "0" * 64)
        docker = _docker(environment)
        _owned_graph(docker, state, environment)
        _command(
            [*_compose(docker, template, state), "down", "--timeout", "30"], environment, timeout=60
        )
    return {"instance": config.instance, "graph": "stopped", "volumes": "retained"}


def doctor_setup(config: SetupConfig) -> dict:
    result = {
        "instance": config.instance,
        "url": config.url,
        "state": "missing",
        "authority": "not_checked",
        "daemon": "stopped",
        "credentials": "not_checked",
        "personal_credential": "not_checked",
        "api": "not_checked",
        "api_identity": "not_verified",
        "graph": "not_checked",
        "model_processing": "not_checked",
    }
    if not config.path("state.json").exists():
        return result
    try:
        state = _state(config)
    except (InvalidRequest, OSError, KeyError, ValueError):
        result["state"] = "invalid"
        return result
    result["state"] = "valid"
    credential = None
    try:
        _template(config, state)
        environment = runtime_environment(config)
        personal = state["resources"]["personal"]
        token_path = Path(personal["token_file"])
        credential = _secret(token_path, expected_hash=_managed_hash(state, token_path))
        result.update(
            state="valid", credentials="valid", daemon="running" if _running(config) else "stopped"
        )
    except (InvalidRequest, OSError, KeyError, ValueError):
        result["state"] = "invalid"
        result["credentials"] = "invalid"
    result.update(_doctor_inspect(config, state, credential))
    if result["state"] != "valid":
        return result
    try:
        docker = _docker(environment)
        result["graph"] = (
            _graph_health(docker, _compose(docker, config.path(_COMPOSE_FILE), state), environment)
            if _owned_graph(docker, state, environment)
            else "stopped"
        )
    except (InvalidRequest, OSError):
        result["graph"] = "unavailable_or_unowned"
    if result["daemon"] != "running":
        # Another listener may reuse the loopback port after our daemon exits.
        # Never disclose its private credential merely to diagnose that listener.
        result["api"] = "daemon_stopped"
        return result
    if result["authority"] != "valid":
        # Diagnose readiness only after establishing the local storage identity.
        result["api"] = "authority_unverified"
        return result
    try:
        # The daemon may exit and its loopback port may be reused after the lock
        # check. Health probes therefore never include any private credential and
        # never claim to establish the HTTP peer's identity.
        with httpx.Client(
            base_url=config.url, trust_env=False, follow_redirects=False, timeout=2
        ) as client:
            health = client.get("/healthz")
            ready = client.get("/readyz")
        result["api"] = (
            "health_ready" if health.status_code == ready.status_code == 200 else "not_ready"
        )
    except (httpx.HTTPError, TypeError, ValueError, AttributeError):
        result["api"] = "unavailable"
    return result
