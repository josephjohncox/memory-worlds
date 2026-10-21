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
