"""Adversarial checks for the distribution publication boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/audit-artifacts.py"
specification = importlib.util.spec_from_file_location("artifact_audit", SCRIPT)
assert specification and specification.loader
audit = importlib.util.module_from_spec(specification)
specification.loader.exec_module(audit)
build_specification = importlib.util.spec_from_file_location(
    "artifact_build", ROOT / "scripts/build-artifacts.py"
)
assert build_specification and build_specification.loader
build = importlib.util.module_from_spec(build_specification)
build_specification.loader.exec_module(build)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../credentials",
        "package/../../credentials",
        "C:/credentials",
        "package\\credentials",
        "package/./file",
        "package//file",
        "./package/file",
        "package/.env",
        "package/node_modules/index.js",
        "package/daemon.sqlite3-wal",
        "package/authority-backup.mwa",
        "package/ChatGPT-export.json",
        "package/private/key.txt",
        "package/line\nbreak",
    ],
)
def test_reject_unsafe_or_private_artifact_paths(path: str) -> None:
    with pytest.raises(audit.AuditFailure):
        audit.safe_parts(path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_tar_archive_rejects_non_regular_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "unsafe.tgz"
    member = tarfile.TarInfo("package/file")
    member.type = {
        "symlink": tarfile.SYMTYPE,
        "hardlink": tarfile.LNKTYPE,
        "fifo": tarfile.FIFOTYPE,
    }[kind]
    member.linkname = "/etc/passwd"
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(member)
    with pytest.raises(audit.AuditFailure, match="regular files"):
        audit.members(path)


def test_wheel_rejects_symlink(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.whl"
    entry = zipfile.ZipInfo("memory_worlds/link")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(entry, "/etc/passwd")
    with pytest.raises(audit.AuditFailure, match="regular files"):
        audit.members(path)


def test_duplicate_tar_members_cannot_shadow_reviewed_contents(tmp_path: Path) -> None:
    path = tmp_path / "duplicates.tgz"
    with tarfile.open(path, "w:gz") as archive:
        for data in (b"reviewed", b"replacement"):
            entry = tarfile.TarInfo("package/index.js")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    with pytest.raises(audit.AuditFailure, match="Duplicate"):
        audit.members(path)


def test_compression_bomb_rejected_before_member_read(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "bomb.whl"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("memory_worlds/blob", b"a" * 4096)
    monkeypatch.setattr(audit, "MAX_MEMBER", 1024)
    with pytest.raises(audit.AuditFailure, match="size limit"):
        audit.members(path)


def test_expanded_archive_total_is_bounded(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "expanded.tgz"
    with tarfile.open(path, "w:gz") as archive:
        for index in range(4):
            entry = tarfile.TarInfo(f"package/{index}.js")
            entry.size = 1024
            archive.addfile(entry, io.BytesIO(b"a" * 1024))
    monkeypatch.setattr(audit, "MAX_ARCHIVE", 3000)
    with pytest.raises(audit.AuditFailure, match="expanded size"):
        audit.members(path)


def test_content_signatures_do_not_print_sensitive_bytes() -> None:
    secret = b"-----BEGIN " + b"RSA PRIVATE KEY-----\nprivate material"
    with pytest.raises(audit.AuditFailure) as error:
        audit.audit_content("package/leak.txt", secret)
    assert "private material" not in str(error.value)


def test_operator_private_name_deny_pattern(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_CONTENT_DENY_PATTERN", "internal-test-name")
    with pytest.raises(audit.AuditFailure, match="Configured private naming"):
        audit.audit_content("package/readme.txt", b"an INTERNAL-TEST-NAME example")


def test_wheel_license_must_match_approved_license(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "LICENSE").write_bytes(b"approved license")
    monkeypatch.setattr(audit, "ROOT", root)
    path = tmp_path / "package.whl"
    prefix = "memory_worlds-0.1.0.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("memory_worlds/__init__.py", "")
        archive.writestr("memory_worlds/setup_cli.py", "")
        archive.writestr("memory_worlds/templates/setup-graph.compose.yaml", "services: {}\n")
        archive.writestr(
            f"{prefix}/METADATA",
            "Name: memory-worlds\nVersion: 0.1.0\nLicense-Expression: Apache-2.0\n",
        )
        archive.writestr(f"{prefix}/licenses/LICENSE", b"different license")
    with pytest.raises(audit.AuditFailure, match="license differs"):
        audit.audit_distribution(path, "wheel", "0.1.0")


def test_wheel_requires_installed_setup_runtime_template(tmp_path: Path) -> None:
    path = tmp_path / "package.whl"
    prefix = "memory_worlds-0.3.0.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("memory_worlds/__init__.py", "")
        archive.writestr("memory_worlds/setup_cli.py", "")
        archive.writestr(f"{prefix}/METADATA", "Name: memory-worlds\nVersion: 0.3.0\n")
        archive.writestr(f"{prefix}/licenses/LICENSE", (audit.ROOT / "LICENSE").read_bytes())
    with pytest.raises(audit.AuditFailure, match="setup template"):
        audit.audit_distribution(path, "wheel", "0.3.0")


def test_user_home_path_signature_and_scanner_source() -> None:
    private_path = b"/ho" + b"me/alice/documents/private.txt"
    with pytest.raises(audit.AuditFailure, match="Private content signature"):
        audit.audit_content("package/config.txt", private_path)
    audit.audit_content("scripts/audit-artifacts.py", SCRIPT.read_bytes())


def test_changed_distribution_cannot_keep_old_manifest_and_checksums(tmp_path: Path) -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    filenames = [
        f"memory_worlds-{version}-py3-none-any.whl",
        f"memory_worlds-{version}.tar.gz",
        f"memory-worlds-pi-{version}.tgz",
        f"memory-worlds-opencode-{version}.tgz",
        f"memory-worlds-claude-{version}.tar.gz",
        f"memory-worlds-{version}.tgz",
    ]
    receipts = []
    checksums = []
    for name in filenames:
        data = b"original distribution"
        (tmp_path / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        receipts.append({"filename": name, "sha256": digest, "bytes": len(data)})
        checksums.append(f"{digest}  {name}\n")
    manifest = {
        "schema_version": 1,
        "project": "memory-worlds",
        "version": version,
        "source": {"commit": "a" * 40},
        "artifacts": receipts,
    }
    manifest_bytes = json.dumps(manifest).encode()
    (tmp_path / "manifest.json").write_bytes(manifest_bytes)
    checksums.append(f"{hashlib.sha256(manifest_bytes).hexdigest()}  manifest.json\n")
    (tmp_path / "SHA256SUMS").write_text("".join(checksums))
    (tmp_path / filenames[0]).write_bytes(b"replaced distribution")
    with pytest.raises(audit.AuditFailure, match="Checksum mismatch"):
        audit.audit_directory(tmp_path)


def write_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def opencode_files() -> dict[str, bytes]:
    files = {
        "package/package.json": (ROOT / "packages/opencode/package.json").read_bytes(),
        "package/README.md": b"Native OpenCode memory tools.",
        "package/LICENSE": (ROOT / "LICENSE").read_bytes(),
    }
    for name in ("index", "factory", "config", "service", "intents"):
        files[f"package/dist/{name}.js"] = b"export {};\n"
        files[f"package/dist/{name}.d.ts"] = b"export {};\n"
    return files


def claude_files() -> dict[str, bytes]:
    root = ROOT / "plugins/claude"
    return {
        "memory-worlds-claude/" + name: (root / name).read_bytes()
        for name in (
            ".claude-plugin/plugin.json",
            ".mcp.json",
            "skills/memory-worlds/SKILL.md",
            "README.md",
            "LICENSE",
        )
    }


@pytest.mark.parametrize("kind", ["opencode", "claude"])
def test_current_client_distributions_pass_audit(tmp_path, kind):
    path = tmp_path / "client.tar.gz"
    write_tar(path, opencode_files() if kind == "opencode" else claude_files())
    audit.audit_distribution(path, kind, "0.3.0")


@pytest.mark.parametrize(
    "change",
    [
        {"name": "unreviewed-package"},
        {"version": "9.9.9"},
        {"license": "unreviewed-license"},
        {"exports": {".": "./dist/factory.js"}},
        {"exports": {".": "./dist/index.js", "./factory": "./missing.js"}},
        {"exports": {".": "./dist/index.js", "./factory": "/external/private.js"}},
        {"exports": {".": "./dist/index.js", "./factory": {}}},
        {"exports": []},
    ],
)
def test_native_package_rejects_invalid_identity_or_module_exports(tmp_path, change):
    files = opencode_files()
    metadata = json.loads(files["package/package.json"])
    metadata.update(change)
    files["package/package.json"] = json.dumps(metadata).encode()
    path = tmp_path / "client.tgz"
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="metadata|module|exports"):
        audit.audit_distribution(path, "opencode", "0.3.0")


def test_native_package_requires_the_declared_modules_and_approved_license(tmp_path):
    path = tmp_path / "client.tgz"
    files = opencode_files()
    del files["package/dist/service.js"]
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="required modules"):
        audit.audit_distribution(path, "opencode", "0.3.0")
    files = opencode_files()
    files["package/LICENSE"] = b"different license"
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="license differs"):
        audit.audit_distribution(path, "opencode", "0.3.0")


@pytest.mark.parametrize("field", ["name", "version", "license"])
def test_claude_package_rejects_invalid_manifest_identity(tmp_path, field):
    files = claude_files()
    key = "memory-worlds-claude/.claude-plugin/plugin.json"
    manifest = json.loads(files[key])
    manifest[field] = "unreviewed"
    files[key] = json.dumps(manifest).encode()
    path = tmp_path / "client.tar.gz"
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="metadata mismatch"):
        audit.audit_distribution(path, "claude", "0.3.0")


@pytest.mark.parametrize("change", ["inline-token", "alternate-command", "missing-file-setting"])
def test_claude_mcp_requires_configured_runtime_and_private_credential_reference(tmp_path, change):
    files = claude_files()
    if change == "missing-file-setting":
        key = "memory-worlds-claude/.claude-plugin/plugin.json"
        manifest = json.loads(files[key])
        manifest["userConfig"]["token_file"] = {"type": "string"}
    else:
        key = "memory-worlds-claude/.mcp.json"
        manifest = json.loads(files[key])
        if change == "alternate-command":
            manifest["mcpServers"]["memory-worlds"]["command"] = "unreviewed-command"
        else:
            manifest["mcpServers"]["memory-worlds"]["args"] += ["--token", "synthetic-inline"]
    files[key] = json.dumps(manifest).encode()
    path = tmp_path / "client.tar.gz"
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="credential|module command"):
        audit.audit_distribution(path, "claude", "0.3.0")


def test_claude_archive_rejects_unexpected_files_and_license_changes(tmp_path):
    path = tmp_path / "client.tar.gz"
    files = claude_files()
    files["memory-worlds-claude/operator-settings.json"] = b"{}"
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="Claude archive requires"):
        audit.audit_distribution(path, "claude", "0.3.0")
    files = claude_files()
    files["memory-worlds-claude/LICENSE"] = b"different license"
    write_tar(path, files)
    with pytest.raises(audit.AuditFailure, match="license differs"):
        audit.audit_distribution(path, "claude", "0.3.0")


def test_claude_archive_is_deterministic_and_excludes_untracked_inputs(tmp_path, monkeypatch):
    root = tmp_path / "source"
    source = root / "plugins/claude"
    reviewed = claude_files()
    for name, content in reviewed.items():
        path = source / name.removeprefix("memory-worlds-claude/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (source / ".env").write_text("untracked operator data")
    paths = ["plugins/claude/" + name.removeprefix("memory-worlds-claude/") for name in reviewed]
    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(build, "CLAUDE", source)

    def tracked(arguments, *, capture):
        assert arguments == ["git", "ls-files", "-z", "--", "plugins/claude"]
        assert capture
        return "\0".join(reversed(paths)) + "\0"

    monkeypatch.setattr(build, "command", tracked)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    left = build.claude_archive(first, "0.3.0", 1_792_612_800)
    right = build.claude_archive(second, "0.3.0", 1_792_612_800)
    assert left.read_bytes() == right.read_bytes()
    assert audit.members(left) == reviewed
    with tarfile.open(left, "r:gz") as archive:
        for member in archive:
            assert member.uid == member.gid == 0
            assert member.mode == 0o644
            assert member.mtime == 1_792_612_800


@pytest.mark.parametrize("kind", ["opencode", "claude"])
def test_release_versions_include_both_new_client_packages(tmp_path, monkeypatch, kind):
    root = tmp_path / "source"
    for name in (
        "pyproject.toml",
        "packages/pi/package.json",
        "packages/opencode/package.json",
        "plugins/claude/.claude-plugin/plugin.json",
        "deploy/helm/memory-worlds/Chart.yaml",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    monkeypatch.setattr(build, "ROOT", root)
    monkeypatch.setattr(build, "CHART", root / "deploy/helm/memory-worlds")
    assert build.versions() == dict.fromkeys(
        ("python", "pi", "opencode", "claude", "chart", "app"), "0.3.0"
    )
    path = root / (
        "packages/opencode/package.json"
        if kind == "opencode"
        else "plugins/claude/.claude-plugin/plugin.json"
    )
    metadata = json.loads(path.read_bytes())
    metadata["version"] = "9.9.9"
    path.write_text(json.dumps(metadata))
    with pytest.raises(SystemExit, match="must match"):
        build.versions()


def receipt_directory(directory):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    filenames = {
        f"memory_worlds-{version}-py3-none-any.whl": "wheel",
        f"memory_worlds-{version}.tar.gz": "sdist",
        f"memory-worlds-pi-{version}.tgz": "pi",
        f"memory-worlds-opencode-{version}.tgz": "opencode",
        f"memory-worlds-claude-{version}.tar.gz": "claude",
        f"memory-worlds-{version}.tgz": "chart",
    }
    receipts, checksums = [], []
    for name in filenames:
        data = b"distribution contents"
        (directory / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        receipts.append({"filename": name, "sha256": digest, "bytes": len(data)})
        checksums.append(f"{digest}  {name}\n")
    manifest = {
        "schema_version": 1,
        "project": "memory-worlds",
        "version": version,
        "source": {"commit": "a" * 40},
        "artifacts": receipts,
    }
    data = json.dumps(manifest).encode()
    (directory / "manifest.json").write_bytes(data)
    checksums.append(f"{hashlib.sha256(data).hexdigest()}  manifest.json\n")
    (directory / "SHA256SUMS").write_text("".join(checksums))
    return filenames, manifest


def test_complete_receipts_audit_every_current_distribution_kind(tmp_path, monkeypatch):
    expected, _ = receipt_directory(tmp_path)
    checked = {}
    monkeypatch.setattr(
        audit,
        "audit_distribution",
        lambda path, kind, version: checked.update({path.name: kind}),
    )
    audit.audit_directory(tmp_path)
    assert checked == expected


@pytest.mark.parametrize("kind", ["wheel", "sdist", "pi", "opencode", "claude", "chart"])
def test_artifact_directory_requires_all_six_distributions(tmp_path, kind):
    files, _ = receipt_directory(tmp_path)
    next(tmp_path / name for name, item_kind in files.items() if item_kind == kind).unlink()
    with pytest.raises(audit.AuditFailure, match="exactly six"):
        audit.audit_directory(tmp_path)


def test_manifest_cannot_duplicate_a_distribution_receipt(tmp_path):
    _, manifest = receipt_directory(tmp_path)
    manifest["artifacts"].append(manifest["artifacts"][0])
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(audit.AuditFailure, match="exactly the six"):
        audit.audit_directory(tmp_path)
