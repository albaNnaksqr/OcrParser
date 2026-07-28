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


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _imports_in(
    function: ast.FunctionDef,
) -> set[tuple[int, str | None, str]]:
    return {
        (node.level, node.module, alias.name)
        for node in ast.walk(function)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


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


def test_lease_compatibility_wrappers_delegate_directly_to_scheduling() -> None:
    workers = _module(CONTROL_ROOT / "domains" / "workers" / "core.py")
    for name in LEASE_PRIMITIVES:
        imports = _imports_in(_function(workers, name))
        assert imports == {(3, "scheduling", name)}

    jobs = _module(CONTROL_ROOT / "domains" / "jobs" / "core.py")
    assert _imports_in(
        _function(jobs, "reconcile_expired_scan_unit_leases")
    ) == {(3, "scheduling", "reconcile_expired_scan_unit_leases")}
    assert _imports_in(
        _function(jobs, "reconcile_expired_shard_leases")
    ) == {(3, "scheduling", "reconcile_expired_shard_leases")}

    manifests = _module(
        CONTROL_ROOT / "domains" / "manifests" / "core.py"
    )
    for name in {
        "reconcile_expired_scan_unit_leases",
        "reconcile_expired_shard_leases",
        "scan_unit_lease_deadline",
        "shard_lease_deadline",
    }:
        assert _imports_in(_function(manifests, name)) == {
            (3, "scheduling", name)
        }
