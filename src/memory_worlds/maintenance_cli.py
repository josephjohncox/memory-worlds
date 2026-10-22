"""Explicit thread archives and maintenance commands over the ordinary HTTP API."""

import argparse
import math
from pathlib import Path
from uuid import UUID

from .context_models import ContextBundleInput
from .maintenance_models import MaintenanceRequest
from .thread_models import CreateThread


def wait_seconds(value: str) -> float:
    try:
        timeout = float(value)
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError
        return timeout
    except ValueError:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite number from 0 to 60 seconds"
        ) from None


def add_maintenance_parsers(sub) -> None:
    thread = sub.add_parser("thread", help="create and discover your private durable threads")
    commands = thread.add_subparsers(dest="thread_command", required=True)
    commands.add_parser("create", help="create an idempotent thread from a JSON file").add_argument(
        "--input", type=Path, required=True
    )
    listing = commands.add_parser("list", help="list threads accessible to this credential")
    listing.add_argument("--world", dest="world_id", help="world ID or @alias")
    listing.add_argument("--include-expired", action="store_true")
    commands.add_parser("show", help="inspect one authorized thread").add_argument(
        "thread_id", type=UUID
    )
    archive = sub.add_parser(
        "context-archive", help="archive typed context in an explicit private thread"
    )
    archive.add_argument("thread_id", type=UUID)
    archive.add_argument("--input", type=Path, required=True)
    read = sub.add_parser("context-read", help="recover one authorized thread context bundle")
    read.add_argument("thread_id", type=UUID)
    read.add_argument("bundle_id", type=UUID)
    maintenance = sub.add_parser(
        "maintenance", help="submit and inspect explicit memory maintenance jobs"
    )
    jobs = maintenance.add_subparsers(dest="maintenance_command", required=True)
    jobs.add_parser("submit", help="submit an idempotent job from a JSON file").add_argument(
        "--input", type=Path, required=True
    )
    jobs.add_parser("list", help="list this credential's maintenance jobs")
    for action in ("status", "wait", "cancel"):
        command = jobs.add_parser(action)
        command.add_argument("job_id", type=UUID)
        if action == "wait":
            command.add_argument("--timeout", type=wait_seconds, default=30.0)
    jobs.add_parser(
        "artifact", help="read a derived artifact with live source authorization"
    ).add_argument("artifact_id", type=UUID)


def run_maintenance_command(args, client):
    # Shared readers/emitters stay in the main CLI; this module owns command shape.
    from .cli import read_input

    if args.command == "thread":
        if args.thread_command == "create":
            return client.create_thread(read_input(args.input, CreateThread))
        if args.thread_command == "list":
            return client.threads(
                world_id=client.resolve_world(args.world_id) if args.world_id is not None else None,
                include_expired=args.include_expired,
            )
        return client.thread(args.thread_id)
    if args.command == "context-archive":
        return client.archive_context(args.thread_id, read_input(args.input, ContextBundleInput))
    if args.command == "context-read":
        return client.context_bundle(args.thread_id, args.bundle_id)
    action = args.maintenance_command
    if action == "submit":
        return client.submit_maintenance(read_input(args.input, MaintenanceRequest))
    if action == "list":
        return client.maintenance_jobs()
    if action == "status":
        return client.maintenance_job(args.job_id)
    if action == "wait":
        return client.wait_maintenance(args.job_id, timeout_seconds=args.timeout)
    if action == "cancel":
        return client.cancel_maintenance(args.job_id)
    return client.maintenance_artifact(args.artifact_id)
