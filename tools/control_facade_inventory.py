#!/usr/bin/env python3
"""Enforce the v0.4 removal of ``ocr_platform.control.service``.

The pre-removal symbol inventory has been resolved into the checked-in
``consumed_symbol_migrations`` table.  This gate now treats the old module as a
tombstone: the package must not exist and repository code must not import,
dynamically load, embed an import of, or monkeypatch it.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "control_facade_inventory.json"
)
TARGET_MODULE = "ocr_platform.control.service"
TARGET_PARENT = "ocr_platform.control"
EXCLUDED_SCANNER_PATHS = {
    "tests/test_control_facade_inventory.py",
    "tools/control_facade_inventory.py",
}
SCANNED_ROOTS = ("ocr_platform", "ocr_parser", "tests", "tools")


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _constant_string(
    node: ast.AST | None,
    bindings: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left, bindings)
        right = _constant_string(node.right, bindings)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _constant_string(value.value, bindings)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    return None


def _module_bindings(tree: ast.Module) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = _constant_string(statement.value, bindings)
        if value is None:
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = value
    return bindings


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _dynamic_target(
    node: ast.Call,
    bindings: dict[str, str],
) -> str | None:
    call_name = _call_name(node.func)
    if call_name not in {
        "__import__",
        "import_module",
        "importlib.import_module",
        "load_module",
    }:
        return None
    argument = node.args[0] if node.args else None
    if argument is None:
        for keyword in node.keywords:
            if keyword.arg in {"name", "module"}:
                argument = keyword.value
                break
    target = _constant_string(argument, bindings)
    if target == TARGET_MODULE:
        return target
    if target != ".service":
        return None
    package_node = node.args[1] if len(node.args) > 1 else None
    if package_node is None:
        for keyword in node.keywords:
            if keyword.arg == "package":
                package_node = keyword.value
                break
    return (
        TARGET_MODULE
        if _constant_string(package_node, bindings) == TARGET_PARENT
        else None
    )


def _is_relative_service_import(
    path: Path,
    node: ast.ImportFrom,
    root: Path,
) -> bool:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    if not relative.startswith("ocr_platform/control/"):
        return False
    if node.level <= 0:
        return False
    package = relative.removesuffix(".py").split("/")[:-1]
    ascend = node.level - 1
    if ascend > len(package):
        return False
    prefix = package[: len(package) - ascend]
    module_parts = (
        node.module.split(".")
        if node.module not in {None, ""}
        else []
    )
    if module_parts:
        resolved = ".".join([*prefix, *module_parts])
        return resolved == TARGET_MODULE
    return (
        ".".join(prefix) == TARGET_PARENT
        and any(alias.name == "service" for alias in node.names)
    )


def _scan_python(path: Path, root: Path) -> list[dict[str, Any]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    bindings = _module_bindings(tree)
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.as_posix()
    sites: list[dict[str, Any]] = []

    def add(node: ast.AST, kind: str, detail: str) -> None:
        sites.append(
            {
                "path": relative,
                "line": int(getattr(node, "lineno", 0)),
                "kind": kind,
                "detail": detail,
            }
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == TARGET_MODULE:
                    add(node, "ast_import", alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == TARGET_MODULE:
                add(node, "ast_import", TARGET_MODULE)
            elif (
                node.module == TARGET_PARENT
                and any(alias.name == "service" for alias in node.names)
            ):
                add(node, "ast_import", f"{TARGET_PARENT}.service")
            elif _is_relative_service_import(path, node, root):
                add(node, "ast_import", "relative service import")
        elif isinstance(node, ast.Call):
            target = _dynamic_target(node, bindings)
            if target is not None:
                add(node, "dynamic_import", target)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if (
                f"import {TARGET_MODULE}" in value
                or f"from {TARGET_MODULE} import" in value
                or f"from {TARGET_PARENT} import service" in value
            ):
                add(node, "embedded_import", TARGET_MODULE)
            elif value.startswith(f"{TARGET_MODULE}."):
                add(node, "string_reference", value)

    return sites


def scan_references(root: Path = ROOT) -> list[dict[str, Any]]:
    sites: list[dict[str, Any]] = []
    for root_name in SCANNED_ROOTS:
        scan_root = root / root_name
        if not scan_root.exists():
            continue
        for path in sorted(scan_root.rglob("*.py")):
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = path.as_posix()
            if relative in EXCLUDED_SCANNER_PATHS:
                continue
            sites.extend(_scan_python(path, root))
    return sorted(
        sites,
        key=lambda item: (
            item["path"],
            item["line"],
            item["kind"],
            item["detail"],
        ),
    )


def build_facade_inventory(root: Path = ROOT) -> dict[str, Any]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    package_path = root / "ocr_platform" / "control" / "service"
    module_path = root / "ocr_platform" / "control" / "service.py"
    sites = scan_references(root)
    return {
        "schema_version": 2,
        "builder": (
            "tools.control_facade_inventory.build_facade_inventory"
        ),
        "target_module": TARGET_MODULE,
        "facade_exists": package_path.exists() or module_path.exists(),
        "reference_count": len(sites),
        "references": sites,
        "consumed_symbol_migrations": fixture[
            "consumed_symbol_migrations"
        ],
    }


def validate_fixture_shape(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 2:
        raise ValueError("façade tombstone schema_version must be 2")
    migrations = payload.get("consumed_symbol_migrations")
    if not isinstance(migrations, list):
        raise ValueError("consumed symbol migration map is missing")
    symbols = [item.get("symbol") for item in migrations]
    if len(symbols) != 24 or len(set(symbols)) != 24:
        raise ValueError(
            "consumed symbol migration map must contain 24 unique symbols"
        )
    if any(
        item.get("status") != "migrated"
        or item.get("target") in {None, ""}
        for item in migrations
    ):
        raise ValueError(
            "every consumed façade symbol must have a completed target"
        )
    references = payload.get("references")
    if not isinstance(references, list):
        raise ValueError("façade reference list is missing")
    if payload.get("reference_count") != len(references):
        raise ValueError("façade reference count is inconsistent")


def validate_removed(payload: dict[str, Any]) -> None:
    validate_fixture_shape(payload)
    if payload["facade_exists"]:
        raise ValueError(
            "ocr_platform.control.service must be removed"
        )
    if payload["references"]:
        evidence = ", ".join(
            f"{site['path']}:{site['line']} ({site['kind']})"
            for site in payload["references"]
        )
        raise ValueError(
            "legacy Control service façade references are forbidden: "
            + evidence
        )


def refresh() -> None:
    payload = build_facade_inventory()
    validate_removed(payload)
    FIXTURE.write_text(canonical_json(payload), encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)}")


def check() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    validate_removed(expected)
    actual = build_facade_inventory()
    validate_removed(actual)
    if actual != expected:
        raise SystemExit(
            "Control service façade tombstone fixture is stale; run "
            "python tools/control_facade_inventory.py refresh"
        )
    print("Control service façade is removed and has no repository references.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices={"check", "refresh"})
    args = parser.parse_args()
    if args.command == "refresh":
        refresh()
    else:
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
