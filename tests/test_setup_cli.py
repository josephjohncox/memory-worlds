"""Exercise setup through the installed CLI without ambient authority settings."""

import json
import os
import subprocess
import sys

import pytest

from memory_worlds.cli import parser, run
from memory_worlds.setup_config import SetupConfig, save_config


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
