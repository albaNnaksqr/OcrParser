from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_platform_constraints.py"
SPEC = importlib.util.spec_from_file_location("verify_platform_constraints", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_load_pins_requires_exact_unique_versions(tmp_path: Path):
    path = tmp_path / "constraints.txt"
    path.write_text("FastAPI==1.2.3\npsycopg_binary==4.5.6\n", encoding="utf-8")
    assert MODULE.load_pins(path) == {
        "fastapi": "1.2.3",
        "psycopg-binary": "4.5.6",
    }

    path.write_text("fastapi>=1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact"):
        MODULE.load_pins(path)

    path.write_text("fastapi==1\nFastAPI==2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        MODULE.load_pins(path)


def test_verify_reports_unconstrained_and_mismatched_installs(tmp_path: Path, monkeypatch):
    path = tmp_path / "constraints.txt"
    path.write_text("known==1.0\n", encoding="utf-8")
    monkeypatch.setattr(
        MODULE,
        "installed_distributions",
        lambda: {
            "known": "2.0",
            "missing": "3.0",
            "pip": "99.0",
            "ocrparser-platform": "0.4.1",
        },
    )
    assert MODULE.verify(path) == [
        "installed dependency does not match constraint: known==2.0 (expected 1.0)",
        "installed dependency is not constrained: missing==3.0",
    ]
