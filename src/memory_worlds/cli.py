import argparse
import asyncio
import ipaddress
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

from .client import Client
from .errors import MemoryError
from .locking import DaemonLock
from .models import CreateToken, CreateWorld, EpisodeInput, Role, SearchRequest, WorldKind
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
    sub.add_parser("worlds", help="list accessible worlds through the daemon")
    sub.add_parser("me", help="show the authenticated identity and accessible worlds")
    sub.add_parser("mounts", help="list configured remote shared spaces")
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
        capture.add_argument("--source-id", required=True)
        capture.add_argument("--text", help="omit to read text from stdin")
        capture.add_argument("--project", action="store_true")
        capture.add_argument(
            "--at",
            type=datetime.fromisoformat,
            required=True,
            help="original timestamp with timezone; reuse it unchanged on retries",
        )
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--world", action="append", required=True)
    search.add_argument("--graph", action="store_true")
    search.add_argument("--limit", type=int, default=10)
    status = sub.add_parser("status")
    status.add_argument("world")
    routed = sub.add_parser("route-search", help="recall from explicit configured mounts")
    routed.add_argument("query")
    routed.add_argument("--mount", action="append", required=True)
    routed.add_argument("--graph", action="store_true")
    routed.add_argument("--limit", type=int, default=10)
    publication = sub.add_parser("route-publish", help="queue selected text for remote publication")
    publication.add_argument("mount")
    publication.add_argument("world")
    publication.add_argument("--source-id", required=True)
    publication.add_argument("--at", type=datetime.fromisoformat, required=True)
    publication.add_argument("--text", help="omit to read selected text from stdin")
    publication.add_argument("--project", action="store_true")
    sub.add_parser("deliveries", help="inspect your durable publication outbox")
    retry = sub.add_parser("delivery-retry", help="reauthorize and retry your queued publication")
    retry.add_argument("delivery_id")
    for name in ("export", "restore", "import-chatgpt", "import-pi", "backup"):
        cmd = sub.add_parser(name, help="local authenticated administrative operation")
        cmd.add_argument("world")
        cmd.add_argument("path", type=Path)
        if name.startswith("import-"):
            cmd.add_argument("--all-branches", action="store_true")
    rebuild = sub.add_parser("graph-rebuild", help="rebuild while the daemon is stopped")
    rebuild.add_argument("world")
    rebuild.add_argument(
        "--include-unprojected",
        action="store_true",
        help="explicitly consent to model processing of imported sources",
    )
    sub.add_parser("mcp", help="serve stdio MCP tools as a client of the HTTP daemon")
    return p


def local_store(args):
    from .store import MemoryStore

    store = MemoryStore(args.data_dir)
    return store, store.authenticate(read_token(args))


def backup_source(store, principal, world_id: str, target: Path):
    # Restrict backend-file operations to authority administrators.
    if not principal.admin:
        from .errors import Forbidden

        raise Forbidden("source database backup requires an administrator")
    world = store.get_world(principal, world_id)
    target = target.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise MemoryError("backup destination already exists")
    fd, temporary = tempfile.mkstemp(prefix=".memory-backup-", suffix=".pending", dir=target.parent)
    os.close(fd)
    try:
        source_path = store.world_path(world.id)
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MemoryError("backup integrity check failed")
        with open(temporary, "rb") as completed:
            os.fsync(completed.fileno())
        os.link(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        emit({"path": str(target), "world_id": world.id, "format": "sqlite-source-backup"})
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(args):
    if args.command == "init":
        from .store import MemoryStore

        world, token = MemoryStore(args.data_dir).bootstrap(args.user)
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
            ProjectionWorker(MemoryStore(args.data_dir), GraphitiAdapter(config))
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
    if args.command in {"export", "restore", "import-chatgpt", "import-pi", "backup"}:
        from . import portability

        store, principal = local_store(args)
        if args.command == "export":
            emit({"path": str(portability.export_world(store, principal, args.world, args.path))})
        elif args.command == "restore":
            emit(portability.restore_world(store, principal, args.world, args.path))
        elif args.command == "import-chatgpt":
            emit(
                portability.import_chatgpt(
                    store, principal, args.world, args.path, all_branches=args.all_branches
                )
            )
        elif args.command == "import-pi":
            emit(
                portability.import_pi(
                    store, principal, args.world, args.path, all_branches=args.all_branches
                )
            )
        else:
            backup_source(store, principal, args.world, args.path)
        return
    if args.command == "graph-rebuild":
        from .graph import GraphConfig, GraphitiAdapter, ProjectionWorker

        store, principal = local_store(args)
        if not principal.admin:
            raise MemoryError("graph rebuild requires an administrator")
        config = GraphConfig.from_env()
        if config is None:
            raise MemoryError("graph processing is not configured")
        world = store.get_world(principal, args.world, write=True)

        async def rebuild():
            worker = ProjectionWorker(store, GraphitiAdapter(config))
            try:
                return await worker.rebuild(world, include_unprojected=args.include_unprojected)
            finally:
                await worker.close()

        with DaemonLock(args.data_dir):
            emit(asyncio.run(rebuild()))
        return
    token = read_token(args)
    if args.command == "mcp":
        from .mcp import run_mcp

        run_mcp(args.url, token)
        return
    with Client(args.url, token) as client:
        if args.command == "worlds":
            emit(client.worlds())
        elif args.command == "me":
            emit(client.me())
        elif args.command == "mounts":
            emit(client.mounts())
        elif args.command == "route-search":
            emit(
                client.routed_search(
                    RouterSearch(
                        mount_ids=args.mount,
                        query=args.query,
                        limit=args.limit,
                        mode="graph" if args.graph else "source",
                    )
                )
            )
        elif args.command == "route-publish":
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
                        name=args.name, kind=args.kind, owner_id=args.owner
                    ).model_dump(mode="json"),
                )
            )
        elif args.command == "token-create":
            grants = {}
            for grant in args.grant:
                world, role = grant.rsplit(":", 1)
                grants[world] = Role(role)
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
                source_id=args.source_id,
                content=content(args),
                occurred_at=args.at,
                project=args.project,
            )
            if args.command == "capture":
                emit(client.capture(args.world, episode))
            else:
                emit(
                    client.request(
                        "POST",
                        f"/v1/worlds/{args.world}/publications",
                        json={
                            k: v
                            for k, v in episode.model_dump(mode="json").items()
                            if k in {"source_id", "content", "occurred_at", "project"}
                        },
                    )
                )
        elif args.command == "search":
            emit(
                client.search(
                    SearchRequest(
                        world_ids=args.world,
                        query=args.query,
                        limit=args.limit,
                        mode="graph" if args.graph else "source",
                    )
                )
            )
        elif args.command == "status":
            emit(client.status(args.world))


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
