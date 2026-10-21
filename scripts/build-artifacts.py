#!/usr/bin/env python3
"""Build the versioned distributions and a reviewable source/tool receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/memory-worlds"


def command(arguments: list[str], *, cwd: Path = ROOT, capture: bool = False) -> str:
    result = subprocess.run(arguments, cwd=cwd, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ""


def versions() -> dict[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    native = json.loads((ROOT / "packages/pi/package.json").read_text())
    chart_text = (CHART / "Chart.yaml").read_text()
    chart = {}
    for field in ("name", "version", "appVersion"):
        match = re.search(rf"^{field}:\s*[\"']?([^\s\"'#]+)", chart_text, re.MULTILINE)
        if match is None:
            raise SystemExit(f"Chart.yaml must contain {field}")
        chart[field] = match[1]
    if (project["name"], native["name"], chart["name"]) != (
        "memory-worlds",
        "memory-worlds-pi",
        "memory-worlds",
    ):
        raise SystemExit("Package names must use the Memory Worlds names")
    release = project["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release):
        raise SystemExit("Release versions currently require a three-component stable version")
    if {release, native["version"], chart["version"], chart["appVersion"]} != {release}:
        raise SystemExit("Python, Pi, chart and chart appVersion must match")
    return {"python": release, "pi": release, "chart": release, "app": release}


def source_receipt(expected_sha: str | None, require_clean: bool) -> dict[str, object]:
    sha = command(["git", "rev-parse", "HEAD"], capture=True)
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or (expected_sha and sha != expected_sha):
        raise SystemExit("The checkout does not match the expected full source commit")
    clean = not command(["git", "status", "--porcelain", "--untracked-files=all"], capture=True)
    if require_clean and not clean:
        raise SystemExit("Release artifacts require a clean checkout")
    paths = command(["git", "ls-files", "-z"], capture=True).split("\0")
    digest = hashlib.sha256()
    for name in sorted(filter(None, paths)):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"Tracked build inputs must be regular files: {name}")
        digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {
        "commit": sha,
        "commit_tree": command(["git", "rev-parse", "HEAD^{tree}"], capture=True),
        "clean": clean,
        "tracked_working_tree_sha256": digest.hexdigest(),
        "commit_timestamp": int(
            command(["git", "show", "-s", "--format=%ct", "HEAD"], capture=True)
        ),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/artifacts")
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--npm", default="npm")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--source-sha", help="Require this immutable 40-character source commit")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument(
        "--skip-npm-install", action="store_true", help="Use an existing locked npm install"
    )
    parser.add_argument(
        "--check-only", action="store_true", help="Check versions and source identity only"
    )
    args = parser.parse_args()
    release_versions = versions()
    source = source_receipt(args.source_sha, args.require_clean)
    if args.check_only:
        print(json.dumps({"versions": release_versions, "source": source}, indent=2))
        return
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Artifact output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    os.environ["SOURCE_DATE_EPOCH"] = str(source["commit_timestamp"])
    command([sys.executable, str(ROOT / "scripts/audit-artifacts.py"), "--source-only"])
    tool_versions = {
        "python": sys.version.split()[0],
        "python_build_backend": tomllib.loads((ROOT / "pyproject.toml").read_text())[
            "build-system"
        ],
        "uv": command([args.uv, "--version"], capture=True),
        "node": command(["node", "--version"], capture=True),
        "npm": command([args.npm, "--version"], capture=True),
        "helm": command([args.helm, "version", "--short"], capture=True),
    }
    command(
        [
            args.uv,
            "build",
            "--no-sources",
            "--no-create-gitignore",
            "--out-dir",
            str(output),
            "--python",
            sys.executable,
        ]
    )
    if not args.skip_npm_install:
        command([args.npm, "ci", "--ignore-scripts"], cwd=ROOT / "packages/pi")
    command([args.npm, "run", "build"], cwd=ROOT / "packages/pi")
    command(
        [args.npm, "pack", "--ignore-scripts", "--pack-destination", str(output)],
        cwd=ROOT / "packages/pi",
    )
    command([args.helm, "lint", "--strict", str(CHART)])
    command([args.helm, "package", str(CHART), "--destination", str(output)])
    files = sorted(path for path in output.iterdir() if path.is_file())
    manifest = {
        "schema_version": 1,
        "project": "memory-worlds",
        "version": release_versions["app"],
        "created_at": datetime.now(UTC).isoformat(),
        "source": source,
        "tools": tool_versions,
        "artifacts": [
            {"filename": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}
            for path in files
        ],
        "container": {
            "published_separately": True,
            "tag_format": "sha-<full-source-commit>",
            "platforms": ["linux/amd64", "linux/arm64"],
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    files.append(output / "manifest.json")
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(files))
    )
    command([sys.executable, str(ROOT / "scripts/audit-artifacts.py"), str(output)])
    print(f"Built and audited {len(files) - 1} distributions in {output}")


if __name__ == "__main__":
    main()
