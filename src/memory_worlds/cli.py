import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import stat
import sys
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from .client import Client, scope_path
from .errors import MemoryError
from .identity_names import CreateIdentity, UpdateIdentity
from .lifecycle import LifecycleRequest, RetentionRequest
from .locking import DaemonLock
from .models import (
    BindContext,
    CreateScope,
    CreateToken,
    CreateWorld,
    DelegateCredential,
    EpisodeInput,
    Publication,
    Role,
    ScopeFlow,
    ScopeSelection,
    SearchRequest,
    UpdateAlias,
    UpdateScopeGrant,
    WorldKind,
)
from .operations import BackupRequest
from .outbox import RoutedPublication, RouterSearch


def emit(value):
    def encode(obj):
        if isinstance(obj, BaseModel):
            return obj.model_dump(mode="json")
        raise TypeError(type(obj).__name__)

    print(json.dumps(value, default=encode, indent=2))


def read_token(args) -> str:
    value = (
        args.token_file.read_text().strip()
        if args.token_file
        else os.getenv("MEMORY_WORLDS_TOKEN", "").strip()
    )
    if not value:
        raise MemoryError("set MEMORY_WORLDS_TOKEN or --token-file")
    return value


def content(args) -> str:
    return args.text if args.text is not None else sys.stdin.read(200_001)


def read_input(path: Path, model):
    """Load bounded structured input without putting credentials in command arguments."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_048_576:
            raise MemoryError("input must be a regular JSON file no larger than 1 MiB")
        value = source.read(1_048_577)
    if len(value) > 1_048_576:
        raise MemoryError("input exceeds 1 MiB")
    return model.model_validate_json(value)


def scope_selection(value: str) -> tuple[str, str]:
    try:
        world_id, selected = value.rsplit(":", 1)
        if not world_id or not selected:
            raise ValueError("empty selection")
        if not selected.startswith("@"):
            UUID(selected)
        return world_id, selected
    except (ValueError, ValidationError):
        raise argparse.ArgumentTypeError(
            "scope selection must be WORLD:UUID or WORLD:@ALIAS"
        ) from None


def scope_reference(value: str) -> UUID | str:
    try:
        if value.startswith("@"):
            UpdateAlias(alias=value[1:])
            return value
        return UUID(value)
    except (ValueError, ValidationError):
        raise argparse.ArgumentTypeError("scope must be a UUID or @alias") from None


def listing(items, kind: str):
    columns = {
        "worlds": (("ID", "id"), ("ALIAS", "alias"), ("NAME", "name"), ("KIND", "kind")),
        "scopes": (("UUID", "id"), ("ALIAS", "alias"), ("NAME", "name"), ("KIND", "kind")),
        "mounts": (("ID", "id"), ("NAME", "name"), ("WORLD_IDS", "world_ids"), ("URL", "url")),
    }[kind]
    print("\t".join(label for label, _ in columns))
    for item in items:
        row = item.model_dump(mode="json")
        values = []
        for _, key in columns:
            value = row.get(key)
            value = ",".join(value) if isinstance(value, list) else str(value or "-")
            if key == "alias" and value != "-":
                value = "@" + value
            values.append("".join(c if c.isprintable() else " " for c in value))
        print("\t".join(values))


def resolve_local_selection(store, principal, args):
    if args.world.startswith("@"):
        args.world = Client._alias(store.list_worlds(principal), args.world, "world").id
    if getattr(args, "scope", None) is not None:
        if str(args.scope).startswith("@"):
            args.scope = Client._alias(
                store.list_scopes(principal, args.world), args.scope, "scope"
            ).id
        else:
            args.scope = UUID(str(args.scope))


def delegate_to_file(client: Client, request: DelegateCredential, destination: Path):
    """Reserve a private file before issuance and never print the one-time token."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            result = client.delegate(request)
            if (
                not result.token
                or len(result.token) > 8192
                or any(c.isspace() for c in result.token)
            ):
                raise MemoryError("delegation returned an invalid credential")
            output.write(result.token + "\n")
            output.flush()
            os.fsync(output.fileno())
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {
        **result.model_dump(mode="json", exclude={"token"}),
        "token_file": str(destination),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory-worlds", description="Shared local agent memory")
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path(
            os.getenv("MEMORY_WORLDS_DATA_DIR", str(Path.home() / ".local/share/memory-worlds"))
        ),
    )
    p.add_argument("--url", default=os.getenv("MEMORY_WORLDS_URL", "http://127.0.0.1:8787"))
    p.add_argument("--token-file", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="initialize owner and print a one-time admin credential")
    init.add_argument("--user", default="owner")
    serve = sub.add_parser("serve", help="run the local HTTP daemon")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--allow-network", action="store_true")
    sub.add_parser("worlds", help="list accessible worlds and canonical IDs").add_argument(
        "--table", action="store_true"
    )
    discover = sub.add_parser("discover", help="discover authorized worlds, scopes and mounts")
    discover.add_argument("--world", help="world ID or @alias; includes that world's scopes")
    discover.add_argument(
        "--scopes", action="store_true", help="include accessible scopes in each world"
    )
    discover.add_argument("--json", action="store_true", help="machine-readable discovery")
    resolve = sub.add_parser("resolve", help="resolve an authorized @alias to a canonical ID")
    resolve.add_argument("kind", choices=["world", "scope", "mount"])
    resolve.add_argument("reference")
    resolve.add_argument("--world", help="world ID or @alias for scope resolution")
    login = sub.add_parser("login", help="sign in to an OIDC provider using a native device flow")
    login.add_argument("--issuer", required=True)
    login.add_argument("--client-id", required=True)
    login.add_argument("--scope", action="append")
    login.add_argument("--token-output", type=Path, required=True)
    sub.add_parser("me", help="show the authenticated identity and accessible worlds")
    scopes = sub.add_parser("scopes", help="list explicitly accessible scopes in a world")
    scopes.add_argument("world")
    scopes.add_argument("--table", action="store_true")
    scope = sub.add_parser("scope", help="inspect one authorized scope")
    scope.add_argument("world")
    scope.add_argument("scope", type=scope_reference)
    world_alias = sub.add_parser(
        "world-alias", help="set or clear a world's operator-managed alias"
    )
    world_alias.add_argument("world")
    world_alias.add_argument("alias", help="lowercase alias, or 'none' to clear")
    scope_alias = sub.add_parser("scope-alias", help="set or clear an authorized scope alias")
    scope_alias.add_argument("world")
    scope_alias.add_argument("scope", type=scope_reference)
    scope_alias.add_argument("alias", help="lowercase alias, or 'none' to clear")
    create_scope = sub.add_parser(
        "scope-create", help="create a scope from a CreateScope JSON file"
    )
    create_scope.add_argument("world")
    create_scope.add_argument("--input", type=Path, required=True)
    grant_scope = sub.add_parser(
        "scope-grant", help="set explicit credential operations on a scope"
    )
    grant_scope.add_argument("world")
    grant_scope.add_argument("scope", type=scope_reference)
    grant_scope.add_argument("--input", type=Path, required=True)
    flow = sub.add_parser("scope-flow", help="check audience containment for private capture")
    flow.add_argument("--input", type=Path, required=True)
    bind_context = sub.add_parser("context-bind", help="bind invocation reads and write target")
    bind_context.add_argument("--input", type=Path, required=True)
    context = sub.add_parser("context", help="inspect an invocation's immutable context binding")
    context.add_argument("context_id", type=UUID)
    delegate = sub.add_parser("agent-delegate", help="issue an attenuated child credential")
    delegate.add_argument("--input", type=Path, required=True)
    delegate.add_argument("--token-output", type=Path, required=True)
    sub.add_parser(
        "mounts", help="list configured remote shared spaces and world IDs"
    ).add_argument("--table", action="store_true")
    sub.add_parser("users", help="list shared-service identities (administrator)")
    user = sub.add_parser("user-set", help="enable or disable a shared-service identity")
    user.add_argument("user_id")
    enabled = user.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    members = sub.add_parser("memberships", help="list world membership (administrator)")
    members.add_argument("world")
    member = sub.add_parser("membership-set", help="set or remove manual world membership")
    member.add_argument("world")
    member.add_argument("user_id")
    member.add_argument("role", choices=[*list(Role), "none"])
    world = sub.add_parser("world-create")
    world.add_argument("name")
    world.add_argument("--kind", choices=list(WorldKind), default=WorldKind.PERSONAL)
    world.add_argument("--owner")
    world.add_argument("--alias")
    token = sub.add_parser("token-create", help="create a separately revocable agent credential")
    token.add_argument("--user", required=True)
    token.add_argument("--agent", required=True)
    token.add_argument("--grant", action="append", default=[], metavar="WORLD:ROLE")
    token.add_argument("--admin", action="store_true")
    revoke = sub.add_parser("token-revoke")
    revoke.add_argument("credential_id")
    for name in ("capture", "publish"):
        capture = sub.add_parser(name)
        capture.add_argument("world")
        capture.add_argument("--scope", type=scope_reference, help="scope UUID or @alias")
        capture.add_argument(
            "--context", type=UUID, help="bind invocation attribution and write target"
        )
        capture.add_argument("--source-id", required=True)
        capture.add_argument("--text", help="omit to read text from stdin")
        capture.add_argument(
            "--source-only",
            action="store_false",
            dest="project",
            help="skip Graphiti extraction for this capture",
        )
        capture.add_argument(
            "--at",
            type=datetime.fromisoformat,
            required=True,
            help="original timestamp with timezone; reuse it unchanged on retries",
        )
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--world", action="append", default=[])
    selected_scopes = search.add_mutually_exclusive_group()
    selected_scopes.add_argument(
        "--scope", type=scope_reference, help="scope UUID or @alias (select one world)"
    )
    selected_scopes.add_argument("--select", type=scope_selection, action="append", default=[])
    search.add_argument(
        "--source", action="store_true", help="search source text instead of Graphiti"
    )
    search.add_argument(
        "--entities", action="store_true", help="include entity summaries with graph facts"
    )
    search.add_argument("--limit", type=int, default=10)
    status = sub.add_parser("status")
    status.add_argument("world")
    status.add_argument("--scope", type=scope_reference, help="scope UUID or @alias")
    routed = sub.add_parser("route-search", help="recall from explicit configured mounts")
    routed.add_argument("query")
    routed.add_argument("--mount", action="append", required=True)
    routed.add_argument(
        "--source", action="store_true", help="search remote source text instead of Graphiti"
    )
    routed.add_argument("--limit", type=int, default=10)
    publication = sub.add_parser("route-publish", help="queue selected text for remote publication")
    publication.add_argument("mount")
    publication.add_argument("world")
    publication.add_argument("--source-id", required=True)
    publication.add_argument("--at", type=datetime.fromisoformat, required=True)
    publication.add_argument("--text", help="omit to read selected text from stdin")
    publication.add_argument("--source-only", action="store_false", dest="project")
    sub.add_parser("deliveries", help="inspect your durable publication outbox")
    retry = sub.add_parser("delivery-retry", help="reauthorize and retry your queued publication")
    retry.add_argument("delivery_id")
    named = sub.add_parser("identities", help="list your named agent/run/invocation UUIDs")
    named.add_argument("kind", choices=("agent", "run", "invocation"))
    named_create = sub.add_parser("identity-create", help="register a named canonical UUID")
    named_create.add_argument("kind", choices=("agent", "run", "invocation"))
    named_create.add_argument("--input", type=Path, required=True)
    named_resolve = sub.add_parser(
        "identity-resolve", help="resolve a visible identity ID or @alias"
    )
    named_resolve.add_argument("kind", choices=("agent", "run", "invocation"))
    named_resolve.add_argument("reference")
    named_update = sub.add_parser(
        "identity-update", help="rename an owned identity or change its alias"
    )
    named_update.add_argument("kind", choices=("agent", "run", "invocation"))
    named_update.add_argument("reference")
    named_update.add_argument("--input", type=Path, required=True)
    directory_import = sub.add_parser(
        "import-directory", help="preview or import extracted chats into an explicit private scope"
    )
    directory_import.add_argument("world", help="destination world ID or @alias")
    directory_import.add_argument("directory", type=Path)
    directory_import.add_argument(
        "--scope", type=scope_reference, required=True, help="private scope UUID or @alias"
    )
    directory_import.add_argument(
        "--format",
        choices=("auto", "chatgpt", "claude", "richlow-md", "documents"),
        default="auto",
        help="accepted source format; documents explicitly selects plain text and Markdown",
    )
    directory_import.add_argument("--all-branches", action="store_true")
    directory_import.add_argument(
        "--source-timezone", help="IANA timezone for rendered dates without an offset"
    )
    directory_import.add_argument(
        "--fallback-timestamp", help="explicit timestamp with timezone for undated sources"
    )
    directory_import.add_argument(
        "--dry-run", action="store_true", help="validate and preview locally without credentials"
    )
    directory_import.add_argument(
        "--source-only", action="store_true", help="store evidence without Graphiti extraction"
    )
    planner = sub.add_parser(
        "plan-directory", help="emit validated private records for a native client integration"
    )
    planner.add_argument("directory", type=Path)
    planner.add_argument(
        "--emit-records",
        action="store_true",
        required=True,
        help="explicitly include private transcript records in stdout",
    )
    planner.add_argument(
        "--format",
        choices=("auto", "chatgpt", "claude", "richlow-md", "documents"),
        default="auto",
    )
    planner.add_argument("--all-branches", action="store_true")
    planner.add_argument("--source-timezone")
    planner.add_argument("--fallback-timestamp")
    planner.add_argument("--source-only", action="store_true")
    for name in ("export", "restore", "import-chatgpt", "import-pi"):
        cmd = sub.add_parser(name, help="local authenticated administrative operation")
        cmd.add_argument("world")
        cmd.add_argument("path", type=Path)
        cmd.add_argument("--scope", type=scope_reference, help="scope UUID or @alias")
        if name.startswith("import-"):
            cmd.add_argument("--all-branches", action="store_true")
    rebuild = sub.add_parser("graph-rebuild", help="rebuild while the daemon is stopped")
    rebuild.add_argument("world")
    rebuild.add_argument("--scope", type=scope_reference, help="scope UUID or @alias")
    rebuild.add_argument(
        "--projected-only",
        action="store_false",
        dest="include_unprojected",
        help="rebuild only sources already selected for extraction",
    )
    keygen = sub.add_parser("authority-keygen", help="create a private authority encryption key")
    keygen.add_argument("path", type=Path)
    sub.add_parser("authority-backups", help="list completed authority backup artifacts")
    backup = sub.add_parser(
        "authority-backup", help="request and download an encrypted authority cut"
    )
    backup.add_argument("path", type=Path)
    backup.add_argument("--graph-mode", choices=("native", "rebuild"), default="native")
    recover = sub.add_parser(
        "authority-restore", help="recover an archive into a fresh stopped authority"
    )
    recover.add_argument("path", type=Path)
    recover.add_argument("--key-file", type=Path, required=True)
    recover.add_argument(
        "--native-graph",
        action="store_true",
        help="restore original graph keys into a configured empty backend",
    )
    sync = sub.add_parser(
        "artifact-sync", help="copy a verified encrypted backup to a named cloud remote"
    )
    sync.add_argument("path", type=Path)
    sync.add_argument("--key-file", type=Path, required=True)
    sync.add_argument("--config", type=Path, required=True)
    for name in ("lifecycle", "lifecycle-receipt", "graph-purge"):
        operation = sub.add_parser(name)
        operation.add_argument("world")
        operation.add_argument("--scope", type=scope_reference, required=True)
        if name == "lifecycle":
            operation.add_argument("--input", type=Path, required=True)
            operation.add_argument("--preview", action="store_true")
        elif name == "lifecycle-receipt":
            operation.add_argument("operation_id", type=UUID)
        else:
            operation.add_argument(
                "--workers-drained",
                action="store_true",
                required=True,
                help="confirm all authority graph writers are stopped/drained",
            )
    retention = sub.add_parser(
        "retention", help="apply or preview an explicit bounded retention policy"
    )
    retention.add_argument("--input", type=Path, required=True)
    retention.add_argument("--preview", action="store_true")
    sub.add_parser("retention-receipt").add_argument("operation_id", type=UUID)
    sub.add_parser("mcp", help="serve stdio MCP tools as a client of the HTTP daemon")
    from .setup import add_setup_parser

    add_setup_parser(sub)
    return p


def local_store(args):
    from .store import MemoryStore

    store = MemoryStore.from_env(args.data_dir)
    return store, store.authenticate(read_token(args))


def download_backup(client, artifact_id: str, target: Path):
    from .recovery_archive import MAX_ENCRYPTED_BACKUP_BYTES

    target = target.expanduser().absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise MemoryError("backup destination already exists")
    fd, temporary = tempfile.mkstemp(
        prefix=".authority-backup-", suffix=".pending", dir=target.parent
    )
    digest, size = hashlib.sha256(), 0
    try:
        with (
            os.fdopen(fd, "wb") as output,
            client.http.stream(
                "GET",
                f"/v1/authority/backups/{UUID(artifact_id)}",
                timeout=1200,
            ) as response,
        ):
            response.raise_for_status()
            expected = int(response.headers["content-length"])
            if not 0 < expected <= MAX_ENCRYPTED_BACKUP_BYTES:
                raise MemoryError("authority artifact exceeds the download limit")
            for chunk in response.iter_bytes(65536):
                size += len(chunk)
                if size > expected:
                    raise MemoryError("authority artifact size changed during download")
                output.write(chunk)
                digest.update(chunk)
            if size != expected:
                raise MemoryError("authority artifact download was truncated")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return {"path": str(target), "bytes": size, "sha256": digest.hexdigest()}
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(args):
    if args.command == "setup":
        from .setup import run_setup_command

        result = run_setup_command(args)
        if result is not None:
            emit(result)
        return
    if args.command == "artifact-sync":
        from .artifact_sync import ArtifactSyncConfig, sync_artifact

        emit(sync_artifact(args.path, args.key_file, read_input(args.config, ArtifactSyncConfig)))
        return
    if args.command == "authority-keygen":
        from .recovery_archive import generate_key

        generate_key(args.path)
        emit({"path": str(args.path), "format": "aes-256-gcm-key"})
        return
    if args.command == "authority-restore":
        from .graph import GraphConfig
        from .graph_snapshots import FalkorSnapshots
        from .recovery import restore_authority
        from .store import MemoryStore

        config = GraphConfig.from_env() if args.native_graph else None
        if args.native_graph and config is None:
            raise MemoryError("native recovery requires a configured empty FalkorDB backend")
        graph = FalkorSnapshots(config) if config else None
        try:
            emit(
                restore_authority(
                    args.path,
                    args.key_file,
                    args.data_dir,
                    database_url=MemoryStore.database_url_from_env(),
                    graph=graph,
                )
            )
        finally:
            if graph is not None:
                graph.close()
        return
    if args.command == "plan-directory":
        from .directory_import import plan_directory

        plan = plan_directory(
            args.directory,
            format=args.format,
            all_branches=args.all_branches,
            timezone=args.source_timezone,
            fallback_timestamp=args.fallback_timestamp,
        )
        if args.source_only:
            plan = replace(
                plan,
                episodes=[
                    episode.model_copy(update={"project": False}) for episode in plan.episodes
                ],
            )
            payload = plan.payload(UUID(int=0))
            plan = replace(
                plan,
                report={
                    **plan.report,
                    "payload_bytes": len(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                },
            )
        records = [
            episode.model_copy(update={"project": not args.source_only}).model_dump(mode="json")
            for episode in plan.episodes
        ]
        # Private integration transport: never use the human preview formatter.
        # Compact UTF-8 bounds the native client's pipe to the same batch budget.
        sys.stdout.write(
            json.dumps(
                {"report": plan.report, "episodes": records},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        return
    if args.command == "login":
        from .login import device_login

        emit(
            device_login(
                args.url,
                issuer=args.issuer,
                client_id=args.client_id,
                scopes=tuple(args.scope or ["openid", "memory-worlds"]),
                token_output=args.token_output,
            )
        )
        return
    if args.command == "init":
        from .store import MemoryStore

        world, token = MemoryStore.from_env(args.data_dir).bootstrap(args.user)
        emit({"world": world, "token": token})
        return
    if args.command == "serve":
        import uvicorn

        from .api import create_app
        from .graph import GraphConfig, GraphitiAdapter, ProjectionWorker
        from .store import MemoryStore

        try:
            loopback = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback and not args.allow_network:
            raise MemoryError("non-loopback binding requires --allow-network and TLS ingress")
        config = GraphConfig.from_env()
        worker = (
            ProjectionWorker(MemoryStore.from_env(args.data_dir), GraphitiAdapter(config))
            if config
            else None
        )
        uvicorn.run(
            create_app(args.data_dir, worker),
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
        )
        return
    if args.command in {"export", "restore", "import-chatgpt", "import-pi"}:
        from . import portability

        store, principal = local_store(args)
        resolve_local_selection(store, principal, args)
        if args.command == "export":
            emit(
                {
                    "path": str(
                        portability.export_world(
                            store, principal, args.world, args.path, scope_id=args.scope
                        )
                    )
                }
            )
        elif args.command == "restore":
            emit(
                portability.restore_world(
                    store, principal, args.world, args.path, scope_id=args.scope
                )
            )
        elif args.command == "import-chatgpt":
            emit(
                portability.import_chatgpt(
                    store,
                    principal,
                    args.world,
                    args.path,
                    all_branches=args.all_branches,
                    scope_id=args.scope,
                )
            )
        elif args.command == "import-pi":
            emit(
                portability.import_pi(
                    store,
                    principal,
                    args.world,
                    args.path,
                    all_branches=args.all_branches,
                    scope_id=args.scope,
                )
            )
        return
    if args.command in {"graph-rebuild", "graph-purge"}:
        from contextlib import nullcontext

        from .graph import GraphConfig, GraphitiAdapter, ProjectionWorker
        from .lifecycle import EpisodeLifecycle

        store, principal = local_store(args)
        resolve_local_selection(store, principal, args)
        if not principal.admin:
            raise MemoryError("graph maintenance requires an administrator")
        config = GraphConfig.from_env()
        if config is None:
            raise MemoryError("graph processing is not configured")
        world = store.get_world(principal, args.world, write=True, scope_id=args.scope)

        async def rebuild():
            if args.command == "graph-purge":
                adapter = GraphitiAdapter(config)
                try:
                    return await EpisodeLifecycle(store).purge(
                        principal,
                        args.world,
                        args.scope,
                        adapter,
                        workers_drained=args.workers_drained,
                    )
                finally:
                    await adapter.close()
            worker = ProjectionWorker(store, GraphitiAdapter(config))
            try:
                return await worker.rebuild(world, include_unprojected=args.include_unprojected)
            finally:
                await worker.close()

        with nullcontext() if store.persistence.is_postgres else DaemonLock(args.data_dir):
            emit(asyncio.run(rebuild()))
        return
    if args.command == "import-directory":
        from .directory_import import plan_directory

        import_plan = plan_directory(
            args.directory,
            format=args.format,
            all_branches=args.all_branches,
            timezone=args.source_timezone,
            fallback_timestamp=args.fallback_timestamp,
        )
        if args.source_only:
            import_plan = replace(
                import_plan,
                episodes=[
                    episode.model_copy(update={"project": False})
                    for episode in import_plan.episodes
                ],
            )
            # Recheck the encoded budget after changing projection intent.
            preview_payload = import_plan.payload(UUID(int=0))
            import_plan = replace(
                import_plan,
                report={
                    **import_plan.report,
                    "payload_bytes": len(
                        json.dumps(
                            preview_payload, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    ),
                },
            )
        if args.dry_run:
            emit(
                {
                    **import_plan.report,
                    "dry_run": True,
                    "destination": {"world_id": args.world, "scope_id": str(args.scope)},
                    "project": not args.source_only,
                }
            )
            return
    token = read_token(args)
    if args.command == "mcp":
        from .mcp import run_mcp

        run_mcp(args.url, token)
        return
    with Client(args.url, token) as client:
        if args.command not in {"discover", "resolve", "route-publish"}:
            if hasattr(args, "world"):
                args.world = (
                    [client.resolve_world(w) for w in args.world]
                    if isinstance(args.world, list)
                    else client.resolve_world(args.world)
                )
            if getattr(args, "scope", None) is not None:
                selected_world = args.world
                if isinstance(selected_world, list):
                    if len(selected_world) != 1:
                        raise MemoryError("--scope requires exactly one --world")
                    selected_world = selected_world[0]
                args.scope = client.resolve_scope(selected_world, args.scope)
        if getattr(args, "select", None):
            args.select = [
                ScopeSelection(
                    world_id=(wid := client.resolve_world(world)),
                    scope_id=client.resolve_scope(wid, sid),
                )
                for world, sid in args.select
            ]
        if args.command == "authority-backup":
            if args.path.exists() or args.path.is_symlink():
                raise MemoryError("backup destination already exists")
            client.http.timeout = httpx.Timeout(1200)
            receipt = client.create_backup(BackupRequest(graph_mode=args.graph_mode))
            emit({**receipt, **download_backup(client, receipt["artifact_id"], args.path)})
        elif args.command == "authority-backups":
            emit(client.backups())
        elif args.command == "lifecycle":
            emit(
                client.lifecycle(
                    args.world,
                    args.scope,
                    read_input(args.input, LifecycleRequest),
                    preview=args.preview,
                )
            )
        elif args.command == "lifecycle-receipt":
            emit(client.lifecycle_receipt(args.world, args.scope, args.operation_id))
        elif args.command == "retention":
            emit(client.retention(read_input(args.input, RetentionRequest), preview=args.preview))
        elif args.command == "retention-receipt":
            emit(client.retention_receipt(args.operation_id))
        elif args.command == "import-directory":
            result = client.request(
                "POST",
                f"{scope_path(args.world, args.scope)}/imports",
                json=import_plan.payload(args.scope),
            )
            emit(
                {
                    **import_plan.report,
                    **result,
                    "dry_run": False,
                    "destination": {"world_id": args.world, "scope_id": str(args.scope)},
                    "project": not args.source_only,
                }
            )
        elif args.command == "identities":
            emit(client.identities(args.kind))
        elif args.command == "identity-create":
            emit(client.create_identity(args.kind, read_input(args.input, CreateIdentity)))
        elif args.command == "identity-resolve":
            emit(client.resolve_identity(args.kind, args.reference))
        elif args.command == "identity-update":
            selected = client.resolve_identity(args.kind, args.reference)
            emit(
                client.update_identity(
                    args.kind, selected.id, read_input(args.input, UpdateIdentity)
                )
            )
        elif args.command == "worlds":
            listing(client.worlds(), "worlds") if args.table else emit(client.worlds())
        elif args.command == "discover":
            found = client.discover(
                world=args.world, include_scopes=args.scopes or args.world is not None
            )
            if args.json:
                emit(found)
            else:
                for kind in ("worlds", "mounts"):
                    print(kind.upper())
                    listing(found[kind], kind)
                for world_id, scopes in found["scopes"].items():
                    print(f"SCOPES {world_id}")
                    listing(scopes, "scopes")
        elif args.command == "resolve":
            if args.kind == "world":
                wid = client.resolve_world(args.reference)
                matches = [w for w in client.worlds() if w.id == wid]
                if not matches:
                    raise MemoryError("No accessible world matches that ID")
                emit(matches[0])
            elif args.kind == "scope":
                if not args.world:
                    raise MemoryError("resolve scope requires --world")
                wid = client.resolve_world(args.world)
                emit(client.get_scope(wid, client.resolve_scope(wid, args.reference)))
            else:
                matches = [m for m in client.mounts() if m.id == args.reference.removeprefix("@")]
                if not matches:
                    raise MemoryError("No accessible mount matches that ID")
                emit(matches[0])
        elif args.command == "me":
            emit(client.me())
        elif args.command == "scopes":
            listing(client.scopes(args.world), "scopes") if args.table else emit(
                client.scopes(args.world)
            )
        elif args.command == "scope":
            emit(client.get_scope(args.world, args.scope))
        elif args.command == "world-alias":
            emit(
                client.set_world_alias(
                    args.world, UpdateAlias(alias=None if args.alias == "none" else args.alias)
                )
            )
        elif args.command == "scope-alias":
            emit(
                client.set_scope_alias(
                    args.world,
                    args.scope,
                    UpdateAlias(alias=None if args.alias == "none" else args.alias),
                )
            )
        elif args.command == "scope-create":
            emit(client.create_scope(args.world, read_input(args.input, CreateScope)))
        elif args.command == "scope-grant":
            client.grant_scope(args.world, args.scope, read_input(args.input, UpdateScopeGrant))
            emit({"world_id": args.world, "scope_id": str(args.scope), "updated": True})
        elif args.command == "scope-flow":
            emit({"allowed": client.can_flow(read_input(args.input, ScopeFlow))})
        elif args.command == "context-bind":
            emit(client.bind_context(read_input(args.input, BindContext)))
        elif args.command == "context":
            emit(client.get_context(args.context_id))
        elif args.command == "agent-delegate":
            emit(
                delegate_to_file(
                    client, read_input(args.input, DelegateCredential), args.token_output
                )
            )
        elif args.command == "mounts":
            listing(client.mounts(), "mounts") if args.table else emit(client.mounts())
        elif args.command == "route-search":
            emit(
                client.routed_search(
                    RouterSearch(
                        mount_ids=args.mount,
                        query=args.query,
                        limit=args.limit,
                        mode="source" if args.source else "graph",
                    )
                )
            )
        elif args.command == "route-publish":
            if args.world.startswith("@"):
                raise MemoryError("remote publication requires a mounted world ID; see discover")
            emit(
                client.publish_remote(
                    RoutedPublication(
                        destination_mount_id=args.mount,
                        world_id=args.world,
                        source_id=args.source_id,
                        content=content(args),
                        occurred_at=args.at,
                        project=args.project,
                    )
                )
            )
        elif args.command == "deliveries":
            emit(client.deliveries())
        elif args.command == "delivery-retry":
            emit(client.retry_delivery(args.delivery_id))
        elif args.command == "users":
            emit(client.request("GET", "/v1/users"))
        elif args.command == "user-set":
            response = client.http.patch(f"/v1/users/{args.user_id}", json={"enabled": args.enable})
            response.raise_for_status()
            emit({"user_id": args.user_id, "enabled": args.enable})
        elif args.command == "memberships":
            emit(client.request("GET", f"/v1/worlds/{args.world}/memberships"))
        elif args.command == "membership-set":
            role = None if args.role == "none" else args.role
            response = client.http.put(
                f"/v1/worlds/{args.world}/memberships/{args.user_id}", json={"role": role}
            )
            response.raise_for_status()
            emit({"world_id": args.world, "user_id": args.user_id, "role": role})
        elif args.command == "world-create":
            emit(
                client.request(
                    "POST",
                    "/v1/worlds",
                    json=CreateWorld(
                        name=args.name, alias=args.alias, kind=args.kind, owner_id=args.owner
                    ).model_dump(mode="json"),
                )
            )
        elif args.command == "token-create":
            grants = {}
            for grant in args.grant:
                world, role = grant.rsplit(":", 1)
                grants[client.resolve_world(world)] = Role(role)
            emit(
                client.request(
                    "POST",
                    "/v1/credentials",
                    json=CreateToken(
                        user_id=args.user, agent_id=args.agent, grants=grants, admin=args.admin
                    ).model_dump(mode="json"),
                )
            )
        elif args.command == "token-revoke":
            response = client.http.delete(f"/v1/credentials/{args.credential_id}")
            response.raise_for_status()
            emit({"revoked": args.credential_id})
        elif args.command in ("capture", "publish"):
            episode = EpisodeInput(
                scope_id=args.scope,
                context_id=args.context,
                source_id=args.source_id,
                content=content(args),
                occurred_at=args.at,
                project=args.project,
            )
            if args.command == "capture":
                emit(client.capture(args.world, episode))
            else:
                emit(
                    client.publish(
                        args.world,
                        Publication(
                            source_id=episode.source_id,
                            content=episode.content,
                            occurred_at=episode.occurred_at,
                            project=episode.project,
                            context_id=episode.context_id,
                        ),
                        scope_id=args.scope,
                    )
                )
        elif args.command == "search":
            emit(
                client.search(
                    SearchRequest(
                        world_ids=args.world,
                        scope_id=args.scope,
                        scopes=args.select,
                        include_entities=args.entities,
                        query=args.query,
                        limit=args.limit,
                        mode="source" if args.source else "graph",
                    )
                )
            )
        elif args.command == "status":
            emit(client.status(args.world, scope_id=args.scope))


def main():
    try:
        run(parser().parse_args())
    except (MemoryError, ValidationError, ValueError, OSError) as exc:
        # Validation errors can include input text; omit it from terminal errors.
        message = (
            "invalid input; check command arguments"
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
    except httpx.HTTPError as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            print(f"memory API returned HTTP {exc.response.status_code}", file=sys.stderr)
        else:
            print("memory API is unreachable", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
