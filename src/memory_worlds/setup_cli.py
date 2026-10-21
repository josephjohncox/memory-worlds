"""Guided setup CLI dispatch; runtime and provisioning remain separate."""

from __future__ import annotations

import argparse
from pathlib import Path

from .errors import MemoryError
from .setup_config import SetupConfig, load_config
from .setup_files import check_path, private_read


def add_setup_parser(subparsers) -> None:
    parser = subparsers.add_parser("setup", help="configure and manage local or hybrid memory")
    default = Path.home() / ".config/memory-worlds/default/setup.toml"
    parser.add_argument("--config", type=Path, default=default)
    operations = parser.add_subparsers(dest="setup_command")
    help_text = {
        "init": "interactively create a private setup configuration",
        "validate": "validate configuration and referenced files without changing state",
        "plan": "show the configured resources and setup effects",
        "connect": "authenticate and select corporate access as an ordinary user",
        "apply": "create or recover local identities, private credentials, and profiles",
        "run": "start the owned graph and run the local API in the foreground",
        "doctor": "inspect configured identity, resources, and readiness",
        "down": "stop the owned graph after the foreground API has stopped",
    }
    for name, help_value in help_text.items():
        operation = operations.add_parser(name, help=help_value)
        operation.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if name in ("apply", "init"):
            operation.add_argument("--adopt-admin-file", type=Path)


def validate_setup(config: SetupConfig, *, adopt_admin_file: Path | None = None) -> dict:
    adoption = adopt_admin_file if adopt_admin_file is not None else config.adopt_admin_file
    if adoption is not None:
        private_read(check_path(adoption), maximum=256)
    for settings in (config.extraction, config.embedding):
        if settings.api_key_file is not None:
            content = private_read(settings.api_key_file, maximum=16386)
            try:
                key = content.decode("ascii").strip()
            except UnicodeError:
                raise MemoryError("provider key file must contain an ASCII credential") from None
            if not key or any(c.isspace() or not c.isprintable() for c in key):
                raise MemoryError("provider key file must contain one nonblank credential")
    if config.corporate and config.corporate.token_file:
        private_read(config.corporate.token_file, maximum=16386)
    return {"valid": True, "instance": config.instance, "config_digest": config.digest}


def plan_setup(config: SetupConfig) -> dict:
    return {
        "instance": config.instance,
        "mode": config.mode,
        "config_digest": config.digest,
        "local": {
            "url": config.url,
            "data_dir": str(config.data_dir),
            "state_dir": str(config.state_dir),
            "graph": "FalkorDB, owned Docker Compose project, persistent volume",
            "graph_port": config.graph_port,
            "journal": "SQLite",
            "authority": "adopt existing" if config.adopt_admin_file else "new or setup-owned",
            "worlds": ["personal"] + (["private work"] if config.mode == "hybrid" else []),
            "credentials": ["local operator", "personal agent"]
            + (["work agent"] if config.mode == "hybrid" else []),
        },
        "models": {
            name: {
                "provider": settings.provider,
                "model": settings.model,
                "base_url": settings.base_url,
                "key_file": str(settings.api_key_file) if settings.api_key_file else None,
            }
            for name, settings in (
                ("extraction", config.extraction),
                ("embedding", config.embedding),
            )
        },
        "corporate": {
            "url": config.corporate.url,
            "selected_world_ids": config.corporate.world_ids,
            "mount_access": "reader, exact local work credential only",
            "publication": False,
        }
        if config.corporate
        else None,
        "apply": "requires stopped local API; retains private credentials and canonical IDs",
        "run": "foreground API; Ctrl-C leaves owned graph running; down retains graph data",
        "profiles": {"pi": config.pi_profiles, "mcp": config.mcp_profiles},
        "authentication_effects": (
            "corporate connect and authenticated probes can establish sessions and reconcile "
            "the user's private work identity and group memberships"
        )
        if config.corporate
        else None,
    }


def run_setup_command(args):
    command = args.setup_command
    if command in (None, "init"):
        from .setup_interactive import wizard

        config = wizard(args.config)
        print(f"Configuration saved to {args.config.expanduser().absolute()}")
        try:
            answer = input("Apply the local setup now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "no"
        if answer not in ("y", "yes"):
            return {"configured": True, "instance": config.instance, "next": "setup apply"}
        from .setup_provision import apply_setup

        adoption = getattr(args, "adopt_admin_file", None) or config.adopt_admin_file
        validate_setup(config, adopt_admin_file=adoption)
        return apply_setup(config, adopt_admin_file=adoption)
    config = load_config(args.config)
    if command == "validate":
        return validate_setup(config)
    if command == "plan":
        return plan_setup(config)
    if command == "connect":
        from .setup_interactive import connect_setup

        return connect_setup(config)
    if command == "apply":
        from .setup_provision import apply_setup

        adoption = args.adopt_admin_file or config.adopt_admin_file
        validate_setup(config, adopt_admin_file=adoption)
        return apply_setup(config, adopt_admin_file=adoption)
    if command == "run":
        from .setup_runtime import run_setup

        return run_setup(config)
    if command == "doctor":
        from .setup_runtime import doctor_setup

        return doctor_setup(config)
    if command == "down":
        from .setup_runtime import down_setup

        return down_setup(config)
    raise MemoryError("unknown setup operation")
