from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_ROOT = ROOT / "ocr_platform" / "control"

LEASE_PRIMITIVES = {
    "reconcile_expired_scan_unit_leases",
    "renew_running_scan_unit_leases",
    "renew_running_shard_leases",
    "scan_unit_lease_deadline",
    "shard_lease_deadline",
}
SCHEDULING_LEASE_PRIMITIVES = {
    *LEASE_PRIMITIVES,
    "reconcile_expired_shard_leases",
}


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_lease_primitives_are_owned_by_scheduling_without_domain_imports() -> None:
    scheduling = _module(CONTROL_ROOT / "scheduling.py")
    functions = {
        node.name
        for node in scheduling.body
        if isinstance(node, ast.FunctionDef)
    }
    assert SCHEDULING_LEASE_PRIMITIVES <= functions

    forbidden = {
        "domains.jobs",
        "domains.manifests",
        "domains.workers",
    }
    for node in ast.walk(scheduling):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(
                module == target or module.startswith(f"{target}.")
                for target in forbidden
            )


def test_domain_compatibility_facades_are_removed() -> None:
    for domain in ("jobs", "workers", "manifests", "model_profiles"):
        assert not (
            CONTROL_ROOT / "domains" / domain / "core.py"
        ).exists()
