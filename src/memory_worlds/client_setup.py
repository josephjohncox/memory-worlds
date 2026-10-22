"""Export explicit harness connections without editing global agent settings."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from .errors import Conflict, MemoryError
from .models import ScopeSelection
from .setup_config import load_config
from .setup_files import check_path, load_state, private_read, write_private


def add_clients_parser(subparsers) -> None:
    clients = subparsers.add_parser("clients", help="export connections for agent harnesses")
    commands = clients.add_subparsers(dest="clients_command", required=True)
    for name in ("export", "profile"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True, help="applied setup TOML")
        command.add_argument(
            "--profile", choices=("personal", "work", "corporate"), default="personal"
        )
        command.add_argument("--output", type=Path, required=True, help="new private output file")
        if name == "export":
            command.add_argument("--client", choices=("codex", "claude", "opencode"), required=True)


def connection(config_file: Path, profile: str) -> dict:
    """Use only the selected routine credential, never the local operator token."""
    config = load_config(config_file)
    state = load_state(config)
    if profile == "corporate":
        from .setup_provision import _corporate

        corporate = state.get("corporate")
        if config.corporate is None or not corporate or corporate["url"] != config.corporate.url:
            raise MemoryError("this setup has no applied direct corporate connection")
        token_file = config.path("corporate.token")
        if corporate["token_file"] != str(token_file):
            raise Conflict("corporate credential path differs from this installation")
        if _corporate(config) != corporate:
            raise Conflict("corporate connection differs from the applied identity")
        return {"url": corporate["url"], "tokenFile": str(token_file), "readScopes": []}
    resource = state["resources"].get(profile)
    if resource is None:
        raise MemoryError("the selected profile is not part of this applied installation")
    token_file = config.path(f"{profile}.token")
    if resource["token_file"] != str(token_file):
        raise Conflict("agent credential path differs from this installation")
    expected = state["files"].get(str(token_file))
    content = private_read(token_file, maximum=16386, managed=True)
    if expected is None or hashlib.sha256(content).hexdigest() != expected:
        raise Conflict("setup agent credential changed outside setup")
    selection = ScopeSelection(world_id=resource["world_id"], scope_id=resource["scope_id"])
    result = {
        "url": config.url,
        "tokenFile": str(token_file),
        "readScopes": [selection.model_dump(mode="json")],
    }
    if profile == "personal":
        result["writeScope"] = result["readScopes"][0]
    return result


def mcp_fragment(client: str, profile: dict) -> bytes:
    # Keep the venv path: resolving its interpreter symlink loses site-packages.
    python = str(Path(sys.executable).absolute())
    arguments = [
        "-I",
        "-m",
        "memory_worlds.cli",
        "--url",
        profile["url"],
        "--token-file",
        profile["tokenFile"],
        "mcp",
    ]
    if client == "codex":
        return (
            "[mcp_servers.memory-worlds]\n"
            + "command = "
            + json.dumps(python, ensure_ascii=False)
            + "\n"
            + "args = "
            + json.dumps(arguments, ensure_ascii=False)
            + "\n"
        ).encode()
    if client == "claude":
        result = {"mcpServers": {"memory-worlds": {"command": python, "args": arguments}}}
    elif client == "opencode":
        result = {
            "mcp": {
                "memory-worlds": {
                    "type": "local",
                    "command": [python, *arguments],
                    "enabled": True,
                }
            }
        }
    else:
        raise MemoryError("unknown client configuration format")
    return (json.dumps(result, indent=2) + "\n").encode()


def run_clients_command(args) -> dict:
    profile = connection(args.config, args.profile)
    if args.clients_command == "export":
        content = mcp_fragment(args.client, profile)
    elif args.clients_command == "profile":
        content = (json.dumps(profile, indent=2) + "\n").encode()
    else:
        raise MemoryError("unknown client setup operation")
    output = check_path(args.output)
    write_private(output, content)
    return {
        "output": str(output),
        "profile": args.profile,
        "format": args.client if args.clients_command == "export" else "native",
        "global_settings_changed": False,
        "automatic_capture": False,
    }
