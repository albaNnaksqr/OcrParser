#!/usr/bin/env python3
"""Generate and enforce the decreasing v0.4 Control architecture-debt gate."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "control_architecture_debt.json"
)
TRANSACTION_METHODS = {"commit", "rollback", "flush"}
MUTATION_METHODS = {
    *TRANSACTION_METHODS,
    "add",
    "add_all",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update_mappings",
    "delete",
    "merge",
}
SESSION_DML_EXECUTION_METHODS = {"execute", "scalar", "scalars"}
SESSION_BULK_MUTATION_METHODS = {
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update_mappings",
}
STATUS_FIELDS = {"status", "worker_integrity_status"}


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


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _relative_source(path: Path, root: Path, line: int) -> str:
    return f"{path.relative_to(root)}:{line}"


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

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        self.function_stack.append("<lambda>")
        self.generic_visit(node)
        self.function_stack.pop()


@dataclass
class _RawSite:
    module: str
    function: str
    operation: str
    source: str
    fingerprint: str
    details: dict[str, Any]
    line: int
    column: int


def _finalize_sites(raw_sites: Iterable[_RawSite]) -> list[dict[str, Any]]:
    ordered = sorted(
        raw_sites,
        key=lambda item: (
            item.module,
            item.function,
            item.operation,
            item.line,
            item.column,
            item.fingerprint,
        ),
    )
    ordinals: Counter[tuple[str, str, str, str]] = Counter()
    sites: list[dict[str, Any]] = []
    for item in ordered:
        stable_operation = (
            f"{item.operation}@{item.fingerprint[:12]}"
        )
        key = (
            item.module,
            item.function,
            stable_operation,
            item.fingerprint,
        )
        ordinals[key] += 1
        sites.append(
            {
                "id": (
                    f"{item.module}:{item.function}:{stable_operation}:"
                    f"{ordinals[key]}"
                ),
                "fingerprint": item.fingerprint,
                "source": item.source,
                **item.details,
            }
        )
    return sites


def _resolve_import_from(
    *,
    current_module: str,
    is_package: bool,
    node: ast.ImportFrom,
) -> str:
    if node.level == 0:
        return node.module or ""
    package = current_module if is_package else current_module.rsplit(".", 1)[0]
    package_parts = package.split(".")
    keep = len(package_parts) - (node.level - 1)
    if keep < 0:
        return ""
    suffix = (node.module or "").split(".") if node.module else []
    return ".".join([*package_parts[:keep], *suffix])


def _domain_from_module(
    module: str,
    domain_names: set[str],
) -> str | None:
    prefix = "ocr_platform.control.domains."
    if not module.startswith(prefix):
        return None
    remainder = module[len(prefix) :]
    domain = remainder.split(".", 1)[0]
    return domain if domain in domain_names else None


def _is_query_path(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(
        root / "ocr_platform" / "control" / "domains"
    ).parts
    return (
        path.name in {"query.py", "queries.py"}
        or "queries" in relative_parts
    )


class _CrossDomainImportVisitor(_FunctionAwareVisitor):
    def __init__(
        self,
        path: Path,
        root: Path,
        module: str,
        domain_names: set[str],
    ) -> None:
        super().__init__()
        self.path = path
        self.root = root
        self.module = module
        self.domain_names = domain_names
        self.source_domain = _domain_from_module(module, domain_names)
        self.raw_sites: list[_RawSite] = []
        self.raw_statements: list[_RawSite] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        imported_from = _resolve_import_from(
            current_module=self.module,
            is_package=self.path.name == "__init__.py",
            node=node,
        )
        aliases: list[tuple[ast.alias, str, str]] = []
        for alias in node.names:
            target_module = (
                imported_from
                if imported_from.endswith(".core")
                else f"{imported_from}.core"
                if alias.name == "core"
                else ""
            )
            target_domain = _domain_from_module(
                target_module,
                self.domain_names,
            )
            if (
                target_module
                and self.source_domain is not None
                and target_domain is not None
                and self.source_domain != target_domain
                and target_module.endswith(".core")
            ):
                aliases.append((alias, target_module, target_domain))

        lazy = self.function != "<module>"
        for target_module, target_domain in sorted(
            {
                (target_module, target_domain)
                for _, target_module, target_domain in aliases
            }
        ):
            self.raw_statements.append(
                _RawSite(
                    module=self.module,
                    function=self.function,
                    operation=f"import-statement[{target_module}]",
                    source=_relative_source(
                        self.path,
                        self.root,
                        node.lineno,
                    ),
                    fingerprint=_fingerprint(
                        {
                            "kind": "cross_domain_core_import_statement",
                            "target_module": target_module,
                            "lazy_wrapper": lazy,
                        }
                    ),
                    details={
                        "source_domain": self.source_domain,
                        "target_domain": target_domain,
                        "target_module": target_module,
                        "lazy_wrapper": lazy,
                    },
                    line=node.lineno,
                    column=node.col_offset,
                )
            )

        for alias, target_module, target_domain in aliases:
            alias_suffix = f" as {alias.asname}" if alias.asname else ""
            operation = (
                f"import[{target_module}.{alias.name}{alias_suffix}]"
            )
            details = {
                "source_domain": self.source_domain,
                "target_domain": target_domain,
                "target_module": target_module,
                "symbol": alias.name,
                "alias": alias.asname,
                "private": alias.name.startswith("_"),
                "lazy_wrapper": lazy,
            }
            self.raw_sites.append(
                _RawSite(
                    module=self.module,
                    function=self.function,
                    operation=operation,
                    source=_relative_source(
                        self.path,
                        self.root,
                        node.lineno,
                    ),
                    fingerprint=_fingerprint(
                        {
                            "target_module": target_module,
                            "alias_ast": ast.dump(
                                alias,
                                annotate_fields=True,
                                include_attributes=False,
                            ),
                            "symbol": alias.name,
                            "alias": alias.asname,
                        }
                    ),
                    details=details,
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            target_module = alias.name
            target_domain = _domain_from_module(
                target_module,
                self.domain_names,
            )
            if not (
                self.source_domain is not None
                and target_domain is not None
                and self.source_domain != target_domain
                and target_module.endswith(".core")
            ):
                continue
            lazy = self.function != "<module>"
            statement_operation = f"import-statement[{target_module}]"
            statement_fingerprint = _fingerprint(
                {
                    "kind": "cross_domain_core_import_statement",
                    "target_module": target_module,
                    "lazy_wrapper": lazy,
                }
            )
            self.raw_statements.append(
                _RawSite(
                    module=self.module,
                    function=self.function,
                    operation=statement_operation,
                    source=_relative_source(
                        self.path,
                        self.root,
                        node.lineno,
                    ),
                    fingerprint=statement_fingerprint,
                    details={
                        "source_domain": self.source_domain,
                        "target_domain": target_domain,
                        "target_module": target_module,
                        "lazy_wrapper": lazy,
                    },
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
            alias_suffix = f" as {alias.asname}" if alias.asname else ""
            self.raw_sites.append(
                _RawSite(
                    module=self.module,
                    function=self.function,
                    operation=(
                        f"import[{target_module}{alias_suffix}]"
                    ),
                    source=_relative_source(
                        self.path,
                        self.root,
                        node.lineno,
                    ),
                    fingerprint=_fingerprint(
                        {
                            "target_module": target_module,
                            "alias_ast": ast.dump(
                                alias,
                                annotate_fields=True,
                                include_attributes=False,
                            ),
                            "symbol": target_module,
                            "alias": alias.asname,
                        }
                    ),
                    details={
                        "source_domain": self.source_domain,
                        "target_domain": target_domain,
                        "target_module": target_module,
                        "symbol": target_module,
                        "alias": alias.asname,
                        "private": False,
                        "lazy_wrapper": lazy,
                    },
                    line=node.lineno,
                    column=node.col_offset,
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        is_dynamic_import = (
            isinstance(node.func, ast.Name)
            and node.func.id in {"__import__", "import_module"}
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        )
        if (
            is_dynamic_import
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            target_module = node.args[0].value
            target_domain = _domain_from_module(
                target_module,
                self.domain_names,
            )
            if (
                self.source_domain is not None
                and target_domain is not None
                and self.source_domain != target_domain
                and target_module.endswith(".core")
            ):
                lazy = self.function != "<module>"
                common = {
                    "source_domain": self.source_domain,
                    "target_domain": target_domain,
                    "target_module": target_module,
                    "lazy_wrapper": lazy,
                }
                self.raw_statements.append(
                    _RawSite(
                        module=self.module,
                        function=self.function,
                        operation=(
                            f"dynamic-import-statement[{target_module}]"
                        ),
                        source=_relative_source(
                            self.path,
                            self.root,
                            node.lineno,
                        ),
                        fingerprint=_fingerprint(node),
                        details=common,
                        line=node.lineno,
                        column=node.col_offset,
                    )
                )
                self.raw_sites.append(
                    _RawSite(
                        module=self.module,
                        function=self.function,
                        operation=f"dynamic-import[{target_module}]",
                        source=_relative_source(
                            self.path,
                            self.root,
                            node.lineno,
                        ),
                        fingerprint=_fingerprint(node),
                        details={
                            **common,
                            "symbol": target_module,
                            "alias": None,
                            "private": False,
                        },
                        line=node.lineno,
                        column=node.col_offset,
                    )
                )
        self.generic_visit(node)


class _SessionAndPolicyVisitor(_FunctionAwareVisitor):
    def __init__(
        self,
        path: Path,
        root: Path,
        module: str,
        *,
        sql_dml_names: dict[str, str] | None = None,
        sqlalchemy_module_aliases: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.path = path
        self.root = root
        self.module = module
        self.transactions: list[_RawSite] = []
        self.policy_writes: list[_RawSite] = []
        self.direct_query_dml: list[_RawSite] = []
        self.query_dml_names: dict[str, set[str]] = defaultdict(set)
        self.session_name_stack: list[set[str]] = [{"session"}]
        self.has_mutation_sink = False
        self.sql_dml_names = {
            "delete": "delete",
            "insert": "insert",
            "text": "text",
            "update": "update",
            **(sql_dml_names or {}),
        }
        self.sqlalchemy_module_aliases = sqlalchemy_module_aliases or set()
        relative_parts = path.relative_to(
            root / "ocr_platform" / "control" / "domains"
        ).parts
        self.is_query = _is_query_path(path, root)
        self.is_owner_policy = (
            path.name == "policy.py"
            or "policies" in relative_parts
            or "scheduling" in relative_parts
        )

    def _site(
        self,
        *,
        node: ast.AST,
        operation: str,
        details: dict[str, Any],
    ) -> _RawSite:
        return _RawSite(
            module=self.module,
            function=self.function,
            operation=operation,
            source=_relative_source(
                self.path,
                self.root,
                int(getattr(node, "lineno")),
            ),
            fingerprint=_fingerprint(node),
            details=details,
            line=int(getattr(node, "lineno")),
            column=int(getattr(node, "col_offset")),
        )

    @staticmethod
    def _annotation_is_session(annotation: ast.expr | None) -> bool:
        if annotation is None:
            return False
        return any(
            (
                isinstance(item, ast.Name)
                and item.id == "Session"
            )
            or (
                isinstance(item, ast.Attribute)
                and item.attr == "Session"
            )
            for item in ast.walk(annotation)
        )

    @classmethod
    def _function_session_names(
        cls,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> set[str]:
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        return {
            argument.arg
            for argument in arguments
            if cls._annotation_is_session(argument.annotation)
        } | {"session"}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.session_name_stack.append(
            self.session_name_stack[-1]
            | self._function_session_names(node)
        )
        self.generic_visit(node)
        self.session_name_stack.pop()
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.session_name_stack.append(
            self.session_name_stack[-1]
            | self._function_session_names(node)
        )
        self.generic_visit(node)
        self.session_name_stack.pop()
        self.function_stack.pop()

    def _session_method(self, node: ast.Call) -> str | None:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.session_name_stack[-1]
        ):
            return node.func.attr
        return None

    def _contains_sql_dml(self, node: ast.AST) -> str | None:
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
            ):
                match = re.match(
                    r"\s*(?:/\*.*?\*/\s*)*(DELETE|INSERT|UPDATE)\b",
                    child.value,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                if match:
                    return f"raw_{match.group(1).lower()}"
            if not isinstance(child, ast.Call):
                continue
            if (
                isinstance(child.func, ast.Name)
                and child.func.id in self.sql_dml_names
                and self.sql_dml_names[child.func.id]
                in {"delete", "insert", "update"}
            ):
                return self.sql_dml_names[child.func.id]
            if (
                isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id in self.sqlalchemy_module_aliases
                and child.func.attr in {"delete", "insert", "update"}
            ):
                return child.func.attr
        return None

    def _session_query_bulk_method(self, node: ast.Call) -> str | None:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"delete", "update"}
        ):
            return None
        receiver = node.func.value
        for child in ast.walk(receiver):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "query"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id in self.session_name_stack[-1]
            ):
                return f"session.query.{node.func.attr}"
        return None

    def _is_sql_update_values(self, node: ast.Call) -> bool:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
        ):
            return False
        receiver = node.func.value
        return self._contains_sql_dml(receiver) == "update"

    @staticmethod
    def _mapping_status_entries(
        node: ast.Call,
    ) -> list[tuple[str, ast.AST | None, str]]:
        entries: list[tuple[str, ast.AST | None, str]] = []
        for keyword in node.keywords:
            if keyword.arg in STATUS_FIELDS:
                entries.append(
                    (str(keyword.arg), keyword.value, "keyword")
                )
        for argument in node.args:
            if isinstance(argument, ast.Dict):
                for key, value in zip(argument.keys, argument.values):
                    field = (
                        str(key.value)
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in STATUS_FIELDS
                        )
                        else key.attr
                        if (
                            isinstance(key, ast.Attribute)
                            and key.attr in STATUS_FIELDS
                        )
                        else None
                    )
                    if field is not None:
                        entries.append((field, value, "dict"))
            else:
                entries.append(("*", None, "dynamic_mapping"))
        return entries

    @staticmethod
    def _raw_sql_status_fields(node: ast.AST) -> set[str]:
        fields: set[str] = set()
        for child in ast.walk(node):
            if not (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
            ):
                continue
            upper = child.value.upper()
            for field in STATUS_FIELDS:
                if re.search(rf"\b{re.escape(field.upper())}\b", upper):
                    fields.add(field)
        return fields

    @staticmethod
    def _bulk_status_entries(
        node: ast.Call,
        method: str,
    ) -> list[tuple[str, ast.AST | None, str]]:
        payloads = (
            node.args[1:]
            if method in {
                "bulk_insert_mappings",
                "bulk_update_mappings",
            }
            else node.args
        )
        entries: list[tuple[str, ast.AST | None, str]] = []
        for payload in payloads:
            found = False
            for child in ast.walk(payload):
                if isinstance(child, ast.Dict):
                    for key, value in zip(child.keys, child.values):
                        field = (
                            str(key.value)
                            if (
                                isinstance(key, ast.Constant)
                                and key.value in STATUS_FIELDS
                            )
                            else key.attr
                            if (
                                isinstance(key, ast.Attribute)
                                and key.attr in STATUS_FIELDS
                            )
                            else None
                        )
                        if field is not None:
                            entries.append((field, value, "dict"))
                            found = True
                elif isinstance(child, ast.Call):
                    for keyword in child.keywords:
                        if keyword.arg in STATUS_FIELDS:
                            entries.append(
                                (
                                    str(keyword.arg),
                                    keyword.value,
                                    "constructor",
                                )
                            )
                            found = True
            if not found and isinstance(
                payload,
                (ast.Name, ast.Attribute, ast.Call),
            ):
                entries.append(("*", None, "dynamic_payload"))
        return entries

    def visit_Call(self, node: ast.Call) -> Any:
        method = self._session_method(node)
        query_bulk_method = self._session_query_bulk_method(node)
        execute_sql_operation: str | None = None
        if method in SESSION_DML_EXECUTION_METHODS and node.args:
            execute_sql_operation = self._contains_sql_dml(node.args[0])
            if (
                execute_sql_operation is None
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id
                in self.query_dml_names[self.function]
            ):
                execute_sql_operation = "tainted_statement"
        if (
            method in MUTATION_METHODS
            or query_bulk_method is not None
            or execute_sql_operation is not None
        ):
            self.has_mutation_sink = True
        if method in TRANSACTION_METHODS:
            self.transactions.append(
                self._site(
                    node=node,
                    operation=f"session.{method}",
                    details={"operation": method},
                )
            )
        if self.is_query:
            dml_operation: str | None = None
            if method in MUTATION_METHODS:
                dml_operation = f"session.{method}"
            elif query_bulk_method is not None:
                dml_operation = query_bulk_method
            elif execute_sql_operation is not None:
                dml_operation = (
                    f"session.execute.{execute_sql_operation}"
                )
            if dml_operation is not None:
                self.direct_query_dml.append(
                    self._site(
                        node=node,
                        operation=(
                            f"query-dml[{dml_operation}@"
                            f"{_fingerprint(node)[:12]}]"
                        ),
                        details={"operation": dml_operation},
                    )
                )
        if (
            not self.is_owner_policy
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 3
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in STATUS_FIELDS
        ):
            field = str(node.args[1].value)
            self.has_mutation_sink = True
            if self.is_query:
                self.direct_query_dml.append(
                    self._site(
                        node=node,
                        operation=(
                            f"query-dml[setattr.{field}@"
                            f"{_fingerprint(node)[:12]}]"
                        ),
                        details={"operation": f"setattr.{field}"},
                    )
                )
            self.policy_writes.append(
                self._site(
                    node=node,
                    operation=(
                        f"setattr-write[{field}<-"
                        f"{_fingerprint(node.args[2])[:12]}]"
                    ),
                    details={
                        "field": field,
                        "target": ast.unparse(node.args[0]),
                        "write_kind": "setattr",
                    },
                )
            )
        status_mapping_kind: str | None = None
        if self._is_sql_update_values(node):
            status_mapping_kind = "sql_values"
        elif query_bulk_method == "session.query.update":
            status_mapping_kind = "query_update"
        if not self.is_owner_policy and status_mapping_kind is not None:
            for field, value, form in self._mapping_status_entries(node):
                self.has_mutation_sink = True
                value_fingerprint = (
                    _fingerprint(value)[:12]
                    if value is not None
                    else "dynamic"
                )
                self.policy_writes.append(
                    self._site(
                        node=node,
                        operation=(
                            f"{status_mapping_kind}[{field}<-"
                            f"{value_fingerprint}]"
                        ),
                        details={
                            "field": field,
                            "write_kind": (
                                status_mapping_kind
                                if form == "keyword"
                                else f"{status_mapping_kind}_{form}"
                            ),
                        },
                    )
                )
        if (
            not self.is_owner_policy
            and method in SESSION_DML_EXECUTION_METHODS
            and node.args
            and execute_sql_operation is not None
            and execute_sql_operation.startswith("raw_")
        ):
            for field in sorted(
                self._raw_sql_status_fields(node.args[0])
            ):
                self.policy_writes.append(
                    self._site(
                        node=node,
                        operation=(
                            f"raw-sql-status[{field}<-"
                            f"{_fingerprint(node.args[0])[:12]}]"
                        ),
                        details={
                            "field": field,
                            "write_kind": "raw_sql",
                        },
                    )
                )
        if (
            not self.is_owner_policy
            and method in SESSION_BULK_MUTATION_METHODS
        ):
            for field, value, form in self._bulk_status_entries(
                node,
                method,
            ):
                self.has_mutation_sink = True
                value_fingerprint = (
                    _fingerprint(value)[:12]
                    if value is not None
                    else "dynamic"
                )
                self.policy_writes.append(
                    self._site(
                        node=node,
                        operation=(
                            f"session-{method}[{field}<-"
                            f"{value_fingerprint}]"
                        ),
                        details={
                            "field": field,
                            "write_kind": f"session_{method}_{form}",
                        },
                    )
                )
        self.generic_visit(node)

    def _record_assignment(
        self,
        *,
        node: ast.AST,
        targets: Iterable[ast.expr],
    ) -> None:
        value = getattr(node, "value", None)
        value_fingerprint = (
            _fingerprint(value)[:12]
            if isinstance(value, ast.AST)
            else "unknown"
        )
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Name)
                and value.id in self.session_name_stack[-1]
            ):
                self.session_name_stack[-1].add(target.id)
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.AST)
                and self._contains_sql_dml(value) is not None
            ):
                self.query_dml_names[self.function].add(target.id)
            if self.is_query and isinstance(target, ast.Attribute):
                self.has_mutation_sink = True
                self.direct_query_dml.append(
                    self._site(
                        node=node,
                        operation=(
                            f"query-dml[attribute-write."
                            f"{target.attr}@{_fingerprint(node)[:12]}]"
                        ),
                        details={
                            "operation": (
                                f"attribute-write.{target.attr}"
                            )
                        },
                    )
                )
            if (
                isinstance(target, ast.Attribute)
                and target.attr in STATUS_FIELDS
            ):
                self.has_mutation_sink = True
                if self.is_owner_policy:
                    continue
                self.policy_writes.append(
                    self._site(
                        node=node,
                        operation=(
                            f"attribute-write[{ast.unparse(target)}<-"
                            f"{value_fingerprint}]"
                        ),
                        details={
                            "field": target.attr,
                            "target": ast.unparse(target),
                            "write_kind": "attribute_assignment",
                        },
                    )
                )

    def visit_Assign(self, node: ast.Assign) -> Any:
        self._record_assignment(node=node, targets=node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        self._record_assignment(node=node, targets=[node.target])
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> Any:
        self._record_assignment(node=node, targets=[node.target])
        self.generic_visit(node)


def _domain_python_files(root: Path) -> list[Path]:
    domain_root = root / "ocr_platform" / "control" / "domains"
    return sorted(domain_root.rglob("*.py"))


def _domain_names(root: Path) -> set[str]:
    domain_root = root / "ocr_platform" / "control" / "domains"
    return {
        path.name
        for path in domain_root.iterdir()
        if path.is_dir() and any(path.rglob("*.py"))
    }


def _strongly_connected_components(
    edges: Iterable[tuple[str, str]],
) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for source, target in edges:
        adjacency[source].add(target)
        nodes.update({source, target})
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency[node]):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            current = stack.pop()
            on_stack.remove(current)
            component.append(current)
            if current == node:
                break
        if len(component) > 1:
            components.append(sorted(component))

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return sorted(components)


def _function_definitions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _sqlalchemy_aliases(
    tree: ast.Module,
) -> tuple[dict[str, str], set[str]]:
    names: dict[str, str] = {}
    modules: set[str] = set()
    # Walk all scopes deliberately. The gate is conservative and must not let a
    # function-local alias hide a DML constructor from the shared sink detector.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            node.module == "sqlalchemy"
            or (node.module or "").startswith("sqlalchemy.")
        ):
            for alias in node.names:
                if alias.name in {"delete", "insert", "text", "update"}:
                    names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy":
                    modules.add(alias.asname or "sqlalchemy")
    return names, modules


def _function_is_directly_mutating(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    path: Path,
    root: Path,
    module: str,
    sql_dml_names: dict[str, str],
    sqlalchemy_module_aliases: set[str],
) -> bool:
    visitor = _SessionAndPolicyVisitor(
        path,
        root,
        module,
        sql_dml_names=sql_dml_names,
        sqlalchemy_module_aliases=sqlalchemy_module_aliases,
    )
    visitor.visit(node)
    return visitor.has_mutation_sink


def _same_domain_runtime_modules(
    domain_dir: Path,
    root: Path,
) -> dict[str, tuple[Path, ast.Module]]:
    modules: dict[str, tuple[Path, ast.Module]] = {}
    for path in sorted(domain_dir.rglob("*.py")):
        if _is_query_path(path, root):
            continue
        modules[_module_name(path, root)] = (
            path,
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            ),
        )
    return modules


def _import_bindings(
    nodes: Iterable[ast.AST],
    *,
    current_module: str,
    is_package: bool,
    known_modules: set[str],
    known_symbols: set[str],
    recursive: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    direct: dict[str, str] = {}
    modules: dict[str, str] = {}
    candidates: Iterable[ast.AST]
    if recursive:
        candidates = (
            child
            for node in nodes
            for child in ast.walk(node)
        )
    else:
        candidates = nodes
    for node in candidates:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known_modules:
                    modules[alias.asname or alias.name.split(".")[-1]] = (
                        alias.name
                    )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        imported_from = _resolve_import_from(
            current_module=current_module,
            is_package=is_package,
            node=node,
        )
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            imported_name = (
                f"{imported_from}.{alias.name}"
                if imported_from
                else alias.name
            )
            if imported_name in known_modules:
                modules[local_name] = imported_name
            if imported_name in known_symbols:
                direct[local_name] = imported_name
    return direct, modules


def _runtime_mutating_symbols(
    runtime_modules: dict[str, tuple[Path, ast.Module]],
    *,
    root: Path,
    symbol_aliases: dict[str, str],
) -> set[str]:
    definitions: dict[
        str,
        tuple[
            ast.FunctionDef | ast.AsyncFunctionDef,
            Path,
            str,
            dict[str, str],
            set[str],
        ],
    ] = {}
    for module, (path, tree) in runtime_modules.items():
        sql_names, sql_modules = _sqlalchemy_aliases(tree)
        for name, node in _function_definitions(tree).items():
            definitions[f"{module}.{name}"] = (
                node,
                path,
                module,
                sql_names,
                sql_modules,
            )
    known_modules = set(runtime_modules)
    known_symbols = set(definitions) | set(symbol_aliases)
    mutating = {
        symbol
        for symbol, (
            node,
            path,
            module,
            sql_names,
            sql_modules,
        ) in definitions.items()
        if _function_is_directly_mutating(
            node,
            path=path,
            root=root,
            module=module,
            sql_dml_names=sql_names,
            sqlalchemy_module_aliases=sql_modules,
        )
    }

    calls: dict[str, set[str]] = {}
    for symbol, (node, path, module, _, _) in definitions.items():
        tree = runtime_modules[module][1]
        global_direct, global_modules = _import_bindings(
            tree.body,
            current_module=module,
            is_package=path.name == "__init__.py",
            known_modules=known_modules,
            known_symbols=known_symbols,
            recursive=False,
        )
        local_direct, local_modules = _import_bindings(
            [node],
            current_module=module,
            is_package=False,
            known_modules=known_modules,
            known_symbols=known_symbols,
            recursive=True,
        )
        direct = {**global_direct, **local_direct}
        module_aliases = {**global_modules, **local_modules}
        callees: set[str] = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                candidate = direct.get(
                    child.func.id,
                    f"{module}.{child.func.id}",
                )
                if candidate in known_symbols:
                    callees.add(candidate)
            elif (
                isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id in module_aliases
            ):
                candidate = (
                    f"{module_aliases[child.func.value.id]}."
                    f"{child.func.attr}"
                )
                if candidate in known_symbols:
                    callees.add(candidate)
        calls[symbol] = callees

    while True:
        expanded = mutating | {
            symbol
            for symbol, callees in calls.items()
            if callees & mutating
        } | {
            alias
            for alias, target in symbol_aliases.items()
            if target in mutating
        }
        if expanded == mutating:
            return mutating
        mutating = expanded


def _runtime_symbol_aliases(
    runtime_modules: dict[str, tuple[Path, ast.Module]],
) -> dict[str, str]:
    definitions = {
        f"{module}.{name}"
        for module, (_, tree) in runtime_modules.items()
        for name in _function_definitions(tree)
    }
    aliases: dict[str, str] = {}
    while True:
        discovered = dict(aliases)
        resolvable = definitions | set(aliases)
        for module, (path, tree) in runtime_modules.items():
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom):
                    continue
                imported_from = _resolve_import_from(
                    current_module=module,
                    is_package=path.name == "__init__.py",
                    node=node,
                )
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported_name = (
                        f"{imported_from}.{alias.name}"
                        if imported_from
                        else alias.name
                    )
                    if imported_name not in resolvable:
                        continue
                    discovered[f"{module}.{alias.asname or alias.name}"] = (
                        aliases.get(imported_name, imported_name)
                    )
        if discovered == aliases:
            return aliases
        aliases = discovered


class _SemanticQueryVisitor(_FunctionAwareVisitor):
    def __init__(
        self,
        *,
        path: Path,
        root: Path,
        module: str,
        known_modules: set[str],
        known_symbols: set[str],
        mutating_symbols: set[str],
    ) -> None:
        super().__init__()
        self.path = path
        self.root = root
        self.module = module
        self.known_modules = known_modules
        self.known_symbols = known_symbols
        self.mutating_symbols = mutating_symbols
        self.direct_stack: list[dict[str, str]] = [{}]
        self.module_stack: list[dict[str, str]] = [{}]
        self.raw_sites: list[_RawSite] = []

    def _append_site(
        self,
        *,
        node: ast.AST,
        symbol: str,
        target_module: str,
        evidence: str,
    ) -> None:
        operation = f"semantic-query-mutation[{symbol}]"
        self.raw_sites.append(
            _RawSite(
                module=self.module,
                function=self.function,
                operation=operation,
                source=_relative_source(
                    self.path,
                    self.root,
                    int(getattr(node, "lineno")),
                ),
                fingerprint=_fingerprint(
                    {
                        "node_ast": ast.dump(
                            node,
                            annotate_fields=True,
                            include_attributes=False,
                        ),
                        "target_module": target_module,
                        "symbol": symbol,
                        "classification": "semantic_mutation",
                        "evidence": evidence,
                    }
                ),
                details={
                    "symbol": symbol,
                    # Retain the v0.4 PR1 fixture key for compatibility. It
                    # denotes the target runtime module, not only core.py.
                    "core_module": target_module,
                    "classification": "transitive AST mutation analysis",
                },
                line=int(getattr(node, "lineno")),
                column=int(getattr(node, "col_offset")),
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.direct_stack.append(dict(self.direct_stack[-1]))
        self.module_stack.append(dict(self.module_stack[-1]))
        for statement in node.body:
            self.visit(statement)
        self.module_stack.pop()
        self.direct_stack.pop()
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self.function_stack.append(node.name)
        self.direct_stack.append(dict(self.direct_stack[-1]))
        self.module_stack.append(dict(self.module_stack[-1]))
        for statement in node.body:
            self.visit(statement)
        self.module_stack.pop()
        self.direct_stack.pop()
        self.function_stack.pop()

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            if alias.name in self.known_modules:
                self.module_stack[-1][
                    alias.asname or alias.name.split(".")[-1]
                ] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        imported_from = _resolve_import_from(
            current_module=self.module,
            is_package=self.path.name == "__init__.py",
            node=node,
        )
        for alias in node.names:
            if alias.name == "*":
                continue
            local_name = alias.asname or alias.name
            imported_name = (
                f"{imported_from}.{alias.name}"
                if imported_from
                else alias.name
            )
            if imported_name in self.known_modules:
                self.module_stack[-1][local_name] = imported_name
            if imported_name not in self.known_symbols:
                continue
            self.direct_stack[-1][local_name] = imported_name
            if imported_name in self.mutating_symbols:
                self._append_site(
                    node=node,
                    symbol=alias.name,
                    target_module=imported_name.rsplit(".", 1)[0],
                    evidence="direct_import",
                )

    def visit_Call(self, node: ast.Call) -> Any:
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.module_stack[-1]
        ):
            target_module = self.module_stack[-1][node.func.value.id]
            target_symbol = f"{target_module}.{node.func.attr}"
            if target_symbol in self.mutating_symbols:
                self._append_site(
                    node=node,
                    symbol=node.func.attr,
                    target_module=target_module,
                    evidence="module_call",
                )
        self.generic_visit(node)


def _semantic_query_mutations(root: Path) -> list[dict[str, Any]]:
    raw_sites: list[_RawSite] = []
    domain_root = root / "ocr_platform" / "control" / "domains"
    query_paths = [
        path
        for path in sorted(domain_root.rglob("*.py"))
        if _is_query_path(path, root)
    ]
    for query_path in query_paths:
        relative_parts = query_path.relative_to(domain_root).parts
        domain_dir = domain_root / relative_parts[0]
        runtime_modules = _same_domain_runtime_modules(
            domain_dir,
            root,
        )
        if not runtime_modules:
            continue
        query_tree = ast.parse(
            query_path.read_text(encoding="utf-8"),
            filename=str(query_path),
        )
        module = _module_name(query_path, root)
        symbol_aliases = _runtime_symbol_aliases(runtime_modules)
        known_modules = set(runtime_modules)
        known_symbols = {
            f"{runtime_module}.{name}"
            for runtime_module, (_, tree) in runtime_modules.items()
            for name in _function_definitions(tree)
        } | set(symbol_aliases)
        visitor = _SemanticQueryVisitor(
            path=query_path,
            root=root,
            module=module,
            known_modules=known_modules,
            known_symbols=known_symbols,
            mutating_symbols=_runtime_mutating_symbols(
                runtime_modules,
                root=root,
                symbol_aliases=symbol_aliases,
            ),
        )
        visitor.visit(query_tree)
        raw_sites.extend(visitor.raw_sites)
    return _finalize_sites(raw_sites)


def build_architecture_debt(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    domain_names = _domain_names(root)
    import_raw: list[_RawSite] = []
    import_statement_raw: list[_RawSite] = []
    transaction_raw: list[_RawSite] = []
    direct_query_raw: list[_RawSite] = []
    policy_raw: list[_RawSite] = []

    for path in _domain_python_files(root):
        module = _module_name(path, root)
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        sql_dml_names, sqlalchemy_module_aliases = _sqlalchemy_aliases(tree)
        import_visitor = _CrossDomainImportVisitor(
            path,
            root,
            module,
            domain_names,
        )
        import_visitor.visit(tree)
        import_raw.extend(import_visitor.raw_sites)
        import_statement_raw.extend(import_visitor.raw_statements)

        debt_visitor = _SessionAndPolicyVisitor(
            path,
            root,
            module,
            sql_dml_names=sql_dml_names,
            sqlalchemy_module_aliases=sqlalchemy_module_aliases,
        )
        debt_visitor.visit(tree)
        transaction_raw.extend(debt_visitor.transactions)
        direct_query_raw.extend(debt_visitor.direct_query_dml)
        policy_raw.extend(debt_visitor.policy_writes)

    import_sites = _finalize_sites(import_raw)
    import_statement_sites = _finalize_sites(import_statement_raw)
    transaction_sites = _finalize_sites(transaction_raw)
    direct_query_sites = _finalize_sites(direct_query_raw)
    policy_sites = _finalize_sites(policy_raw)
    semantic_query_sites = _semantic_query_mutations(root)

    edge_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"statement_count": 0, "symbol_count": 0}
    )
    for site in import_sites:
        edge = (site["source_domain"], site["target_domain"])
        edge_counts[edge]["symbol_count"] += 1
    for statement in import_statement_sites:
        edge = (statement["source_domain"], statement["target_domain"])
        edge_counts[edge]["statement_count"] += 1
    edges = [
        {
            "source": source,
            "target": target,
            **counts,
        }
        for (source, target), counts in sorted(edge_counts.items())
    ]

    transaction_counts = Counter(
        site["operation"] for site in transaction_sites
    )
    payload = {
        "schema_version": 1,
        "builder": "tools.control_architecture_debt.build_architecture_debt",
        "decreasing_gate": {
            "rule": (
                "actual (stable_id, normalized_ast_fingerprint) pairs must "
                "be a subset of the reviewed baseline"
            ),
            "line_numbers_are_evidence_only": True,
            "deletions_allowed": True,
            "new_or_replaced_sites_allowed": False,
        },
        "false_positive_rules": {
            "cross_domain_imports": (
                "only imports of another named business domain's core "
                "module are counted; common.py, schemas, and imports outside "
                "business domains are excluded; function-local imports are "
                "lazy"
            ),
            "transactions": (
                "commit/rollback/flush are counted only on the conventional "
                "session name, a Session-annotated parameter, or an "
                "assignment/closure alias of one"
            ),
            "query_dml": (
                "direct query DML includes session mutation and bulk APIs; "
                "session.execute/scalar/scalars of insert/update/delete, "
                "including local or raw-SQL statement variables; "
                "session.query bulk mutation; and any ORM attribute write. "
                "query.py, queries.py, and queries/ are covered. Semantic "
                "mutations reuse the same sinks through function-aware call "
                "graphs across every same-domain runtime module"
            ),
            "policy_writes": (
                "attribute/setattr writes, SQL update values (keyword, dict, "
                "or conservative dynamic mapping), session bulk mappings, "
                "session.query update mappings, and raw SQL for status fields "
                "are counted outside policy.py, policies/, and scheduling/; "
                "ORM constructor initial values outside bulk APIs and "
                "unrelated .values calls are ignored"
            ),
        },
        "cross_domain_imports": {
            "statement_count": len(import_statement_sites),
            "symbol_count": len(import_sites),
            "private_symbol_count": sum(
                bool(site["private"]) for site in import_sites
            ),
            "lazy_wrapper_count": sum(
                bool(site["lazy_wrapper"]) for site in import_sites
            ),
            "statements": import_statement_sites,
            "sites": import_sites,
            "edges": edges,
            "strongly_connected_components": (
                _strongly_connected_components(
                    (item["source"], item["target"]) for item in edges
                )
            ),
        },
        "transactions": {
            "total": len(transaction_sites),
            "operations": {
                method: transaction_counts[method]
                for method in sorted(TRANSACTION_METHODS)
            },
            "sites": transaction_sites,
        },
        "query_mutations": {
            "direct_dml_count": len(direct_query_sites),
            "direct_dml_sites": direct_query_sites,
            "semantic_allowlist_count": len(semantic_query_sites),
            "semantic_allowlist_sites": semantic_query_sites,
        },
        "policy_external_status_writes": {
            "count": len(policy_sites),
            "sites": policy_sites,
        },
    }
    validate_fixture_shape(payload)
    return payload


def _site_pairs(section: dict[str, Any], key: str = "sites") -> set[tuple[str, str]]:
    return {
        (str(site["id"]), str(site["fingerprint"]))
        for site in section.get(key, [])
    }


def validate_fixture_shape(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != 1:
        raise ValueError("architecture debt schema_version must be 1")
    imports = payload["cross_domain_imports"]
    if imports["statement_count"] != len(imports["statements"]):
        raise ValueError("cross-domain statement count is inconsistent")
    if imports["symbol_count"] != len(imports["sites"]):
        raise ValueError("cross-domain symbol count is inconsistent")
    if imports["private_symbol_count"] != sum(
        bool(site["private"]) for site in imports["sites"]
    ):
        raise ValueError("cross-domain private count is inconsistent")
    if imports["lazy_wrapper_count"] != sum(
        bool(site["lazy_wrapper"]) for site in imports["sites"]
    ):
        raise ValueError("cross-domain lazy count is inconsistent")
    expected_edges: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"statement_count": 0, "symbol_count": 0}
    )
    for statement in imports["statements"]:
        expected_edges[
            (statement["source_domain"], statement["target_domain"])
        ]["statement_count"] += 1
    for site in imports["sites"]:
        expected_edges[
            (site["source_domain"], site["target_domain"])
        ]["symbol_count"] += 1
    rendered_edges = [
        {
            "source": source,
            "target": target,
            **counts,
        }
        for (source, target), counts in sorted(expected_edges.items())
    ]
    if imports["edges"] != rendered_edges:
        raise ValueError("cross-domain edge metadata is inconsistent")
    expected_sccs = _strongly_connected_components(
        (item["source"], item["target"]) for item in rendered_edges
    )
    if imports["strongly_connected_components"] != expected_sccs:
        raise ValueError("cross-domain SCC metadata is inconsistent")
    transactions = payload["transactions"]
    if transactions["total"] != len(transactions["sites"]):
        raise ValueError("transaction total is inconsistent")
    if sum(transactions["operations"].values()) != transactions["total"]:
        raise ValueError("transaction operation counts are inconsistent")
    query = payload["query_mutations"]
    if query["direct_dml_count"] != len(query["direct_dml_sites"]):
        raise ValueError("direct query DML count is inconsistent")
    if query["semantic_allowlist_count"] != len(
        query["semantic_allowlist_sites"]
    ):
        raise ValueError("semantic query mutation count is inconsistent")
    policy = payload["policy_external_status_writes"]
    if policy["count"] != len(policy["sites"]):
        raise ValueError("policy-external write count is inconsistent")
    all_ids = [
        site["id"]
        for section, key in (
            (imports, "statements"),
            (imports, "sites"),
            (transactions, "sites"),
            (query, "direct_dml_sites"),
            (query, "semantic_allowlist_sites"),
            (policy, "sites"),
        )
        for site in section[key]
    ]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("architecture debt stable IDs are not unique")


def validate_decreasing(
    actual: dict[str, Any],
    baseline: dict[str, Any],
) -> None:
    """Allow debt deletion while rejecting every new or replaced AST site."""

    validate_fixture_shape(actual)
    validate_fixture_shape(baseline)
    categories = (
        ("cross_domain_imports", "statements"),
        ("cross_domain_imports", "sites"),
        ("transactions", "sites"),
        ("query_mutations", "direct_dml_sites"),
        ("query_mutations", "semantic_allowlist_sites"),
        ("policy_external_status_writes", "sites"),
    )
    for section_name, key in categories:
        actual_pairs = _site_pairs(actual[section_name], key)
        baseline_pairs = _site_pairs(baseline[section_name], key)
        unexpected = sorted(actual_pairs - baseline_pairs)
        if unexpected:
            raise ValueError(
                f"{section_name}.{key} contains new or replaced sites: "
                f"{unexpected}"
            )

    actual_edges = {
        (item["source"], item["target"]): item
        for item in actual["cross_domain_imports"]["edges"]
    }
    baseline_edges = {
        (item["source"], item["target"]): item
        for item in baseline["cross_domain_imports"]["edges"]
    }
    for edge, counts in actual_edges.items():
        if edge not in baseline_edges:
            raise ValueError(f"new cross-domain edge: {edge}")
        if any(
            counts[field] > baseline_edges[edge][field]
            for field in ("statement_count", "symbol_count")
        ):
            raise ValueError(f"cross-domain edge debt increased: {edge}")
    validate_scc_decreasing(
        actual["cross_domain_imports"]["strongly_connected_components"],
        baseline["cross_domain_imports"]["strongly_connected_components"],
    )


def validate_scc_decreasing(
    actual_components: list[list[str]],
    baseline_components: list[list[str]],
) -> None:
    """Allow an existing cycle to split or shrink, but never to expand."""

    actual_sccs = [set(component) for component in actual_components]
    baseline_sccs = [set(component) for component in baseline_components]
    unexpected_sccs = [
        sorted(component)
        for component in actual_sccs
        if not any(
            component <= baseline_component
            for baseline_component in baseline_sccs
        )
    ]
    if unexpected_sccs:
        raise ValueError(
            f"new cross-domain SCCs: {unexpected_sccs}"
        )


def refresh() -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        canonical_json(build_architecture_debt()),
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE.relative_to(ROOT)}")


def check() -> None:
    if not FIXTURE.is_file():
        raise SystemExit(f"missing architecture debt fixture: {FIXTURE}")
    baseline = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = build_architecture_debt()
    validate_decreasing(actual, baseline)
    print("Control architecture debt is within the checked-in baseline.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "refresh"))
    args = parser.parse_args(argv)
    if args.command == "refresh":
        refresh()
    else:
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
