"""Recover an explicitly approved first-run wizard without journaling secrets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from getpass import GetPassWarning
from pathlib import Path

from pydantic import ValidationError

from .errors import MemoryError
from .setup_config import SetupConfig, config_bytes
from .setup_files import check_path, private_read, unlink_private, write_private


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def intent_path(output: Path) -> Path:
    return output.with_name(output.name + ".wizard.json")


def validate_output(output: Path, config: SetupConfig) -> None:
    managed = {
        config.path(name)
        for name in (
            "setup.lock",
            "provision.json",
            "state.json",
            "operator.token",
            "personal.token",
            "work.token",
            "graph.token",
            "extraction.key",
            "embedding.key",
            "compose.graph.yaml",
            "mounts.json",
            "connection.json",
            "corporate.json",
            "corporate.token",
            "pi-personal.json",
            "pi-work.json",
            "pi-corporate.json",
            "mcp-personal.json",
            "mcp-work.json",
        )
    }
    if (
        output == config.data_dir
        or config.data_dir in output.parents
        or output in config.data_dir.parents
        or output == config.state_dir
        or output in config.state_dir.parents
        or any(path == output or path in output.parents for path in managed)
    ):
        raise MemoryError("Wizard configuration path conflicts with managed state or source data")


def hidden_key(secret_fn, label: str) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            secret = secret_fn(label + " API key (hidden): ")
    except GetPassWarning as error:
        raise MemoryError(
            "Hidden key entry is unavailable; choose an existing private API key file"
        ) from error
    except (EOFError, KeyboardInterrupt) as error:
        raise MemoryError("Setup cancelled; no daemon was started") from error
    if (
        not isinstance(secret, str)
        or not secret
        or len(secret) > 16384
        or any(not c.isascii() or c.isspace() or not c.isprintable() for c in secret)
    ):
        raise MemoryError("API key must be a bounded single-line value")
    return (secret + "\n").encode()


def begin(output: Path, config: SetupConfig, pending: dict[Path, bytes], identity: dict | None):
    """Record approval before any provider file, TOML or connection is published."""
    validate_output(output, config)
    value = {
        "phase": "prepared",
        "output": str(output),
        "config": config.model_dump(mode="json"),
        "config_sha256": _hash(config_bytes(config)),
        "identity": identity,
        "keys": [{"path": str(path), "sha256": _hash(data)} for path, data in pending.items()],
    }
    write_private(intent_path(output), _encode(value))
    return value


def load(output: Path) -> tuple[dict, SetupConfig] | None:
    path = intent_path(output)
    if not os.path.lexists(path):
        return None
    try:
        value = json.loads(private_read(path, managed=True))
        if (
            not isinstance(value, dict)
            or set(value) != {"phase", "output", "config", "config_sha256", "identity", "keys"}
            or value.get("phase") not in ("prepared", "complete")
            or value.get("output") != str(output)
        ):
            raise ValueError
        config = SetupConfig.model_validate(value["config"])
        validate_output(output, config)
        if value["config_sha256"] != _hash(config_bytes(config)):
            raise ValueError
        identity = value["identity"]
        if config.mode == "hybrid":
            if (
                not isinstance(identity, dict)
                or set(identity) != {"authority_id", "user_id", "credential_id"}
                or any(not isinstance(v, str) or not 1 <= len(v) <= 2000 for v in identity.values())
            ):
                raise ValueError
        elif identity is not None:
            raise ValueError
        keys = value["keys"]
        allowed = {
            str(config.path(kind + ".key"))
            for kind, provider in (
                ("extraction", config.extraction),
                ("embedding", config.embedding),
            )
            if provider.api_key_file == config.path(kind + ".key")
        }
        if not isinstance(keys, list) or len(keys) > 2:
            raise ValueError
        seen = set()
        for key in keys:
            if (
                not isinstance(key, dict)
                or set(key) != {"path", "sha256"}
                or not isinstance(key["path"], str)
                or key["path"] not in allowed
                or key["path"] in seen
                or not isinstance(key["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", key["sha256"]) is None
            ):
                raise ValueError
            seen.add(key["path"])
        return value, config
    except (ValueError, TypeError, KeyError, RecursionError, ValidationError):
        raise MemoryError("Wizard intent is invalid or belongs to another output path") from None


def publish(output: Path, value: dict, config: SetupConfig, pending: dict[Path, bytes], secret_fn):
    """Resume hash-bound files; missing unjournaled key bytes must be re-entered."""
    if os.path.lexists(output):
        if _hash(private_read(output, managed=True)) != value["config_sha256"]:
            raise MemoryError("Wizard configuration changed outside its approved intent")
    elif value["phase"] == "complete":
        raise MemoryError("Completed wizard configuration is missing")
    # Check every existing output before collecting secrets or publishing missing files.
    for key in value["keys"]:
        path = check_path(Path(key["path"]))
        if os.path.lexists(path):
            if _hash(private_read(path, maximum=16386, managed=True)) != key["sha256"]:
                raise MemoryError("Wizard provider file changed outside its approved intent")
        elif value["phase"] == "complete":
            raise MemoryError("Completed wizard provider file is missing")
    for key in value["keys"]:
        path = check_path(Path(key["path"]))
        if not os.path.lexists(path):
            data = pending.get(path)
            if data is None:
                data = hidden_key(secret_fn, "Resume " + path.stem.title())
            if _hash(data) != key["sha256"]:
                raise MemoryError(
                    "Re-entered provider key does not match the approved wizard intent"
                )
            write_private(path, data)
    if not os.path.lexists(output):
        write_private(output, config_bytes(config))


def complete(output: Path, value: dict):
    path = intent_path(output)
    before = _encode(value)
    if value["phase"] != "complete":
        value["phase"] = "complete"
        write_private(path, _encode(value), expected_hash=_hash(before))
    unlink_private(path, expected_hash=_hash(_encode(value)))
