#!/usr/bin/env python3
"""Reject private inputs and malformed distributions before storing build artifacts."""

from __future__ import annotations

import argparse
import email
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_MEMBER = 16 * 1024 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_MEMBERS = 10_000
FORBIDDEN_PARTS = {
    ".git",
    ".aws",
    ".agents",
    ".codex",
    ".venv",
    "private",
    "node_modules",
    "__pycache__",
    ".memory-worlds",
    ".kube",
    ".pytest_cache",
    ".ruff_cache",
    ".npmrc",
    ".env",
    "daemon.lock",
}
FORBIDDEN_NAMES = (
    "ChatGPT-*.json",
    "conversations*.json",
    "*.sqlite*",
    "*.mworld",
    "*.mwa",
    "*.mwauthority",
    "*.pem",
    "*.key",
    "*credentials*.json",
    "*.pyc",
    "*.pyo",
    "*.token",
    "*.kubeconfig",
    "*.env",
    "bootstrap.json",
)
SOURCE_ROOTS = {
    "AGENTS.md",
    "src",
    "tests",
    "docs",
    "deploy",
    "scripts",
    "examples",
    "packages",
    "plugins",
    ".github",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "uv.lock",
    ".gitignore",
    ".env.example",
    ".python-version",
    ".dockerignore",
    ".gitattributes",
    "mise.toml",
    "mise.lock",
    "tasks",
}
SIGNATURES = re.compile(
    rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"
    rb"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    rb"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
    rb"|mw_[a-f0-9]{32}\.[A-Za-z0-9_-]{30,}"
    rb"|https://chatgpt\.com/(?:c|share)/[A-Za-z0-9_-]+"
    rb"|/ho"
    rb"me/[^/\s]+/"
)


class AuditFailure(ValueError):
    pass


def fail(message: str) -> None:
    raise AuditFailure(message)


def safe_parts(name: str) -> tuple[str, ...]:
    path = PurePosixPath(name)
    if (
        not name
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or "\0" in name
        or ":" in path.parts[0]
        or str(path) != name.rstrip("/")
    ):
        fail(f"Unsafe archive path: {name!r}")
    if any(ord(character) < 32 for character in name):
        fail("Control characters are forbidden in artifact paths")
    if FORBIDDEN_PARTS.intersection(path.parts) or any(
        fnmatch.fnmatch(part.casefold(), pattern.casefold())
        for part in path.parts
        for pattern in FORBIDDEN_NAMES
    ):
        fail(f"Private input or generated local file: {name}")
    return path.parts


def audit_content(name: str, data: bytes) -> None:
    if len(data) > MAX_MEMBER:
        fail(f"Artifact member exceeds the size limit: {name}")
    if SIGNATURES.search(data):
        fail(f"Private content signature in: {name}")
    extra = os.environ.get("PUBLIC_CONTENT_DENY_PATTERN", "")
    if extra:
        pattern = re.compile(extra, re.IGNORECASE)
        if pattern.search(name) or pattern.search(data.decode("utf-8", errors="replace")):
            fail(f"Configured private naming pattern in: {name}")


def audit_sources() -> None:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        name = os.fsdecode(raw_name)
        parts = safe_parts(name)
        if parts[0] not in SOURCE_ROOTS:
            fail(f"Unexpected tracked public source path: {name}")
        if parts[0] == "packages" and parts[:2] not in {
            ("packages", "pi"),
            ("packages", "opencode"),
        }:
            fail(f"Unexpected package source path: {name}")
        if parts[0] == "plugins" and parts[:2] != ("plugins", "claude"):
            fail(f"Unexpected plugin source path: {name}")
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            fail(f"Tracked sources must be regular files: {name}")
        if path.stat().st_size > MAX_MEMBER:
            fail(f"Tracked source exceeds the size limit: {name}")
        audit_content(name, path.read_bytes())


def members(archive: Path) -> dict[str, bytes]:
    if archive.stat().st_size > MAX_ARCHIVE:
        fail("Compressed archive exceeds the size limit")
    entries: dict[str, bytes] = {}
    total = 0

    def add(name: str, data: bytes) -> None:
        nonlocal total
        safe_parts(name)
        if name in entries:
            fail(f"Duplicate artifact path: {name}")
        total += len(data)
        if total > MAX_ARCHIVE or len(entries) >= MAX_MEMBERS:
            fail("Archive exceeds the expanded size or entry count limit")
        audit_content(name, data)
        entries[name] = data

    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as package:
            if len(package.infolist()) > MAX_MEMBERS:
                fail("Archive exceeds the entry count limit")
            for member in package.infolist():
                safe_parts(member.filename)
                kind = stat.S_IFMT(member.external_attr >> 16)
                if member.is_dir():
                    if kind not in {0, stat.S_IFDIR}:
                        fail("Archive directory has an invalid type")
                    continue
                if kind not in {0, stat.S_IFREG} or member.file_size > MAX_MEMBER:
                    fail("Wheels require regular files within the size limit")
                add(member.filename, package.read(member))
    else:
        with tarfile.open(archive, "r:gz") as package:
            for index, member in enumerate(package):
                if index >= MAX_MEMBERS:
                    fail("Archive exceeds the entry count limit")
                safe_parts(member.name)
                if member.isdir():
                    continue
                if not member.isfile() or member.size > MAX_MEMBER:
                    fail("Archives require regular files within the size limit")
                stream = package.extractfile(member)
                if stream is None:
                    fail("Unreadable artifact member")
                add(member.name, stream.read(MAX_MEMBER + 1))
    return entries


def metadata(data: bytes, version: str) -> None:
    parsed = email.message_from_bytes(data)
    if parsed["Name"] != "memory-worlds" or parsed["Version"] != version:
        fail("Python package metadata name or version mismatch")
    if parsed["License-Expression"] != "Apache-2.0":
        fail("Python package must declare Apache-2.0")
    if any(
        parsed.get(field)
        for field in (
            "Author",
            "Author-email",
            "Maintainer",
            "Maintainer-email",
            "Home-page",
        )
    ):
        fail("Review attribution metadata before public distribution")


def native_metadata(native: dict, files: dict[str, bytes], kind: str, version: str) -> None:
    if not isinstance(native, dict) or (
        native.get("name"),
        native.get("version"),
        native.get("license"),
    ) != (
        f"memory-worlds-{kind}",
        version,
        "Apache-2.0",
    ):
        fail("Native package metadata mismatch")
    exports = native.get("exports")
    if not isinstance(exports, dict) or "." not in exports:
        fail("Native package requires explicit module exports")
    entry = exports["."]
    entry = entry.get("import") if isinstance(entry, dict) else entry
    if entry != ("./dist/extension.js" if kind == "pi" else "./dist/index.js"):
        fail("Native package module entry mismatch")
    for value in exports.values():
        targets = list(value.values()) if isinstance(value, dict) else [value]
        if not targets:
            fail("Native package exports must identify packaged modules")
        for target in targets:
            if (
                not isinstance(target, str)
                or not target.startswith("./dist/")
                or "package/" + target[2:] not in files
            ):
                fail("Native package export references an absent or external module")


def claude_metadata(files: dict[str, bytes], version: str) -> None:
    prefix = "memory-worlds-claude/"
    plugin = json.loads(files[prefix + ".claude-plugin/plugin.json"])
    if not isinstance(plugin, dict) or (
        plugin.get("name"),
        plugin.get("version"),
        plugin.get("license"),
    ) != (
        "memory-worlds",
        version,
        "Apache-2.0",
    ):
        fail("Claude plugin metadata mismatch")
    config = plugin.get("userConfig")
    if not isinstance(config, dict) or set(config) != {"runtime_python", "url", "token_file"}:
        fail("Claude plugin requires explicit runtime, service and credential-file settings")
    for name in ("runtime_python", "token_file"):
        if (
            not isinstance(config[name], dict)
            or config[name].get("type") != "file"
            or config[name].get("required") is not True
        ):
            fail("Claude plugin runtime and credential references require file settings")
    if not isinstance(config["url"], dict) or config["url"].get("type") != "string":
        fail("Claude plugin service URL requires a string setting")
    mcp = json.loads(files[prefix + ".mcp.json"])
    if mcp != {
        "mcpServers": {
            "memory-worlds": {
                "command": "${user_config.runtime_python}",
                "args": [
                    "-I",
                    "-m",
                    "memory_worlds.cli",
                    "--url",
                    "${user_config.url}",
                    "--token-file",
                    "${user_config.token_file}",
                    "mcp",
                ],
            }
        }
    }:
        fail("Claude MCP module command must use configured runtime and credential-file references")


def audit_distribution(archive: Path, kind: str, version: str) -> None:
    files = members(archive)
    license_bytes = (ROOT / "LICENSE").read_bytes()
    if kind == "wheel":
        prefix = f"memory_worlds-{version}.dist-info"
        if any(safe_parts(name)[0] not in {"memory_worlds", prefix} for name in files):
            fail("Unexpected wheel path")
        required = {
            "memory_worlds/__init__.py",
            "memory_worlds/setup_cli.py",
            "memory_worlds/templates/setup-graph.compose.yaml",
            f"{prefix}/METADATA",
            f"{prefix}/licenses/LICENSE",
        }
        if not required <= files.keys():
            fail("Wheel lacks required module, setup template, metadata or license")
        metadata(files[f"{prefix}/METADATA"], version)
        if files[f"{prefix}/licenses/LICENSE"] != license_bytes:
            fail("Wheel license differs from the approved license")
    elif kind == "sdist":
        prefix = f"memory_worlds-{version}/"
        if any(not name.startswith(prefix) for name in files):
            fail("Source archive requires one correctly named root")
        for name in files:
            parts = safe_parts(name)[1:]
            if not parts or parts[0] not in SOURCE_ROOTS | {"PKG-INFO"}:
                fail(f"Unexpected source distribution path: {name}")
            if parts[0] == "packages" and parts[:2] not in {
                ("packages", "pi"),
                ("packages", "opencode"),
            }:
                fail(f"Unexpected package source distribution path: {name}")
            if parts[0] == "plugins" and parts[:2] != ("plugins", "claude"):
                fail(f"Unexpected plugin source distribution path: {name}")
        required = {
            "pyproject.toml",
            "uv.lock",
            "LICENSE",
            "PKG-INFO",
            "deploy/helm/memory-worlds/Chart.yaml",
            "src/memory_worlds/templates/setup-graph.compose.yaml",
            "scripts/bootstrap.py",
            "tasks/setup.toml",
            "docs/setup.md",
            "tasks/opencode.toml",
            "packages/opencode/package.json",
            "packages/opencode/package-lock.json",
            "packages/opencode/tsconfig.json",
            "packages/opencode/src/index.ts",
            "packages/opencode/src/factory.ts",
            "packages/opencode/test/plugin.test.mjs",
            "packages/opencode/README.md",
            "packages/opencode/LICENSE",
            "plugins/claude/.claude-plugin/plugin.json",
            "plugins/claude/.mcp.json",
            "plugins/claude/skills/memory-worlds/SKILL.md",
            "plugins/claude/README.md",
            "plugins/claude/LICENSE",
        }
        if not {prefix + name for name in required} <= files.keys():
            fail("Source archive lacks required source, chart, lock or license files")
        metadata(files[prefix + "PKG-INFO"], version)
        if files[prefix + "LICENSE"] != license_bytes:
            fail("Source archive license differs from the approved license")
    elif kind in {"pi", "opencode"}:
        for name in files:
            parts = safe_parts(name)
            if (
                len(parts) < 2
                or parts[0] != "package"
                or parts[1] not in {"dist", "package.json", "README.md", "LICENSE"}
            ):
                fail(f"Unexpected native package path: {name}")
        required = {
            "package/package.json",
            "package/README.md",
            "package/LICENSE",
        }
        required |= (
            {"package/dist/extension.js", "package/dist/client.js"}
            if kind == "pi"
            else {
                "package/dist/index.js",
                "package/dist/index.d.ts",
                "package/dist/factory.js",
                "package/dist/factory.d.ts",
                "package/dist/config.js",
                "package/dist/service.js",
                "package/dist/intents.js",
            }
        )
        if not required <= files.keys():
            fail("Native package lacks required modules, metadata or license")
        native = json.loads(files["package/package.json"])
        native_metadata(native, files, kind, version)
        if files["package/LICENSE"] != license_bytes:
            fail("Native package license differs from the approved license")
    elif kind == "claude":
        required = {
            "memory-worlds-claude/.claude-plugin/plugin.json",
            "memory-worlds-claude/.mcp.json",
            "memory-worlds-claude/skills/memory-worlds/SKILL.md",
            "memory-worlds-claude/README.md",
            "memory-worlds-claude/LICENSE",
        }
        if set(files) != required:
            fail(
                "Claude archive requires the plugin manifest, MCP config, skill, README and license"
            )
        claude_metadata(files, version)
        if files["memory-worlds-claude/LICENSE"] != license_bytes:
            fail("Claude plugin license differs from the approved license")
    elif kind == "chart":
        if any(safe_parts(name)[0] != "memory-worlds" for name in files):
            fail("Unexpected chart root")
        required = {
            "memory-worlds/Chart.yaml",
            "memory-worlds/values.yaml",
            "memory-worlds/values.schema.json",
            "memory-worlds/LICENSE",
        }
        if not required <= files.keys() or not any(
            name.startswith("memory-worlds/templates/") for name in files
        ):
            fail("Chart lacks required templates, schema, values or license")
        for field in ("version", "appVersion"):
            match = re.search(
                rf"^{field}:\s*[\"']?([^\s\"'#]+)",
                files["memory-worlds/Chart.yaml"].decode(),
                re.MULTILINE,
            )
            if match is None or match[1] != version:
                fail("Chart version or appVersion mismatch")
        if files["memory-worlds/LICENSE"] != license_bytes:
            fail("Chart license differs from the approved license")
    else:
        fail("Unknown distribution kind")


def audit_python_directory(directory: Path) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    expected = {
        f"memory_worlds-{version}-py3-none-any.whl": "wheel",
        f"memory_worlds-{version}.tar.gz": "sdist",
    }
    names = {path.name for path in directory.iterdir()}
    generated_ignore = directory / ".gitignore"
    if generated_ignore.is_file() and not generated_ignore.is_symlink():
        if generated_ignore.read_bytes() in {b"*", b"*\n"}:
            names.discard(".gitignore")
    if names != expected.keys():
        fail("Expected exactly one Python wheel and source distribution")
    for name, kind in expected.items():
        path = directory / name
        if not path.is_file() or path.is_symlink():
            fail("Artifact outputs must be regular files")
        audit_distribution(path, kind, version)


def audit_directory(directory: Path) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    expected = {
        f"memory_worlds-{version}-py3-none-any.whl": "wheel",
        f"memory_worlds-{version}.tar.gz": "sdist",
        f"memory-worlds-pi-{version}.tgz": "pi",
        f"memory-worlds-opencode-{version}.tgz": "opencode",
        f"memory-worlds-claude-{version}.tar.gz": "claude",
        f"memory-worlds-{version}.tgz": "chart",
    }
    names = {path.name for path in directory.iterdir()}
    if names != expected.keys() | {"manifest.json", "SHA256SUMS"}:
        fail("Artifact directory must contain exactly six distributions, manifest and checksums")
    for name in names:
        path = directory / name
        if not path.is_file() or path.is_symlink():
            fail("Artifact outputs must be regular files")
        limit = MAX_ARCHIVE if name in expected else 65536
        if path.stat().st_size > limit:
            fail(f"Artifact output exceeds the size limit: {name}")
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("project") != "memory-worlds"
        or manifest.get("version") != version
    ):
        fail("Artifact manifest identity or schema mismatch")
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("commit"), str)
        or not re.fullmatch(r"[a-f0-9]{40}", source["commit"])
    ):
        fail("Artifact manifest requires the full source commit")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("filename"), str)
        or not isinstance(item.get("sha256"), str)
        or not isinstance(item.get("bytes"), int)
        for item in artifacts
    ):
        fail("Manifest artifact receipts must identify files, checksums and byte counts")
    receipts = {item["filename"]: item for item in artifacts}
    if set(receipts) != set(expected) or len(artifacts) != len(expected):
        fail("Manifest must identify exactly the six distributions")
    checksums = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if match is None or match[2] in checksums:
            fail("Malformed or duplicate checksum entry")
        checksums[match[2]] = match[1]
    if set(checksums) != set(expected) | {"manifest.json"}:
        fail("Checksums must cover every distribution and the manifest")
    for name, digest in checksums.items():
        data = (directory / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            fail(f"Checksum mismatch: {name}")
        if name in receipts and (
            receipts[name]["sha256"] != digest or receipts[name]["bytes"] != len(data)
        ):
            fail(f"Manifest receipt mismatch: {name}")
    for name, kind in expected.items():
        audit_distribution(directory / name, kind, version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source-only", action="store_true")
    mode.add_argument("--python-only", action="store_true")
    args = parser.parse_args()
    try:
        audit_sources()
        if not args.source_only:
            if args.directory is None:
                parser.error("directory is required without --source-only")
            if args.python_only:
                audit_python_directory(args.directory)
            else:
                audit_directory(args.directory)
    except (
        AuditFailure,
        KeyError,
        ValueError,
        OSError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        raise SystemExit(str(error)) from error
    print(
        "Artifact audit passed: bounded regular files, expected metadata, "
        "approved licenses and no private signatures"
    )


if __name__ == "__main__":
    main()
