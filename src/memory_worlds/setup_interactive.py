"""Interactive configuration and ordinary-user corporate connection.

The wizard does not initialize, start, or administer either authority. Corporate
connection requests metadata and retains one validated user token. Authentication
can establish sessions and reconcile the user's source-side memberships.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from collections.abc import Callable
from getpass import GetPassWarning, getpass
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .errors import MemoryError
from .login import device_login
from .models import World, WorldKind
from .setup_config import CorporateSettings, ProviderSettings, SetupConfig, save_config
from .setup_files import (
    check_path,
    ensure_private_dir,
    private_read,
    setup_lock,
    unlink_private,
    write_private,
)

_BINDING_FIELDS = {
    "url",
    "authority_id",
    "user_id",
    "credential_id",
    "world_ids",
    "token_file",
    "token_sha256",
}
_SHARED = {WorldKind.TEAM, WorldKind.COMPANY, WorldKind.AD_HOC}
_INTENT_FIELDS = {
    "phase",
    "instance",
    "url",
    "before_binding",
    "after_binding",
    "before_binding_sha256",
    "after_binding_sha256",
    "before_token_sha256",
    "after_token_sha256",
    "staged_token_file",
}


def _json(data: bytes) -> object:
    try:
        return json.loads(data)
    except (ValueError, RecursionError, UnicodeError) as error:
        raise MemoryError("Corporate connection returned invalid JSON") from error


def _encode(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _token(data: bytes) -> str:
    try:
        value = data.decode("ascii").strip()
    except UnicodeError as error:
        raise MemoryError("Corporate token file contains an invalid bearer token") from error
    if not 1 <= len(value) <= 16384 or any(not c.isprintable() or c.isspace() for c in value):
        raise MemoryError("Corporate token file contains an invalid bearer token")
    return value


def _get(client: httpx.Client, path: str) -> object:
    try:
        with client.stream("GET", path) as response:
            if response.status_code != 200:
                raise MemoryError(f"Corporate discovery returned HTTP {response.status_code}")
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > 4 * 1024 * 1024:
                    raise MemoryError("Corporate discovery response exceeds four MiB")
                data.extend(chunk)
    except httpx.HTTPError as error:
        raise MemoryError("Corporate discovery request failed") from error
    return _json(bytes(data))


def _discover(url: str, token: str) -> tuple[dict, list[World]]:
    with httpx.Client(
        base_url=url,
        headers={"Authorization": "Bearer " + token},
        timeout=10,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        identity = _get(client, "/v1/me")
        if not isinstance(identity, dict) or any(
            not isinstance(identity.get(name), str) or not 1 <= len(identity[name]) <= 2000
            for name in ("authority_id", "user_id", "credential_id")
        ):
            raise MemoryError("Corporate service returned an invalid identity")
        if identity.get("admin") is True:
            raise MemoryError(
                "Connect with an ordinary corporate user token, not an operator token"
            )
        raw = _get(client, "/v1/worlds")
        if not isinstance(raw, list) or len(raw) > 10000:
            raise MemoryError("Corporate service returned an invalid world catalog")
        try:
            worlds = [World.model_validate(item) for item in raw]
        except ValidationError as error:
            raise MemoryError("Corporate service returned an invalid world catalog") from error
        if len({world.id for world in worlds}) != len(worlds) or any(
            world.authority_id != identity["authority_id"] for world in worlds
        ):
            raise MemoryError("Corporate world catalog does not match the connected authority")
        return identity, worlds


def _binding(config: SetupConfig) -> tuple[dict | None, bytes | None, bytes | None]:
    path, token_path = config.path("corporate.json"), config.path("corporate.token")
    if not os.path.lexists(path):
        if os.path.lexists(token_path):
            raise MemoryError("An unbound corporate token already occupies the managed path")
        return None, None, None
    data = private_read(path)
    value = _json(data)
    if not isinstance(value, dict) or set(value) != _BINDING_FIELDS:
        raise MemoryError("Corporate connection binding is invalid")
    if value.get("url") != config.corporate.url or value.get("token_file") != str(token_path):
        raise MemoryError("Corporate connection URL or managed token path changed")
    previous = private_read(token_path, maximum=16386)
    if value.get("token_sha256") != _hash(previous):
        raise MemoryError("Managed corporate token does not match its connection binding")
    return value, data, previous


def _valid_binding(value: object, config: SetupConfig) -> bool:
    if not isinstance(value, dict) or set(value) != _BINDING_FIELDS:
        return False
    worlds = value.get("world_ids")
    return (
        value.get("url") == config.corporate.url
        and value.get("token_file") == str(config.path("corporate.token"))
        and all(
            isinstance(value.get(key), str) and 1 <= len(value[key]) <= 2000
            for key in ("authority_id", "user_id", "credential_id")
        )
        and isinstance(value.get("token_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["token_sha256"]) is not None
        and isinstance(worlds, list)
        and len(worlds) <= 50
        and all(
            isinstance(item, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", item) for item in worlds
        )
        and len(worlds) == len(set(worlds))
    )


def _connection_intent(config: SetupConfig, data: bytes) -> dict:
    value = _json(data)
    if isinstance(value, dict) and value.get("url") != config.corporate.url:
        raise MemoryError("Corporate connection URL changed; use a separate setup instance")
    if (
        not isinstance(value, dict)
        or set(value) != _INTENT_FIELDS
        or not isinstance(value.get("phase"), str)
        or value["phase"] not in {"prepared", "complete", "abandoned"}
        or value.get("instance") != config.instance
        or value.get("url") != config.corporate.url
        or not _valid_binding(value.get("after_binding"), config)
    ):
        raise MemoryError("Corporate connection journal is invalid or belongs to another setup")
    before, after = value["before_binding"], value["after_binding"]
    if before is not None and (
        not _valid_binding(before, config)
        or any(before[key] != after[key] for key in ("authority_id", "user_id"))
    ):
        raise MemoryError("Corporate connection journal changes its pinned identity")
    for label, binding in (("before", before), ("after", after)):
        expected_binding = _hash(_encode(binding)) if binding is not None else None
        expected_token = binding["token_sha256"] if binding is not None else None
        if (
            value[f"{label}_binding_sha256"] != expected_binding
            or value[f"{label}_token_sha256"] != expected_token
        ):
            raise MemoryError("Corporate connection journal hashes do not match its binding")
    stage = value["staged_token_file"]
    if stage is not None and (
        not isinstance(stage, str)
        or not re.fullmatch(r"corporate-login-[0-9a-f]{32}\.token", stage)
    ):
        raise MemoryError("Corporate connection journal staging path is invalid")
    return value


def _cleanup_staging(config: SetupConfig, intent: dict) -> None:
    if intent["staged_token_file"] is None:
        return
    stage = config.path(intent["staged_token_file"])
    if os.path.lexists(stage):
        unlink_private(stage, expected_hash=intent["after_token_sha256"])


def _recover_connection(config: SetupConfig) -> bytes | None:
    """Finish only an fsynced, validated intent; refuse unrelated state changes."""
    path = config.path("connection.json")
    if not os.path.lexists(path):
        if os.path.lexists(config.path("corporate.json")):
            raise MemoryError("Corporate connection journal is missing; refusing unowned binding")
        return None
    data = private_read(path)
    intent = _connection_intent(config, data)
    token_path, binding_path = config.path("corporate.token"), config.path("corporate.json")
    token = private_read(token_path, maximum=16386) if os.path.lexists(token_path) else None
    binding = private_read(binding_path) if os.path.lexists(binding_path) else None
    token_hash = _hash(token) if token is not None else None
    binding_hash = _hash(binding) if binding is not None else None
    before, after = intent["before_binding"], intent["after_binding"]
    before_hash, after_hash = intent["before_binding_sha256"], intent["after_binding_sha256"]
    if token_hash not in {
        intent["before_token_sha256"],
        intent["after_token_sha256"],
    } or binding_hash not in {before_hash, after_hash}:
        raise MemoryError("Corporate connection files changed outside their durable intent")
    if intent["phase"] != "prepared":
        label = "after" if intent["phase"] == "complete" else "before"
        if (token_hash, binding_hash) != (
            intent[f"{label}_token_sha256"],
            intent[f"{label}_binding_sha256"],
        ):
            raise MemoryError("Corporate connection files do not match their completed journal")
        _cleanup_staging(config, intent)
        return data
    if token_hash == intent["after_token_sha256"]:
        if binding_hash != after_hash:
            write_private(binding_path, _encode(after), expected_hash=binding_hash)
        intent["phase"] = "complete"
    else:
        # A failed write may have restored the old token after publishing new metadata.
        if binding_hash != before_hash:
            if before is None:
                unlink_private(binding_path, expected_hash=after_hash)
            else:
                write_private(binding_path, _encode(before), expected_hash=binding_hash)
        intent["phase"] = "abandoned"
    recovered = _encode(intent)
    write_private(path, recovered, expected_hash=_hash(data))
    _cleanup_staging(config, intent)
    return recovered


def _connect(
    config: SetupConfig, *, choose_worlds: Callable[[dict, list[World]], list[str]] | None = None
) -> dict:
    if config.mode != "hybrid" or config.corporate is None:
        raise MemoryError("Corporate connection requires a hybrid setup configuration")
    ensure_private_dir(config.state_dir)
    with setup_lock(config):
        previous_intent = _recover_connection(config)
        old, old_binding, old_token = _binding(config)
        stage = config.path(f"corporate-login-{uuid4().hex}.token")
        stage_hash = None
        try:
            corporate = config.corporate
            if corporate.token_file is not None:
                token = _token(private_read(corporate.token_file, maximum=16386))
            else:
                if not corporate.issuer or not corporate.client_id:
                    raise MemoryError("Corporate device login needs an issuer and native client ID")
                device_login(
                    corporate.url,
                    issuer=corporate.issuer,
                    client_id=corporate.client_id,
                    scopes=tuple(corporate.scopes),
                    token_output=stage,
                )
                candidate = private_read(stage, maximum=16386)
                stage_hash = _hash(candidate)
                token = _token(candidate)
            identity, worlds = _discover(corporate.url, token)
            if old and any(old.get(key) != identity[key] for key in ("authority_id", "user_id")):
                raise MemoryError(
                    "Corporate authority or user changed; use a separate setup instance"
                )
            shared = [world for world in worlds if world.kind in _SHARED]
            selected = list(corporate.world_ids)
            if choose_worlds is not None:
                selected = choose_worlds(identity, shared)
            available = {world.id for world in shared}
            if (
                len(selected) > 50
                or len(set(selected)) != len(selected)
                or any(world_id not in available for world_id in selected)
            ):
                raise MemoryError("Corporate mount selection requires accessible shared world IDs")
            # Selection comes only from user/config input, never from new catalog entries.
            corporate.world_ids = selected
            stored = (token + "\n").encode("ascii")
            result = {
                "url": corporate.url,
                **{key: identity[key] for key in ("authority_id", "user_id", "credential_id")},
                "world_ids": selected,
                "token_file": str(config.path("corporate.token")),
                "token_sha256": _hash(stored),
            }
            token_path, binding_path = config.path("corporate.token"), config.path("corporate.json")
            intent = {
                "phase": "prepared",
                "instance": config.instance,
                "url": corporate.url,
                "before_binding": old,
                "after_binding": result,
                "before_binding_sha256": _hash(old_binding) if old_binding is not None else None,
                "after_binding_sha256": _hash(_encode(result)),
                "before_token_sha256": _hash(old_token) if old_token is not None else None,
                "after_token_sha256": _hash(stored),
                "staged_token_file": stage.name if corporate.token_file is None else None,
            }
            prepared = _encode(intent)
            write_private(
                config.path("connection.json"),
                prepared,
                expected_hash=_hash(previous_intent) if previous_intent is not None else None,
            )
            token_hash = write_private(
                token_path,
                stored,
                expected_hash=_hash(old_token) if old_token is not None else None,
            )
            try:
                write_private(
                    binding_path,
                    _encode(result),
                    expected_hash=_hash(old_binding) if old_binding is not None else None,
                )
            except BaseException:
                # Both files are managed and locked; leave the prior valid pair on failure.
                if old_token is not None:
                    write_private(token_path, old_token, expected_hash=token_hash)
                elif _hash(private_read(token_path)) == token_hash:
                    unlink_private(token_path, expected_hash=token_hash)
                raise
            intent["phase"] = "complete"
            write_private(
                config.path("connection.json"), _encode(intent), expected_hash=_hash(prepared)
            )
            return result
        finally:
            # Device login creates this exclusive private staging file, never a supplied path.
            if stage_hash is not None and os.path.lexists(stage):
                unlink_private(stage, expected_hash=stage_hash)


def connect_setup(config: SetupConfig) -> dict:
    """Validate a user's selected corporate worlds and atomically rotate its managed token."""
    return _connect(config)


def wizard(output_path: Path, *, input_fn=input, secret_fn=getpass, output_fn=print) -> SetupConfig:
    """Save a setup configuration; never initialize or start a daemon."""
    output_path = check_path(output_path)
    if os.path.lexists(output_path):
        raise MemoryError("Setup configuration already exists; choose a new output path")
    pending: dict[Path, bytes] = {}

    def ask(label: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        try:
            answer = input_fn(label + suffix + ": ").strip()
        except (EOFError, KeyboardInterrupt) as error:
            raise MemoryError("Setup cancelled; no daemon was started") from error
        if not answer and default is not None:
            return default
        if not answer:
            raise MemoryError(f"{label} is required")
        return answer

    def path(label: str, default: str | None = None) -> Path:
        return Path(ask(label, default)).expanduser().absolute()

    def choice(label: str, options: tuple[str, ...], default: str) -> str:
        value = ask(f"{label} ({'/'.join(options)})", default)
        if value not in options:
            raise MemoryError(f"{label} requires one of: {', '.join(options)}")
        return value

    output_fn("Setup creates configuration only. Applying it and starting the daemon are separate.")
    mode = choice("Memory setup", ("local", "hybrid"), "local")
    instance = ask("Instance name", "personal" if mode == "local" else "work")
    owner = ask("Local owner", "owner")
    data_dir = check_path(path("Local data directory", f"~/.local/share/memory-worlds/{instance}"))
    if data_dir.exists() and not data_dir.is_dir():
        raise MemoryError("Local data path must be a directory")
    existing = data_dir.exists() and any(data_dir.iterdir())
    authority_mode = choice("Local authority", ("new", "adopt"), "adopt" if existing else "new")
    if authority_mode == "new" and existing:
        raise MemoryError("A new local authority requires an empty data directory; choose adopt")
    adopt_admin_file = None
    if authority_mode == "adopt":
        if not existing or not os.path.lexists(check_path(data_dir / "control.sqlite3")):
            raise MemoryError("Adoption requires an existing local authority control file")
        adopt_admin_file = path("Existing local administrator private token file")
        try:
            administrator = private_read(adopt_admin_file, maximum=256).decode("ascii")
        except UnicodeError as error:
            raise MemoryError(
                "Local administrator file must contain an ASCII credential"
            ) from error
        if not re.fullmatch(r"mw_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}\n?", administrator):
            raise MemoryError("Local administrator file must contain one local API credential")
    state_dir = path("Private setup state directory", f"~/.local/state/memory-worlds/{instance}")
    if any(
        os.path.lexists(check_path(state_dir / name))
        for name in ("corporate.json", "connection.json")
    ):
        raise MemoryError(
            "This state directory already has a corporate connection; edit its setup configuration "
            "and run setup connect, or choose a new instance"
        )
    try:
        port = int(ask("Loopback API port", "8787"))
        graph_port = int(ask("Loopback FalkorDB port", "16379"))
    except ValueError as error:
        raise MemoryError("API and graph ports require integers between 1024 and 65535") from error
    if any(not 1024 <= value <= 65535 for value in (port, graph_port)) or port == graph_port:
        raise MemoryError("API and graph ports must differ and be between 1024 and 65535")

    def provider(kind: str) -> ProviderSettings:
        selected = choice(f"{kind} provider", ("openai-compatible", "openai"), "openai-compatible")
        model = ask(f"{kind} model (explicit provider model ID)")
        base = ask(f"{kind} base URL", "" if selected == "openai" else None) or None
        auth = choice(f"{kind} API key", ("none", "file", "new"), "none")
        key_file = None
        if auth == "file":
            key_file = path(f"{kind} private API key file")
            try:
                value = private_read(key_file, maximum=16386).decode("ascii").strip()
            except UnicodeError as error:
                raise MemoryError("API key file must contain an ASCII credential") from error
            if not 1 <= len(value) <= 16384 or any(
                c.isspace() or not c.isprintable() for c in value
            ):
                raise MemoryError("API key file must contain one bounded single-line credential")
        elif auth == "new":
            consent = choice("Save a newly entered key in a private file", ("yes", "no"), "no")
            if consent != "yes":
                raise MemoryError("API key storage was not selected; rerun and choose none or file")
            key_file = state_dir / (kind.lower() + ".key")
            if os.path.lexists(key_file):
                raise MemoryError("API key path already exists; rerun and choose file")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", GetPassWarning)
                    secret = secret_fn(f"{kind} API key (hidden): ")
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
            pending[key_file] = (secret + "\n").encode()
        return ProviderSettings(
            provider=selected, model=model, base_url=base, api_key_file=key_file
        )

    extraction = provider("Extraction")
    embedding = provider("Embedding")
    try:
        dimensions = int(ask("Embedding dimensions", "1536"))
    except ValueError as error:
        raise MemoryError("Embedding dimensions must be a positive integer") from error
    pi_profiles = choice("Generate native Pi profiles", ("yes", "no"), "yes") == "yes"
    mcp_profiles = choice("Generate MCP client examples", ("yes", "no"), "no") == "yes"
    corporate = None
    if mode == "hybrid":
        output_fn(
            "Corporate login uses your own user identity; no corporate administrator is needed."
        )
        url = ask("Corporate Memory Worlds API URL")
        method = choice("Corporate authentication", ("device", "file"), "device")
        if method == "file":
            corporate = CorporateSettings(
                url=url, token_file=path("Corporate private user-token file")
            )
        else:
            corporate = CorporateSettings(
                url=url,
                issuer=ask("Trusted OAuth issuer URL"),
                client_id=ask("Native public OAuth client ID"),
                scopes=ask("OAuth scopes (space-separated)", "openid memory-worlds").split(),
            )
    config = SetupConfig(
        instance=instance,
        mode=mode,
        owner=owner,
        adopt_admin_file=adopt_admin_file,
        data_dir=data_dir,
        state_dir=state_dir,
        port=port,
        graph_port=graph_port,
        extraction=extraction,
        embedding=embedding,
        embedding_dimensions=dimensions,
        corporate=corporate,
        pi_profiles=pi_profiles,
        mcp_profiles=mcp_profiles,
    )

    def review(identity: dict | None = None, selected: list[str] | None = None) -> None:
        output_fn("Review setup configuration:")
        for label, value in (
            ("Configuration", output_path),
            ("Instance", config.instance),
            ("Local owner", config.owner),
            ("Local authority", authority_mode),
            ("Existing administrator file", config.adopt_admin_file or "none"),
            ("Local data", config.data_dir),
            ("Private state", config.state_dir),
            ("Local API", config.url),
            ("Loopback graph port", config.graph_port),
        ):
            output_fn(f"  {label}: {value}")
        for label, settings in (("Extraction", extraction), ("Embedding", embedding)):
            output_fn(f"  {label}: {settings.provider} / {settings.model}")
            output_fn(f"  {label} endpoint: {settings.base_url or 'provider default'}")
            output_fn(f"  {label} key file: {settings.api_key_file or 'none'}")
        output_fn(f"  Embedding dimensions: {config.embedding_dimensions}")
        output_fn(f"  Pi profiles: {pi_profiles}; MCP examples: {mcp_profiles}")
        output_fn(
            "  Personal and work credentials remain separate; corporate mounts are reader-only."
        )
        output_fn("  Pi automatic retrieval, capture and publication are off.")
        if config.mode == "hybrid":
            output_fn("  The research profile has no write target; personal notes stay local.")
        else:
            output_fn("  The personal profile's private write target stays on the local authority.")
        if config.corporate is not None:
            output_fn(f"  Corporate API: {config.corporate.url}")
            output_fn(f"  Corporate authority: {identity['authority_id']}")
            output_fn(f"  Corporate user: {identity['user_id']}")
            output_fn("  Corporate mounted world IDs: " + (", ".join(selected or []) or "none"))
            output_fn("  Corporate private scopes require the separate direct-corporate profile.")
        if authority_mode == "adopt":
            output_fn("  Existing local data detected. Setup never initializes over existing data.")
            output_fn(
                "  Apply authenticates the referenced local administrator "
                "while the daemon is stopped."
            )
        if choice("Save this configuration", ("yes", "no"), "no") != "yes":
            raise MemoryError("Setup cancelled before saving configuration")

    def select(identity: dict, worlds: list[World]) -> list[str]:
        output_fn("Select corporate shared worlds to mount. None are selected automatically.")
        for index, world in enumerate(worlds, 1):
            label = "".join(c for c in world.name if c.isprintable())
            alias = f" @{world.alias}" if world.alias else ""
            output_fn(f"{index}. {label}{alias} ({world.kind}) — {world.id}")
        if not worlds:
            output_fn(
                "No shared worlds are currently accessible. "
                "The direct corporate profile remains separate."
            )
            selected = []
        else:
            value = ask("World numbers, comma-separated; none keeps the mount empty", "none")
            if value == "none":
                selected = []
            else:
                try:
                    indices = [int(item.strip()) for item in value.split(",")]
                    if not indices or any(not 1 <= index <= len(worlds) for index in indices):
                        raise ValueError
                except ValueError as error:
                    raise MemoryError("Choose displayed corporate world numbers or none") from error
                selected = list(dict.fromkeys(worlds[index - 1].id for index in indices))
        review(identity, selected)
        return selected

    if corporate is not None:
        _connect(config, choose_worlds=select)
    else:
        review()
    for destination, data in pending.items():
        ensure_private_dir(destination.parent)
        write_private(destination, data)
    ensure_private_dir(output_path.parent)
    save_config(config, output_path)
    output_fn(f"Configuration saved to {output_path}. No daemon was initialized or started.")
    output_fn(
        "Generated Pi profiles keep automatic retrieval/capture/publication off; "
        "hybrid research has no default write target."
    )
    return config
