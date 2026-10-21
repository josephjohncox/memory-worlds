"""Exercise the real mise task and setup controller, including caller-relative argv."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from memory_worlds.setup_config import SetupConfig, save_config


@pytest.mark.skipif(shutil.which("mise") is None, reason="real mise executable required")
def test_real_mise_bootstrap_validates_literal_relative_config_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    caller = tmp_path / "caller with spaces"
    caller.mkdir(mode=0o700)
    # Every expression below must reach argparse as literal filename data.
    relative = Path("config $(touch injected-dollar) `touch injected-backtick`; [literal].toml")
    provider = {
        "provider": "openai-compatible",
        "model": "explicit-local-model",
        "base_url": "http://127.0.0.1:9999/v1",
    }
    config = SetupConfig(
        data_dir=tmp_path / "absent data",
        state_dir=tmp_path / "absent state",
        extraction=provider,
        embedding=provider,
    )
    save_config(config, caller / relative)
    before = {path.name: path.read_bytes() for path in caller.iterdir()}
    environment = os.environ.copy()
    # No model or corporate endpoints are contacted by validation. Locked tools and
    # dependencies are installed by the outer mise task; prevent dependency downloads.
    environment["UV_OFFLINE"] = "1"
    environment.pop("MEMORY_WORLDS_SETUP_CWD", None)
    result = subprocess.run(
        [sys.executable, str(root / "scripts/bootstrap.py"), "validate", "--config", str(relative)],
        cwd=caller,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response == {
        "valid": True,
        "instance": config.instance,
        "config_digest": config.digest,
    }
    assert {path.name: path.read_bytes() for path in caller.iterdir()} == before
    assert not config.data_dir.exists()
    assert not config.state_dir.exists()
