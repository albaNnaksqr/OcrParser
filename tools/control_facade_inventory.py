#!/usr/bin/env python3
"""Inventory and enforce decreasing use of the legacy Control service façade."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


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
CATEGORIES = {
    "supported_explicit_target",
    "settings_pending",
    "scheduling_application_pending",
    "internal_no_compat",
    "unsupported_leaked",
}

CONSUMED_MIGRATIONS: dict[str, dict[str, str | None]] = {
    "database": {
        "classification": "supported_explicit_target",
        "target": "ocr_platform.control.database",
        "wave": "PR 8",
        "reason": "Import the existing Control database module directly.",
    },
    "ShardAttemptConflictError": {
        "classification": "supported_explicit_target",
        "target": (
            "ocr_platform.control.domains.manifests.commands."
            "ShardAttemptConflictError"
        ),
        "wave": "PR 8",
        "reason": "Use the owning manifest command surface.",
    },
    "claim_next_pending_shard": {
        "classification": "supported_explicit_target",
        "target": (
            "ocr_platform.control.domains.manifests.commands."
            "claim_next_pending_shard"
        ),
        "wave": "PR 8",
        "reason": "Use the existing manifest command surface.",
    },
    "claim_next_scan_unit": {
        "classification": "supported_explicit_target",
        "target": (
            "ocr_platform.control.domains.manifests.commands."
            "claim_next_scan_unit"
        ),
        "wave": "PR 8",
        "reason": "Use the existing manifest command surface.",
    },
    "complete_scan_unit": {
        "classification": "supported_explicit_target",
        "target": (
            "ocr_platform.control.domains.manifests.commands."
            "complete_scan_unit"
        ),
        "wave": "PR 8",
        "reason": "Use the existing manifest command surface.",
    },
    "update_work_shard": {
        "classification": "supported_explicit_target",
        "target": (
            "ocr_platform.control.domains.manifests.commands."
            "update_work_shard"
        ),
        "wave": "PR 8",
        "reason": "Use the existing manifest command surface.",
    },
    "create_job": {
        "classification": "supported_explicit_target",
        "target": "ocr_platform.control.domains.jobs.commands.create_job",
        "wave": "PR 8",
        "reason": "Use the existing Job command surface.",
    },
    "infer_failure_category": {
        "classification": "supported_explicit_target",
        "target": "ocr_parser.infra.failure_category.infer_failure_category",
        "wave": "PR 8",
        "reason": "Import the owning parser utility directly.",
    },
    "upsert_model_profile": {
        "classification": "supported_explicit_target",
        "target": (
            "ocr_platform.control.domains.model_profiles.commands."
            "upsert_model_profile"
        ),
        "wave": "PR 8",
        "reason": "Use the existing Model Profile command surface.",
    },
    "STALE_AFTER_SECONDS": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "job_stale_after_seconds"
        ),
        "wave": "PR 2",
        "reason": "Replace import-time environment state with ControlSettings.",
    },
    "SERVER_STALE_AFTER_SECONDS": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "server_stale_after_seconds"
        ),
        "wave": "PR 2",
        "reason": "Replace import-time environment state with ControlSettings.",
    },
    "SHARD_LEASE_SECONDS": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "shard_lease_seconds"
        ),
        "wave": "PR 2",
        "reason": "Replace import-time environment state with ControlSettings.",
    },
    "JOB_FILE_DETAIL_LIMIT": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "job_file_detail_limit"
        ),
        "wave": "PR 2",
        "reason": "Inject an immutable detail limit instead of monkeypatching.",
    },
    "JOB_EVENT_DETAIL_LIMIT": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "job_event_detail_limit"
        ),
        "wave": "PR 2",
        "reason": "Inject an immutable detail limit instead of monkeypatching.",
    },
    "JOB_LOG_DETAIL_LIMIT": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "job_log_detail_limit"
        ),
        "wave": "PR 2",
        "reason": "Inject an immutable detail limit instead of monkeypatching.",
    },
    "SCAN_UNIT_CLAIM_BATCH_SIZE": {
        "classification": "settings_pending",
        "target": (
            "planned:ocr_platform.control.settings.ControlSettings."
            "scan_unit_claim_batch_size"
        ),
        "wave": "PR 2",
        "reason": "Inject the scan claim limit instead of monkeypatching.",
    },
    "_claimable_scan_unit_id_select": {
        "classification": "scheduling_application_pending",
        "target": (
            "planned:ocr_platform.control.scheduling.queries."
            "claimable_scan_unit_id_select"
        ),
        "wave": "PR 6",
        "reason": "Move selector ownership into the scheduling kernel.",
    },
    "_claimable_shard_id_select": {
        "classification": "scheduling_application_pending",
        "target": (
            "planned:ocr_platform.control.scheduling.queries."
            "claimable_shard_id_select"
        ),
        "wave": "PR 6",
        "reason": "Move selector ownership into the scheduling kernel.",
    },
    "_manifest_for_scan_unit_completion_select": {
        "classification": "scheduling_application_pending",
        "target": (
            "planned:ocr_platform.control.scheduling.queries."
            "manifest_for_scan_unit_completion_select"
        ),
        "wave": "PR 6",
        "reason": "Move completion locking into the scheduling kernel.",
    },
    "_database_migration_preflight_issue": {
        "classification": "scheduling_application_pending",
        "target": (
            "planned:ocr_platform.control.application.diagnostics."
            "database_migration_preflight_issue"
        ),
        "wave": "PR 2",
        "reason": "Expose migration readiness through an application use case.",
    },
    "POOL_SERVER_ID": {
        "classification": "internal_no_compat",
        "target": None,
        "wave": "PR 8",
        "reason": "Stop treating an internal scheduler sentinel as integration API.",
    },
    "json_loads_object": {
        "classification": "internal_no_compat",
        "target": None,
        "wave": "PR 8",
        "reason": "Tests must not integrate through an internal JSON helper.",
    },
    "utcnow": {
        "classification": "internal_no_compat",
        "target": None,
        "wave": "PR 8",
        "reason": "Tests should inject a clock or use the owning internal helper.",
    },
}


def canonical_json(payload: Any) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    )


def _fingerprint(payload: Any) -> str:
    if isinstance(payload, ast.AST):
        normalized = ast.dump(
            payload,
            annotate_fields=True,
            include_attributes=False,
        )
    else:
        normalized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _RawSite:
    path: str
    function: str
    operation: str
    fingerprint: str
    line: int
    column: int
    details: dict[str, Any]


def _finalize_sites(raw_sites: Iterable[_RawSite]) -> list[dict[str, Any]]:
    ordered = sorted(
        raw_sites,
        key=lambda site: (
            site.path,
            site.function,
            site.operation,
            site.line,
            site.column,
            site.fingerprint,
        ),
    )
    ordinals: Counter[tuple[str, str, str, str]] = Counter()
    result: list[dict[str, Any]] = []
    for site in ordered:
        stable_operation = f"{site.operation}@{site.fingerprint[:12]}"
        key = (
            site.path,
            site.function,
            stable_operation,
            site.fingerprint,
        )
        ordinals[key] += 1
        result.append(
            {
                "id": (
                    f"{site.path}:{site.function}:{stable_operation}:"
                    f"{ordinals[key]}"
                ),
                "fingerprint": site.fingerprint,
                "source": f"{site.path}:{site.line}",
                **site.details,
            }
        )
    return result


class _FunctionAwareVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_stack: list[str] = []

    @property
    def function(self) -> str:
        return ".".join(self.function_stack) if self.function_stack else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_import_from(
    *,
    current_module: str,
    is_package: bool,
    node: ast.ImportFrom,
) -> str:
    if node.level == 0:
        return node.module or ""
    package = (
        current_module
        if is_package
        else current_module.rsplit(".", 1)[0]
    )
    package_parts = package.split(".") if package else []
    keep = len(package_parts) - (node.level - 1)
    if keep < 0:
        return ""
    suffix = (node.module or "").split(".") if node.module else []
    return ".".join([*package_parts[:keep], *suffix])


class _FacadeConsumerVisitor(_FunctionAwareVisitor):
    def __init__(self, path: Path, root: Path) -> None:
        super().__init__()
        self.path = path
        self.module = _module_name(path, root)
        self.is_package = path.name == "__init__.py"
        self.relative_path = str(path.relative_to(root))
        self.service_alias_stack: list[set[str]] = [set()]
        self.imported_symbol_stack: list[dict[str, str]] = [{}]
        self.constant_stack: list[dict[str, str]] = [{}]
        self.dynamic_callable_stack: list[dict[str, str]] = [
            {"__import__": "__import__"}
        ]
        self.importlib_alias_stack: list[set[str]] = [set()]
        self.imports: list[_RawSite] = []
        self.dynamic_imports: list[_RawSite] = []
        self.symbol_uses: list[_RawSite] = []
        self.monkeypatches: list[_RawSite] = []

    @property
    def service_aliases(self) -> set[str]:
        return self.service_alias_stack[-1]

    @property
    def imported_symbols(self) -> dict[str, str]:
        return self.imported_symbol_stack[-1]

    @property
    def constants(self) -> dict[str, str]:
        return self.constant_stack[-1]

    @property
    def dynamic_callables(self) -> dict[str, str]:
        return self.dynamic_callable_stack[-1]

    @property
    def importlib_aliases(self) -> set[str]:
        return self.importlib_alias_stack[-1]

    @staticmethod
    def _argument_names(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> set[str]:
        return {
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        } | (
            {node.args.vararg.arg} if node.args.vararg is not None else set()
        ) | (
            {node.args.kwarg.arg} if node.args.kwarg is not None else set()
        )

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.function_stack.append(node.name)
        aliases = set(self.service_aliases)
        symbols = dict(self.imported_symbols)
        constants = dict(self.constants)
        dynamic_callables = dict(self.dynamic_callables)
        importlib_aliases = set(self.importlib_aliases)
        for argument in self._argument_names(node):
            aliases.discard(argument)
            symbols.pop(argument, None)
            constants.pop(argument, None)
            dynamic_callables.pop(argument, None)
            importlib_aliases.discard(argument)
        self.service_alias_stack.append(aliases)
        self.imported_symbol_stack.append(symbols)
        self.constant_stack.append(constants)
        self.dynamic_callable_stack.append(dynamic_callables)
        self.importlib_alias_stack.append(importlib_aliases)
        self.generic_visit(node)
        self.importlib_alias_stack.pop()
        self.dynamic_callable_stack.pop()
        self.constant_stack.pop()
        self.imported_symbol_stack.pop()
        self.service_alias_stack.pop()
        self.function_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node)

    def _static_string(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.constants.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._static_string(node.left)
            right = self._static_string(node.right)
            return (
                f"{left}{right}"
                if left is not None and right is not None
                else None
            )
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(
                    value.value,
                    str,
                ):
                    parts.append(value.value)
                    continue
                if isinstance(value, ast.FormattedValue):
                    resolved = self._static_string(value.value)
                    if resolved is not None:
                        parts.append(resolved)
                        continue
                return None
            return "".join(parts)
        return None

    def _dynamic_import_target(self, node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        kind: str | None = None
        if isinstance(node.func, ast.Name):
            kind = self.dynamic_callables.get(node.func.id)
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.importlib_aliases
        ):
            kind = "import_module"
        if kind is None:
            return None
        name_node = (
            node.args[0]
            if node.args
            else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "name"
                ),
                None,
            )
        )
        if name_node is None:
            return None
        name = self._static_string(name_node)
        if name is None:
            return None
        if kind != "import_module" or not name.startswith("."):
            return name
        package_node = (
            node.args[1]
            if len(node.args) >= 2
            else next(
                (
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg == "package"
                ),
                None,
            )
        )
        if package_node is None:
            return None
        package = self._static_string(package_node)
        if not package:
            return None
        level = len(name) - len(name.lstrip("."))
        suffix = name[level:]
        package_parts = package.split(".")
        keep = len(package_parts) - (level - 1)
        if keep <= 0:
            return None
        return ".".join(
            [*package_parts[:keep], *([suffix] if suffix else [])]
        )

    def _is_service_expr(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Name)
            and node.id in self.service_aliases
        ) or self._dynamic_import_target(node) == TARGET_MODULE

    def _site(
        self,
        node: ast.AST,
        operation: str,
        details: dict[str, Any],
        *,
        fingerprint: str | None = None,
    ) -> _RawSite:
        return _RawSite(
            path=self.relative_path,
            function=self.function,
            operation=operation,
            fingerprint=fingerprint or _fingerprint(node),
            line=int(getattr(node, "lineno")),
            column=int(getattr(node, "col_offset")),
            details=details,
        )

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            if alias.name == "importlib":
                self.importlib_aliases.add(
                    alias.asname or "importlib"
                )
        aliases = [
            alias
            for alias in node.names
            if alias.name == TARGET_MODULE
        ]
        if aliases:
            for alias in aliases:
                if alias.asname:
                    self.service_aliases.add(alias.asname)
            self.imports.append(
                self._site(
                    node,
                    "facade-import",
                    {
                        "kind": "import",
                        "symbols": [],
                        "wildcard": False,
                    },
                    fingerprint=_fingerprint(
                        {
                            "kind": "import",
                            "aliases": [
                                {
                                    "name": alias.name,
                                    "asname": alias.asname,
                                }
                                for alias in aliases
                            ],
                        }
                    ),
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        imported_from = _resolve_import_from(
            current_module=self.module,
            is_package=self.is_package,
            node=node,
        )
        if imported_from == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    self.dynamic_callables[
                        alias.asname or alias.name
                    ] = "import_module"
        if imported_from == "builtins":
            for alias in node.names:
                if alias.name == "__import__":
                    self.dynamic_callables[
                        alias.asname or alias.name
                    ] = "__import__"
        imports_service_module = (
            imported_from == TARGET_PARENT
            and any(alias.name == "service" for alias in node.names)
        )
        imports_service_symbols = imported_from == TARGET_MODULE
        if imports_service_module:
            for alias in node.names:
                if alias.name == "service":
                    self.service_aliases.add(alias.asname or alias.name)
        if imports_service_symbols:
            for alias in node.names:
                if alias.name != "*":
                    self.imported_symbols[alias.asname or alias.name] = (
                        alias.name
                    )
                    self.symbol_uses.append(
                        self._site(
                            node,
                            f"facade-symbol-import[{alias.name}]",
                            {
                                "symbol": alias.name,
                                "kind": "direct_import",
                            },
                            fingerprint=_fingerprint(
                                {
                                    "module": TARGET_MODULE,
                                    "level": node.level,
                                    "symbol": alias.name,
                                    "alias": alias.asname,
                                }
                            ),
                        )
                    )
        if imports_service_module or imports_service_symbols:
            symbols = (
                sorted(
                    alias.name
                    for alias in node.names
                    if imports_service_symbols and alias.name != "*"
                )
            )
            self.imports.append(
                self._site(
                    node,
                    "facade-import",
                    {
                        "kind": (
                            "from_service"
                            if imports_service_symbols
                            else "from_parent"
                        ),
                        "symbols": symbols,
                        "wildcard": any(
                            alias.name == "*" for alias in node.names
                        ),
                    },
                    fingerprint=_fingerprint(
                        {
                            "kind": (
                                "from_service"
                                if imports_service_symbols
                                else "from_parent"
                            ),
                            "module": node.module,
                            "resolved_module": imported_from,
                            "level": node.level,
                            "service_aliases": (
                                [
                                    {
                                        "name": alias.name,
                                        "asname": alias.asname,
                                    }
                                    for alias in node.names
                                    if alias.name == "service"
                                ]
                                if imports_service_module
                                else []
                            ),
                            "wildcard": any(
                                alias.name == "*" for alias in node.names
                            ),
                        }
                    ),
                )
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in self.service_aliases
        ):
            self.symbol_uses.append(
                self._site(
                    node,
                    f"facade-symbol-use[{node.attr}]",
                    {"symbol": node.attr, "kind": "attribute"},
                )
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        if (
            isinstance(node.ctx, ast.Load)
            and node.id in self.imported_symbols
        ):
            symbol = self.imported_symbols[node.id]
            self.symbol_uses.append(
                self._site(
                    node,
                    f"facade-symbol-use[{symbol}]",
                    {"symbol": symbol, "kind": "direct_name"},
                )
            )

    def visit_Call(self, node: ast.Call) -> Any:
        if self._dynamic_import_target(node) == TARGET_MODULE:
            self.dynamic_imports.append(
                self._site(
                    node,
                    "facade-dynamic-import",
                    {"kind": "dynamic_import"},
                )
            )

        callable_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if (
            callable_name == "getattr"
            and len(node.args) >= 2
            and self._is_service_expr(node.args[0])
        ):
            symbol = self._static_string(node.args[1])
            if symbol:
                self.symbol_uses.append(
                    self._site(
                        node,
                        f"facade-symbol-use[{symbol}]",
                        {"symbol": symbol, "kind": "getattr"},
                    )
                )

        is_setattr = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"delattr", "patch", "setattr"}
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in {"delattr", "patch", "setattr"}
        )
        is_patch_object = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "object"
            and (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "patch"
                or isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "patch"
            )
        )
        if (is_setattr or is_patch_object) and node.args:
            symbol: str | None = None
            form: str | None = None
            if (
                len(node.args) >= 2
                and self._is_service_expr(node.args[0])
            ):
                symbol = self._static_string(node.args[1])
                form = "object"
            else:
                string_target = self._static_string(node.args[0])
                if (
                    string_target is not None
                    and string_target.startswith(f"{TARGET_MODULE}.")
                ):
                    symbol = string_target[len(TARGET_MODULE) + 1 :].split(
                        ".",
                        1,
                    )[0]
                    form = "string"
            if symbol and form:
                self.monkeypatches.append(
                    self._site(
                        node,
                        f"facade-monkeypatch[{symbol}]",
                        {"symbol": symbol, "form": form},
                    )
                )
        is_patch_multiple = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "multiple"
            and (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "patch"
                or isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "patch"
            )
        )
        if is_patch_multiple and node.args:
            string_target = self._static_string(node.args[0])
            targets_service = self._is_service_expr(node.args[0]) or (
                string_target == TARGET_MODULE
            )
            if targets_service:
                for keyword in node.keywords:
                    if keyword.arg is None:
                        continue
                    self.monkeypatches.append(
                        self._site(
                            node,
                            f"facade-monkeypatch[{keyword.arg}]",
                            {
                                "symbol": keyword.arg,
                                "form": "multiple",
                            },
                            fingerprint=_fingerprint(
                                {
                                    "call": ast.dump(
                                        node,
                                        annotate_fields=True,
                                        include_attributes=False,
                                    ),
                                    "symbol": keyword.arg,
                                }
                            ),
                        )
                    )
        self.generic_visit(node)

    def _record_assignment(
        self,
        targets: Iterable[ast.expr],
        value: ast.AST | None,
    ) -> None:
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if (
                isinstance(value, ast.Name)
                and value.id in self.service_aliases
            ) or (
                value is not None
                and self._dynamic_import_target(value) == TARGET_MODULE
            ):
                self.service_aliases.add(name)
            else:
                self.service_aliases.discard(name)
            if (
                isinstance(value, ast.Name)
                and value.id in self.imported_symbols
            ):
                self.imported_symbols[name] = self.imported_symbols[
                    value.id
                ]
            else:
                self.imported_symbols.pop(name, None)
            dynamic_kind: str | None = None
            if (
                isinstance(value, ast.Name)
                and value.id in self.dynamic_callables
            ):
                dynamic_kind = self.dynamic_callables[value.id]
            elif (
                isinstance(value, ast.Attribute)
                and value.attr == "import_module"
                and isinstance(value.value, ast.Name)
                and value.value.id in self.importlib_aliases
            ):
                dynamic_kind = "import_module"
            if dynamic_kind is None:
                self.dynamic_callables.pop(name, None)
            else:
                self.dynamic_callables[name] = dynamic_kind
            if (
                isinstance(value, ast.Name)
                and value.id in self.importlib_aliases
            ):
                self.importlib_aliases.add(name)
            else:
                self.importlib_aliases.discard(name)
            resolved = (
                self._static_string(value)
                if value is not None
                else None
            )
            if resolved is None:
                self.constants.pop(name, None)
            else:
                self.constants[name] = resolved

    def visit_Assign(self, node: ast.Assign) -> Any:
        # Visit the value while the previous bindings are still visible, then
        # update bindings for statements that follow.
        self.visit(node.value)
        self._record_assignment(node.targets, node.value)
        for target in node.targets:
            self.visit(target)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        if node.value is not None:
            self.visit(node.value)
        self._record_assignment([node.target], node.value)
        self.visit(node.target)
        self.visit(node.annotation)


def _source_category(path: str) -> str:
    if path.startswith("tests/"):
        return "test"
    if path.startswith("tools/"):
        return "tool"
    return "production"


def _python_paths(root: Path) -> list[Path]:
    excluded = {".git", ".tox", ".venv", "build", "dist", "__pycache__"}
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part in excluded for part in path.parts)
    ]


def _embedded_sites(
    path: Path,
    root: Path,
    tree: ast.Module,
) -> tuple[list[_RawSite], list[_RawSite]]:
    import_sites: list[_RawSite] = []
    symbol_sites: list[_RawSite] = []
    relative = str(path.relative_to(root))
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (
                TARGET_MODULE in node.value
                or f"from {TARGET_PARENT} import service" in node.value
            )
        ):
            continue
        try:
            embedded_tree = ast.parse(node.value)
        except SyntaxError:
            embedded_tree = None
        if embedded_tree is None:
            continue
        aliases: set[str] = set()
        direct_symbols: list[tuple[str, str | None, ast.ImportFrom]] = []
        target_imports: list[tuple[ast.AST, dict[str, Any]]] = []
        for child in ast.walk(embedded_tree):
            if isinstance(child, ast.Import):
                relevant = [
                    alias
                    for alias in child.names
                    if alias.name == TARGET_MODULE
                ]
                if relevant:
                    aliases.update(
                        alias.asname or alias.name.split(".")[-1]
                        for alias in relevant
                    )
                    target_imports.append(
                        (
                            child,
                            {
                                "kind": "import",
                                "aliases": [
                                    {
                                        "name": alias.name,
                                        "asname": alias.asname,
                                    }
                                    for alias in relevant
                                ],
                            },
                        )
                    )
            elif (
                isinstance(child, ast.ImportFrom)
                and child.module == TARGET_PARENT
            ):
                relevant = [
                    alias
                    for alias in child.names
                    if alias.name == "service"
                ]
                if relevant:
                    aliases.update(
                        alias.asname or alias.name for alias in relevant
                    )
                    target_imports.append(
                        (
                            child,
                            {
                                "kind": "from_parent",
                                "module": child.module,
                                "level": child.level,
                                "service_aliases": [
                                    {
                                        "name": alias.name,
                                        "asname": alias.asname,
                                    }
                                    for alias in relevant
                                ],
                            },
                        )
                    )
            elif (
                isinstance(child, ast.ImportFrom)
                and child.module == TARGET_MODULE
            ):
                direct_symbols.extend(
                    (alias.name, alias.asname, child)
                    for alias in child.names
                    if alias.name != "*"
                )
                target_imports.append(
                    (
                        child,
                        {
                            "kind": "from_service",
                            "module": child.module,
                            "level": child.level,
                            "wildcard": any(
                                alias.name == "*" for alias in child.names
                            ),
                        },
                    )
                )
            elif isinstance(child, ast.Call):
                dynamic = (
                    isinstance(child.func, ast.Name)
                    and child.func.id in {"__import__", "import_module"}
                ) or (
                    isinstance(child.func, ast.Attribute)
                    and child.func.attr == "import_module"
                )
                if (
                    dynamic
                    and child.args
                    and isinstance(child.args[0], ast.Constant)
                    and child.args[0].value == TARGET_MODULE
                ):
                    target_imports.append(
                        (
                            child,
                            {
                                "kind": "dynamic_import",
                                "module": TARGET_MODULE,
                            },
                        )
                    )
        if not target_imports:
            continue
        attribute_uses = [
            child
            for child in ast.walk(embedded_tree)
            if isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id in aliases
        ]
        all_symbols = {
            symbol for symbol, _, _ in direct_symbols
        } | {child.attr for child in attribute_uses}
        for _, import_payload in target_imports:
            import_sites.append(
                _RawSite(
                    path=relative,
                    function="<embedded>",
                    operation="facade-embedded-import",
                    fingerprint=_fingerprint(import_payload),
                    line=node.lineno,
                    column=node.col_offset,
                    details={
                        "kind": "embedded_subprocess",
                        "symbols": sorted(all_symbols),
                    },
                )
            )
        for symbol, alias, _ in direct_symbols:
            symbol_sites.append(
                _RawSite(
                    path=relative,
                    function="<embedded>",
                    operation=f"facade-symbol-use[{symbol}]",
                    fingerprint=_fingerprint(
                        {
                            "module": TARGET_MODULE,
                            "symbol": symbol,
                            "alias": alias,
                        }
                    ),
                    line=node.lineno,
                    column=node.col_offset,
                    details={
                        "symbol": symbol,
                        "kind": "embedded",
                    },
                )
            )
        for attribute in attribute_uses:
            symbol_sites.append(
                _RawSite(
                    path=relative,
                    function="<embedded>",
                    operation=f"facade-symbol-use[{attribute.attr}]",
                    fingerprint=_fingerprint(attribute),
                    line=node.lineno,
                    column=node.col_offset,
                    details={
                        "symbol": attribute.attr,
                        "kind": "embedded",
                    },
                )
            )
    return import_sites, symbol_sites


def _runtime_exports(root: Path) -> list[dict[str, Any]]:
    script = """
import importlib
import json
import sys

service = importlib.import_module(sys.argv[1])
owner_modules = [
    importlib.import_module("ocr_platform.control.domains.common"),
    importlib.import_module("ocr_platform.control.domains.jobs.core"),
    importlib.import_module("ocr_platform.control.domains.manifests.core"),
    importlib.import_module("ocr_platform.control.domains.model_profiles.core"),
    importlib.import_module("ocr_platform.control.domains.workers.core"),
]
rows = []
for name, value in vars(service).items():
    if name.startswith("__"):
        continue
    owners = [
        module.__name__
        for module in owner_modules
        if name in vars(module) and vars(module)[name] is value
    ]
    rows.append({
        "symbol": name,
        "kind": type(value).__name__,
        "defining_module": getattr(value, "__module__", None),
        "owners": sorted(owners),
    })
print(json.dumps(sorted(rows, key=lambda row: row["symbol"])))
"""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(root)
        if not existing
        else f"{root}{os.pathsep}{existing}"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, TARGET_MODULE],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return list(json.loads(completed.stdout))


def _all_declaration(
    tree: ast.Module,
) -> tuple[bool, set[str] | None]:
    for node in tree.body:
        if not (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            )
        ):
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple)) and all(
            isinstance(item, ast.Constant)
            and isinstance(item.value, str)
            for item in value.elts
        ):
            return True, {str(item.value) for item in value.elts}
        return True, None
    return False, None


def _surface_targets(root: Path) -> dict[str, str]:
    candidates: dict[str, list[tuple[int, str]]] = {}
    domain_root = root / "ocr_platform" / "control" / "domains"
    rank = {"commands.py": 0, "queries.py": 1, "schemas.py": 2}
    for path in sorted(domain_root.glob("*/*.py")):
        if path.name not in rank:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local_names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        local_names.update(
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        )
        has_all, explicit_all = _all_declaration(tree)
        if explicit_all is not None:
            exposed = explicit_all
        elif has_all:
            exposed = {
                name for name in local_names if not name.startswith("_")
            }
        else:
            # A schema module without __all__ owns only its definitions.
            # Imported SQLAlchemy, typing, datetime, and model names are
            # implementation dependencies, not supported Python targets.
            exposed = {
                node.name
                for node in tree.body
                if isinstance(
                    node,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
                )
                and not node.name.startswith("_")
            }
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        for symbol in exposed:
            candidates.setdefault(symbol, []).append(
                (rank[path.name], f"{module}.{symbol}")
            )

    central_schema = root / "ocr_platform" / "control" / "schemas.py"
    schema_tree = ast.parse(central_schema.read_text(encoding="utf-8"))
    for node in schema_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                candidates.setdefault(node.name, []).append(
                    (3, f"ocr_platform.control.schemas.{node.name}")
                )
    return {
        symbol: sorted(targets)[0][1]
        for symbol, targets in candidates.items()
    }


def _classify_export(
    export: dict[str, Any],
    *,
    surface_targets: dict[str, str],
) -> dict[str, Any]:
    symbol = str(export["symbol"])
    if symbol in CONSUMED_MIGRATIONS:
        migration = CONSUMED_MIGRATIONS[symbol]
        return {
            **export,
            **migration,
            "consumed": True,
        }
    if symbol in surface_targets:
        return {
            **export,
            "classification": "supported_explicit_target",
            "target": surface_targets[symbol],
            "wave": "PR 7/8",
            "reason": "An explicit domain command/query/schema target exists.",
            "consumed": False,
        }
    defining_module = export.get("defining_module")
    owners = list(export.get("owners") or [])
    if (
        symbol.startswith("_")
        or defining_module == "ocr_platform.control.service"
    ):
        return {
            **export,
            "classification": "internal_no_compat",
            "target": None,
            "wave": "PR 8",
            "reason": "Internal Control implementation detail; no compatibility target.",
            "consumed": False,
        }
    first_party_control = (
        isinstance(defining_module, str)
        and defining_module.startswith(
            "ocr_platform.control.domains."
        )
    )
    if (
        export.get("kind") == "module"
        or (
            defining_module is not None
            and not first_party_control
            and defining_module
            not in {
                "ocr_platform.control.models",
                "ocr_platform.control.schemas",
            }
        )
    ):
        return {
            **export,
            "classification": "unsupported_leaked",
            "target": None,
            "wave": "PR 8",
            "reason": (
                "Imported standard-library, third-party, or unrelated "
                "first-party dependency leaked through wildcard export."
            ),
            "consumed": False,
        }
    if (
        defining_module == "ocr_platform.control.models"
        or owners
        and any(owner.endswith(".common") for owner in owners)
    ):
        return {
            **export,
            "classification": "internal_no_compat",
            "target": None,
            "wave": "PR 8",
            "reason": "Internal Control implementation detail; no compatibility target.",
            "consumed": False,
        }
    if any(
        owner.startswith("ocr_platform.control.domains.")
        and owner.endswith(".core")
        for owner in owners
    ):
        return {
            **export,
            "classification": "scheduling_application_pending",
            "target": "planned:owning domain application/command/query",
            "wave": "PR 5-7",
            "reason": "Public-looking core export needs explicit ownership before removal.",
            "consumed": False,
        }
    return {
        **export,
        "classification": "unsupported_leaked",
        "target": None,
        "wave": "PR 8",
        "reason": "Incidental wildcard dependency leak; not a Control Python API.",
        "consumed": False,
    }


def build_facade_inventory(
    root: Path = ROOT,
    *,
    runtime_exports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    import_raw: list[_RawSite] = []
    dynamic_raw: list[_RawSite] = []
    embedded_raw: list[_RawSite] = []
    use_raw: list[_RawSite] = []
    monkeypatch_raw: list[_RawSite] = []
    for path in _python_paths(root):
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        visitor = _FacadeConsumerVisitor(path, root)
        visitor.visit(tree)
        import_raw.extend(visitor.imports)
        dynamic_raw.extend(visitor.dynamic_imports)
        use_raw.extend(visitor.symbol_uses)
        monkeypatch_raw.extend(visitor.monkeypatches)
        embedded_imports, embedded_uses = _embedded_sites(path, root, tree)
        embedded_raw.extend(embedded_imports)
        use_raw.extend(embedded_uses)

    ast_import_sites = _finalize_sites(import_raw)
    dynamic_import_sites = _finalize_sites(dynamic_raw)
    embedded_import_sites = _finalize_sites(embedded_raw)
    symbol_use_sites = _finalize_sites(use_raw)
    monkeypatch_sites = _finalize_sites(monkeypatch_raw)
    exports = runtime_exports if runtime_exports is not None else _runtime_exports(root)
    surface_root = (
        root
        if (root / "ocr_platform" / "control" / "schemas.py").exists()
        else ROOT
    )
    surface_targets = _surface_targets(surface_root)
    classified_exports = [
        _classify_export(export, surface_targets=surface_targets)
        for export in exports
    ]
    consumed_symbols = sorted(
        {
            str(site["symbol"])
            for site in [*symbol_use_sites, *monkeypatch_sites]
        }
        | {
            str(symbol)
            for site in [*ast_import_sites, *embedded_import_sites]
            for symbol in site.get("symbols", [])
        }
    )
    consumer_paths = sorted(
        {
            site["source"].rsplit(":", 1)[0]
            for site in [
                *ast_import_sites,
                *dynamic_import_sites,
                *embedded_import_sites,
                *symbol_use_sites,
                *monkeypatch_sites,
            ]
        }
    )
    consumers = [
        {"path": path, "category": _source_category(path)}
        for path in consumer_paths
    ]
    return {
        "schema_version": 1,
        "builder": "tools.control_facade_inventory.build_facade_inventory",
        "target_module": TARGET_MODULE,
        "decreasing_gate": {
            "exports": "actual symbol keys must be a subset of baseline",
            "sites": (
                "actual (stable_id, AST-or-string fingerprint) pairs must "
                "be a subset of baseline"
            ),
            "line_numbers_are_evidence_only": True,
            "deletions_allowed": True,
            "new_or_replaced_allowed": False,
        },
        "exports": {
            "count": len(classified_exports),
            "classification_counts": dict(
                sorted(
                    Counter(
                        str(item["classification"])
                        for item in classified_exports
                    ).items()
                )
            ),
            "symbols": classified_exports,
        },
        "imports": {
            "ast_count": len(ast_import_sites),
            "ast_sites": ast_import_sites,
            "dynamic_count": len(dynamic_import_sites),
            "dynamic_sites": dynamic_import_sites,
            "embedded_count": len(embedded_import_sites),
            "embedded_sites": embedded_import_sites,
        },
        "consumers": {
            "file_count": len(consumers),
            "category_counts": dict(
                sorted(Counter(item["category"] for item in consumers).items())
            ),
            "files": consumers,
            "symbol_use_count": len(symbol_use_sites),
            "symbol_use_sites": symbol_use_sites,
            "unique_symbol_count": len(consumed_symbols),
            "unique_symbols": consumed_symbols,
        },
        "monkeypatches": {
            "count": len(monkeypatch_sites),
            "form_counts": dict(
                sorted(Counter(site["form"] for site in monkeypatch_sites).items())
            ),
            "sites": monkeypatch_sites,
        },
        "consumed_symbol_migrations": [
            {"symbol": symbol, **CONSUMED_MIGRATIONS[symbol]}
            for symbol in sorted(CONSUMED_MIGRATIONS)
        ],
    }


SITE_PATHS = [
    ("imports", "ast_sites"),
    ("imports", "dynamic_sites"),
    ("imports", "embedded_sites"),
    ("consumers", "symbol_use_sites"),
    ("monkeypatches", "sites"),
]


def validate_fixture_shape(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise ValueError("façade inventory schema_version must be 1")
    exports = payload["exports"]
    symbols = exports["symbols"]
    if exports["count"] != len(symbols):
        raise ValueError("façade export count is inconsistent")
    export_names = [item["symbol"] for item in symbols]
    if len(export_names) != len(set(export_names)):
        raise ValueError("façade export symbols are not unique")
    if any(item["classification"] not in CATEGORIES for item in symbols):
        raise ValueError("unknown façade export classification")
    if exports["classification_counts"] != dict(
        sorted(Counter(item["classification"] for item in symbols).items())
    ):
        raise ValueError("façade export classification counts are inconsistent")

    imports = payload["imports"]
    for count_key, sites_key in (
        ("ast_count", "ast_sites"),
        ("dynamic_count", "dynamic_sites"),
        ("embedded_count", "embedded_sites"),
    ):
        if imports[count_key] != len(imports[sites_key]):
            raise ValueError(f"{count_key} is inconsistent")
    consumers = payload["consumers"]
    if consumers["file_count"] != len(consumers["files"]):
        raise ValueError("façade consumer file count is inconsistent")
    if consumers["symbol_use_count"] != len(consumers["symbol_use_sites"]):
        raise ValueError("façade symbol use count is inconsistent")
    if consumers["unique_symbol_count"] != len(consumers["unique_symbols"]):
        raise ValueError("façade unique symbol count is inconsistent")
    if consumers["category_counts"] != dict(
        sorted(Counter(item["category"] for item in consumers["files"]).items())
    ):
        raise ValueError("façade consumer category counts are inconsistent")
    if any(item["category"] not in {"production", "test", "tool"} for item in consumers["files"]):
        raise ValueError("unknown façade consumer category")
    patches = payload["monkeypatches"]
    if patches["count"] != len(patches["sites"]):
        raise ValueError("façade monkeypatch count is inconsistent")
    if patches["form_counts"] != dict(
        sorted(Counter(site["form"] for site in patches["sites"]).items())
    ):
        raise ValueError("façade monkeypatch form counts are inconsistent")

    all_sites = [
        site
        for group, key in SITE_PATHS
        for site in payload[group][key]
    ]
    ids = [site["id"] for site in all_sites]
    if len(ids) != len(set(ids)):
        raise ValueError("façade inventory stable IDs are not unique")
    for site in all_sites:
        if not re.fullmatch(r"[0-9a-f]{64}", site["fingerprint"]):
            raise ValueError("façade site fingerprint is not SHA-256")
        if not re.fullmatch(r".+\.py:\d+", site["source"]):
            raise ValueError("façade source evidence is invalid")

    migrations = payload["consumed_symbol_migrations"]
    migration_symbols = {item["symbol"] for item in migrations}
    if not set(consumers["unique_symbols"]).issubset(migration_symbols):
        raise ValueError("consumed symbol migration map is incomplete")
    if migration_symbols != set(CONSUMED_MIGRATIONS):
        raise ValueError("consumed symbol migration authority drifted")


def _site_pairs(payload: dict[str, Any], group: str, key: str) -> set[tuple[str, str]]:
    return {
        (str(site["id"]), str(site["fingerprint"]))
        for site in payload[group][key]
    }


def validate_decreasing(
    actual: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    validate_fixture_shape(baseline)
    production = [
        item["path"]
        for item in actual["consumers"]["files"]
        if item["category"] == "production"
    ]
    if production:
        raise ValueError(
            "production façade consumers are forbidden: "
            + ", ".join(production)
        )
    validate_fixture_shape(actual)
    actual_exports = {
        item["symbol"] for item in actual["exports"]["symbols"]
    }
    baseline_exports = {
        item["symbol"] for item in baseline["exports"]["symbols"]
    }
    new_exports = sorted(actual_exports - baseline_exports)
    if new_exports:
        raise ValueError(
            "new façade exports are forbidden: " + ", ".join(new_exports)
        )
    for group, key in SITE_PATHS:
        new_sites = _site_pairs(actual, group, key) - _site_pairs(
            baseline,
            group,
            key,
        )
        if new_sites:
            raise ValueError(
                f"{group}.{key} contains new or replaced façade sites"
            )


def refresh() -> None:
    payload = build_facade_inventory()
    validate_fixture_shape(payload)
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(canonical_json(payload), encoding="utf-8")
    print(f"wrote {FIXTURE.relative_to(ROOT)}")


def check() -> None:
    if not FIXTURE.exists():
        raise SystemExit(f"missing façade inventory fixture: {FIXTURE}")
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = build_facade_inventory()
    validate_decreasing(actual, baseline)
    print("Control service façade inventory is within the checked-in baseline.")


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
