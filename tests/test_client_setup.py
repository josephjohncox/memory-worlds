"""Harness exports contain file references, never secrets or global mutations."""

import json
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from memory_worlds.cli import parser, run
from memory_worlds.client_setup import mcp_fragment
from memory_worlds.errors import MemoryError
from memory_worlds.setup_config import SetupConfig, save_config
from memory_worlds.setup_files import private_read
from memory_worlds.setup_provision import apply_setup


@pytest.fixture
def installation(tmp_path):
    provider = {
        "provider": "openai-compatible",
        "model": "local",
        "base_url": "http://localhost:9/v1",
    }
    config = SetupConfig(
        data_dir=tmp_path / "source",
        state_dir=tmp_path / "state",
        extraction=provider,
        embedding=provider,
    )
    path = tmp_path / "config" / "setup $HOME `literal`.toml"
    save_config(config, path)
    apply_setup(config)
    return config, path


@pytest.mark.parametrize("client", ["codex", "claude", "opencode"])
def test_mcp_exports_are_literal_private_connections(installation, tmp_path, client, capsys):
    config, path = installation
    output = tmp_path / "export" / f"{client}.config"
    run(
        parser().parse_args(
            [
                "clients",
                "export",
                "--client",
                client,
                "--config",
                str(path),
                "--output",
                str(output),
            ]
        )
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["global_settings_changed"] is False
    assert receipt["automatic_capture"] is False
    raw = output.read_bytes()
    for name in ("personal.token", "operator.token", "graph.token"):
        assert private_read(config.path(name)).strip() not in raw
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    payload = tomllib.loads(raw.decode()) if client == "codex" else json.loads(raw)
    if client == "codex":
        server = payload["mcp_servers"]["memory-worlds"]
        command = [server["command"], *server["args"]]
    elif client == "claude":
        server = payload["mcpServers"]["memory-worlds"]
        command = [server["command"], *server["args"]]
    else:
        server = payload["mcp"]["memory-worlds"]
        assert server["type"] == "local" and server["enabled"] is True
        command = server["command"]
    assert command[:4] == [str(Path(sys.executable).absolute()), "-I", "-m", "memory_worlds.cli"]
    assert command[-3:] == ["--token-file", str(config.path("personal.token")), "mcp"]
    assert "operator.token" not in str(command)
    before = output.read_bytes()
    with pytest.raises(MemoryError):
        run(
            parser().parse_args(
                [
                    "clients",
                    "export",
                    "--client",
                    client,
                    "--config",
                    str(path),
                    "--output",
                    str(output),
                ]
            )
        )
    assert output.read_bytes() == before


def test_native_profile_has_separate_private_read_and_write_selection(
    installation, tmp_path, capsys
):
    config, path = installation
    output = tmp_path / "export" / "native.json"
    run(
        parser().parse_args(
            [
                "clients",
                "profile",
                "--config",
                str(path),
                "--output",
                str(output),
            ]
        )
    )
    capsys.readouterr()
    profile = json.loads(output.read_bytes())
    assert set(profile) == {"url", "tokenFile", "readScopes", "writeScope"}
    assert profile["readScopes"] == [profile["writeScope"]]
    assert profile["tokenFile"] == str(config.path("personal.token"))
    assert not any(key in profile for key in ("automaticCapture", "sharing", "retrieval"))


def test_codex_export_preserves_non_bmp_runtime_and_token_paths(monkeypatch):
    runtime = "/tmp/工具 🐍/bin/python"
    token = "/tmp/私有 🧠/agent.token"
    monkeypatch.setattr(sys, "executable", runtime)
    fragment = mcp_fragment("codex", {"url": "http://127.0.0.1:8787", "tokenFile": token})
    server = tomllib.loads(fragment.decode())["mcp_servers"]["memory-worlds"]
    assert server["command"] == runtime
    assert server["args"][-3:] == ["--token-file", token, "mcp"]


@pytest.mark.parametrize("profile", ["work", "corporate"])
def test_absent_profiles_do_not_create_an_output(installation, tmp_path, profile):
    _, path = installation
    output = tmp_path / "export" / "absent.json"
    with pytest.raises(MemoryError):
        run(
            parser().parse_args(
                [
                    "clients",
                    "profile",
                    "--config",
                    str(path),
                    "--profile",
                    profile,
                    "--output",
                    str(output),
                ]
            )
        )
    assert not output.exists()


def test_changed_agent_token_is_refused_without_exposing_it(installation, tmp_path):
    config, path = installation
    config.path("personal.token").write_text("PRIVATE-CHANGED-CREDENTIAL\n")
    output = tmp_path / "export" / "native.json"
    with pytest.raises(MemoryError, match="changed outside setup"):
        run(
            parser().parse_args(
                [
                    "clients",
                    "profile",
                    "--config",
                    str(path),
                    "--output",
                    str(output),
                ]
            )
        )
    assert not output.exists()


def test_claude_plugin_manifest_and_mcp_form_one_connection():
    root = Path(__file__).resolve().parents[1] / "plugins/claude"
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_bytes())
    mcp = json.loads((root / ".mcp.json").read_bytes())["mcpServers"]
    assert manifest["name"] == "memory-worlds"
    assert manifest["license"] == "Apache-2.0"
    assert set(mcp) == {"memory-worlds"}
    assert mcp["memory-worlds"]["command"] == "${user_config.runtime_python}"
    assert mcp["memory-worlds"]["args"][-1] == "mcp"
    assert "hooks" not in manifest
    assert "runtime_python" in manifest["userConfig"]


def test_export_command_runs_without_network_or_admin_token(installation, tmp_path):
    _, path = installation
    output = tmp_path / "export" / "codex.toml"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "memory_worlds.cli",
            "clients",
            "export",
            "--client",
            "codex",
            "--config",
            str(path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["format"] == "codex"
