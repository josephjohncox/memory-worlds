"""Executable checks for the complete rendered deployment contract."""

import importlib.util
import os
import shutil
from pathlib import Path

import pytest


def test_helm_profiles_and_rejected_unsafe_values():
    helm = os.environ.get("HELM", "helm")
    if not shutil.which(helm):
        pytest.skip("Helm is not installed")
    path = Path(__file__).resolve().parents[1] / "scripts/check-helm.py"
    spec = importlib.util.spec_from_file_location("check_helm", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = module.validate(helm)
    assert receipt["chart_license_matches_root"]


@pytest.fixture(scope="module")
def maintenance_stager():
    helm = os.environ.get("HELM", "helm")
    if not shutil.which(helm):
        pytest.skip("Helm is not installed")
    path = Path(__file__).resolve().parents[1] / "scripts/check-helm.py"
    spec = importlib.util.spec_from_file_location("check_helm_maintenance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    objects = module.render(helm, profile="maintenance")
    module.check_maintenance(objects)
    return module.api_workload(objects)["spec"]["template"]["spec"]["initContainers"][0]["args"][0]


def _projection(root, values):
    root.mkdir()
    generation = root / "..generation-one"
    generation.mkdir()
    for name, value in values.items():
        (generation / name).write_bytes(value)
        (root / name).symlink_to("..data/" + name)
    (root / "..data").symlink_to(generation.name, target_is_directory=True)
    return generation


def _stage(script, source, runtime, *, prelude=""):
    import subprocess
    import sys

    return subprocess.run(
        [sys.executable, "-I", "-c", prelude + script, str(source), str(runtime)],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_rendered_maintenance_stager_creates_private_regular_files_and_retries(
    maintenance_stager, tmp_path
):
    import stat

    from memory_worlds.maintenance_provider import WorkerProfiles
    from memory_worlds.setup_files import private_read

    source, runtime = tmp_path / "source", tmp_path / "runtime"
    runtime.mkdir()
    runtime.chmod(0o2770)  # Kubernetes fsGroup can add setgid to the emptyDir root.
    config = (
        '[maintenance.profiles.lightweight]\nendpoint="https://provider.invalid/v1"\n'
        'model="luna"\napi_key_file="' + str(runtime / "maintenance/model.key") + '"\n'
    ).encode()
    _projection(
        source,
        {
            "maintenance.toml": config,
            "model.key": b"fixture-provider-token",
            ".hidden": b"excluded",
        },
    )
    for _ in range(2):
        completed = _stage(maintenance_stager, source, runtime)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout == completed.stderr == ""
    target = runtime / "maintenance"
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert target.stat().st_uid == os.geteuid()
    assert {item.name for item in target.iterdir()} == {"maintenance.toml", "model.key"}
    for name, expected in (("maintenance.toml", config), ("model.key", b"fixture-provider-token")):
        path = target / name
        assert not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)
        assert path.stat().st_uid == os.geteuid()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1
        assert private_read(path, managed=True) == expected
    loaded = WorkerProfiles.from_env(
        {"MEMORY_WORLDS_MAINTENANCE_CONFIG": str(target / "maintenance.toml")}
    )
    assert loaded.profiles["lightweight"].route == "luna"
    assert loaded.profiles["lightweight"].api_key_file == target / "model.key"
    assert {item.name for item in runtime.iterdir()} == {"maintenance"}


@pytest.mark.parametrize(
    "damage",
    [
        "missing-config",
        "file-size",
        "total-size",
        "file-count",
        "symlink",
        "generation-escape",
        "fifo",
        "hardlink",
    ],
)
def test_rendered_stager_rejects_unsafe_or_unbounded_secret_without_diagnostics(
    maintenance_stager, tmp_path, damage
):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    runtime.mkdir()
    values = {"maintenance.toml": b"[maintenance]\n", "model.key": b"private-staging-canary"}
    if damage == "missing-config":
        del values["maintenance.toml"]
    elif damage == "file-size":
        values["model.key"] = b"x" * 262145
    elif damage == "total-size":
        values.update({f"key-{index}": b"x" * 262144 for index in range(4)})
    elif damage == "file-count":
        values.update({f"key-{index}": b"" for index in range(128)})
    generation = _projection(source, values)
    if damage in {"symlink", "fifo", "hardlink"}:
        key = generation / "model.key"
        key.unlink()
        if damage == "symlink":
            key.symlink_to(generation / "maintenance.toml")
        elif damage == "fifo":
            os.mkfifo(key)
        else:
            os.link(generation / "maintenance.toml", key)
    elif damage == "generation-escape":
        (source / "..data").unlink()
        (source / "..data").symlink_to(runtime, target_is_directory=True)
    completed = _stage(maintenance_stager, source, runtime)
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert (
        completed.stderr.strip()
        == "Maintenance Secret staging failed; verify its files, projection and limits"
    )
    assert "private-staging-canary" not in completed.stderr
    assert list(runtime.iterdir()) == []


def test_rendered_stager_pins_one_secret_generation_during_rotation(maintenance_stager, tmp_path):
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    runtime.mkdir()
    _projection(source, {"maintenance.toml": b"original-config", "model.key": b"original-key"})
    next_generation = source / "..generation-two"
    next_generation.mkdir()
    (next_generation / "maintenance.toml").write_bytes(b"next-config")
    (next_generation / "model.key").write_bytes(b"next-key")
    prelude = """import os, sys
from pathlib import Path
original_open = os.open
rotated = False
def rotate_open(path, flags, *args, **kwargs):
    global rotated
    if kwargs.get("dir_fd") is not None and not rotated:
        rotated = True
        data = Path(sys.argv[1]) / "..data"
        data.unlink()
        data.symlink_to("..generation-two", target_is_directory=True)
    return original_open(path, flags, *args, **kwargs)
os.open = rotate_open
"""
    completed = _stage(maintenance_stager, source, runtime, prelude=prelude)
    assert completed.returncode == 0, completed.stderr
    assert (runtime / "maintenance/maintenance.toml").read_bytes() == b"original-config"
    assert (runtime / "maintenance/model.key").read_bytes() == b"original-key"
    # A completed pod uses a fixed private copy. Rotation needs a new pod and emptyDir.
    repeated = _stage(maintenance_stager, source, runtime)
    assert repeated.returncode != 0
    assert (runtime / "maintenance/model.key").read_bytes() == b"original-key"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    assert _stage(maintenance_stager, source, replacement).returncode == 0
    assert (replacement / "maintenance/model.key").read_bytes() == b"next-key"
