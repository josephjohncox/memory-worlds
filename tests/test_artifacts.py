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
