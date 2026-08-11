#!/usr/bin/env python3
"""Validate an OcrParser production inventory against the deployment contract.

The validator is fail-closed: anything it cannot parse, cannot understand, or
cannot prove correct is reported as a finding and the command exits non-zero.
It uses only the Python standard library plus PyYAML, which is already a runtime
dependency of ``ocrparser-platform``.

Secret values never enter this program's output. The contract forbids literal
secret fields entirely; only ``*_secret_var`` / ``*_env_var`` / ``*_secret_ref``
references naming an environment variable are accepted, and findings report the
variable path only, never a value.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

import yaml


CONTROL_GROUP = "ocr_control"
WORKER_GROUP = "ocr_workers"

# Stable, documented failure codes. Callers may branch on these; the strings are
# part of the bundle contract and must not be renamed.
INVENTORY_UNREADABLE = "inventory_unreadable"
YAML_PARSE_ERROR = "yaml_parse_error"
INVENTORY_NOT_MAPPING = "inventory_not_mapping"
MISSING_ALL_GROUP = "missing_all_group"
MISSING_REQUIRED_VAR = "missing_required_var"
PLACEHOLDER_VALUE = "placeholder_value"
RELEASE_TAG_INVALID = "release_tag_invalid"
RELEASE_COMMIT_INVALID = "release_commit_invalid"
RELEASE_IDENTITY_PARTIAL = "release_identity_partial"
RELEASE_MANIFEST_URL_INVALID = "release_manifest_url_invalid"
SOURCE_REPO_URL_INVALID = "source_repo_url_invalid"
WHEEL_URL_INVALID = "wheel_url_invalid"
WHEEL_SHA256_INVALID = "wheel_sha256_invalid"
LITERAL_SECRET_FIELD = "literal_secret_field"
SECRET_REFERENCE_INVALID = "secret_reference_invalid"
CONTROL_GROUP_MISSING = "control_group_missing"
CONTROL_GROUP_NOT_SINGLE_HOST = "control_group_not_single_host"
CONTROL_VAR_MISSING = "control_var_missing"
CONTROL_PORT_INVALID = "control_port_invalid"
CONTROL_URL_INVALID = "control_url_invalid"
CONTROL_TOKEN_SECRET_REQUIRED = "control_token_secret_required"
WORKER_GROUP_MISSING = "worker_group_missing"
WORKER_GROUP_EMPTY = "worker_group_empty"
WORKER_VAR_MISSING = "worker_var_missing"
WORKER_SERVER_ID_DUPLICATE = "worker_server_id_duplicate"
WORKER_SERVER_ID_INVALID = "worker_server_id_invalid"
PATH_NOT_ABSOLUTE = "path_not_absolute"
PATH_TRAVERSAL = "path_traversal"
PATH_ESCAPES_SHARED_ROOT = "path_escapes_shared_root"
POSTGRES_PORT_INVALID = "postgres_port_invalid"
ROLLOUT_SERIAL_INVALID = "rollout_serial_invalid"
ROLLOUT_FAILURE_PERCENTAGE_INVALID = "rollout_failure_percentage_invalid"
CANARY_FLAG_INVALID = "canary_flag_invalid"
CANARY_CONTRACT_INCOMPLETE = "canary_contract_incomplete"

CODES: tuple[str, ...] = (
    CANARY_CONTRACT_INCOMPLETE,
    CANARY_FLAG_INVALID,
    CONTROL_GROUP_MISSING,
    CONTROL_GROUP_NOT_SINGLE_HOST,
    CONTROL_PORT_INVALID,
    CONTROL_TOKEN_SECRET_REQUIRED,
    CONTROL_URL_INVALID,
    CONTROL_VAR_MISSING,
    INVENTORY_NOT_MAPPING,
    INVENTORY_UNREADABLE,
    LITERAL_SECRET_FIELD,
    MISSING_ALL_GROUP,
    MISSING_REQUIRED_VAR,
    PATH_ESCAPES_SHARED_ROOT,
    PATH_NOT_ABSOLUTE,
    PATH_TRAVERSAL,
    PLACEHOLDER_VALUE,
    POSTGRES_PORT_INVALID,
    RELEASE_COMMIT_INVALID,
    RELEASE_IDENTITY_PARTIAL,
    RELEASE_MANIFEST_URL_INVALID,
    RELEASE_TAG_INVALID,
    ROLLOUT_FAILURE_PERCENTAGE_INVALID,
    ROLLOUT_SERIAL_INVALID,
    SECRET_REFERENCE_INVALID,
    SOURCE_REPO_URL_INVALID,
    WHEEL_SHA256_INVALID,
    WHEEL_URL_INVALID,
    WORKER_GROUP_EMPTY,
    WORKER_GROUP_MISSING,
    WORKER_SERVER_ID_DUPLICATE,
    WORKER_SERVER_ID_INVALID,
    WORKER_VAR_MISSING,
    YAML_PARSE_ERROR,
)

REQUIRED_ALL_VARS: tuple[str, ...] = (
    "ocr_release_tag",
    "ocr_shared_root",
    "ocr_postgres_host",
    "ocr_database_url_secret_var",
    "ocr_api_token_secret_var",
)

REQUIRED_CONTROL_VARS: tuple[str, ...] = (
    "ocr_control_url",
)

REQUIRED_WORKER_VARS: tuple[str, ...] = ()

DEFAULT_ALL_VARS: dict[str, Any] = {
    "ocr_service_user": "ocr-platform",
    "ocr_service_group": "ocr-runtime",
    "ocr_python_interpreter": "/usr/bin/python3",
    "ocr_install_root": "/opt/ocr-platform",
    "ocr_source_dir": "/opt/ocr-platform/source",
    "ocr_venv_dir": "/opt/ocr-platform/venv",
    "ocr_postgres_port": 5432,
    "ocr_rollout_serial": 1,
    "ocr_rollout_max_fail_percentage": 0,
    "ocr_canary_enabled": False,
    "ocr_control_listen_host": "0.0.0.0",
    "ocr_control_listen_port": 8080,
}

WORKER_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SHARED_SUBTREE_VARS: tuple[str, ...] = (
    "ocr_input_root",
    "ocr_output_root",
    "ocr_platform_root",
)

ABSOLUTE_PATH_VARS: tuple[str, ...] = (
    "ocr_install_root",
    "ocr_source_dir",
    "ocr_venv_dir",
    "ocr_shared_root",
    "ocr_input_root",
    "ocr_output_root",
    "ocr_platform_root",
)

CANARY_CONTRACT_VARS: tuple[str, ...] = (
    "ocr_canary_engine",
    "ocr_canary_model_endpoint",
    "ocr_canary_model_name",
    "ocr_canary_model_api_key_env_var",
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})

PLACEHOLDER_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(change[_-]?me|replace[_-]?me|fill[_-]?me|todo|fixme|tbd|"
    r"placeholder|xxxxx+|yyyyy+)(?:$|[^a-z0-9])"
)
ANGLE_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")

SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:api_?tokens?|api_?keys?|tokens?|secrets?|passwords?|passwd|pwd|"
    r"credentials?|private_key|database_url|db_url|dsn|vault_pass)(?:_|$)"
)
SECRET_REFERENCE_KEY_RE = re.compile(r"(?i)_(secret_var|env_var|secret_ref)$")
ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.]+)?$")
HTTPS_URL_RE = re.compile(r"^https://[^\s/]+(?:/[^\s]*)?$")
CONTROL_URL_RE = re.compile(r"^https?://[^\s/]+(?:/[^\s]*)?$")
GIT_REPO_URL_RE = re.compile(
    r"^(?:https://[^\s/]+/[^\s]+|ssh://[^\s/]+/[^\s]+|git@[^\s:]+:[^\s]+)$"
)


@dataclass(frozen=True)
class Finding:
    code: str
    location: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "location": self.location, "message": self.message}


class InventoryLoadError(Exception):
    def __init__(self, code: str, location: str, message: str) -> None:
        super().__init__(message)
        self.finding = Finding(code, location, message)


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a YAML mapping, converting every failure into an InventoryLoadError."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InventoryLoadError(
            INVENTORY_UNREADABLE,
            str(path),
            f"cannot read {path}: {type(exc).__name__}",
        ) from None
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{path}:{mark.line + 1}" if mark is not None else str(path)
        raise InventoryLoadError(
            YAML_PARSE_ERROR,
            where,
            "file is not valid YAML",
        ) from None
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InventoryLoadError(
            INVENTORY_NOT_MAPPING,
            str(path),
            "top-level YAML document must be a mapping",
        )
    return payload


def _iter_items(node: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    """Yield ``(location, key, value)`` for every mapping entry, recursively."""

    if isinstance(node, dict):
        for key, value in node.items():
            location = f"{path}.{key}" if path else str(key)
            yield location, str(key), value
            yield from _iter_items(value, location)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_items(value, f"{path}[{index}]")


def _iter_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            location = f"{path}.{key}" if path else str(key)
            yield from _iter_strings(value, location)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_strings(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def find_group(root: Any, name: str) -> dict[str, Any] | None:
    """Depth-first search for an inventory group by name."""

    if not isinstance(root, dict):
        return None
    children = root.get("children")
    if isinstance(children, dict):
        for child_name, child in children.items():
            if child_name == name:
                return child if isinstance(child, dict) else {}
            found = find_group(child, name)
            if found is not None:
                return found
    return None


def group_hosts(group: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hosts = group.get("hosts")
    if not isinstance(hosts, dict):
        return {}
    resolved: dict[str, dict[str, Any]] = {}
    group_vars = group.get("vars") if isinstance(group.get("vars"), dict) else {}
    for host, host_vars in hosts.items():
        merged = dict(group_vars)
        if isinstance(host_vars, dict):
            merged.update(host_vars)
        resolved[str(host)] = merged
    return resolved


def _is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER_RE.search(value) or ANGLE_PLACEHOLDER_RE.search(value))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _normalize(path_value: str) -> str:
    return posixpath.normpath(path_value)


def _is_within(child: str, parent: str) -> bool:
    child_norm = _normalize(child)
    parent_norm = _normalize(parent).rstrip("/") or "/"
    if child_norm == parent_norm:
        return True
    prefix = parent_norm if parent_norm.endswith("/") else parent_norm + "/"
    return child_norm.startswith(prefix)


def _is_literal_secret_value(value: Any) -> bool:
    """A secret-named field carries a literal unless it is an on/off toggle.

    Booleans and 0/1 integers are policy switches such as
    ``ocr_control_api_auth_required``; nothing else may sit under a secret-named
    key, including an empty string.
    """

    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value not in (0, 1)
    return isinstance(value, str)


def check_secret_hygiene(document: Any, source: str) -> list[Finding]:
    """Reject literal secret fields and malformed secret references."""

    findings: list[Finding] = []
    for location, key, value in _iter_items(document, source):
        is_reference = bool(SECRET_REFERENCE_KEY_RE.search(key))
        if is_reference:
            if not isinstance(value, str) or not ENV_VAR_NAME_RE.match(value.strip()):
                findings.append(
                    Finding(
                        SECRET_REFERENCE_INVALID,
                        location,
                        "secret reference must name an environment variable "
                        "matching ^[A-Z][A-Z0-9_]*$",
                    )
                )
            continue
        if SECRET_KEY_RE.search(key) and _is_literal_secret_value(value):
            findings.append(
                Finding(
                    LITERAL_SECRET_FIELD,
                    location,
                    "literal secret fields are forbidden; rename to "
                    f"{key}_secret_var and store the value in the private repository",
                )
            )
    return findings


def check_placeholders(document: Any, source: str) -> list[Finding]:
    findings: list[Finding] = []
    for location, value in _iter_strings(document, source):
        if _is_placeholder(value):
            findings.append(
                Finding(
                    PLACEHOLDER_VALUE,
                    location,
                    "value still contains a placeholder token",
                )
            )
    return findings


def _check_release_identity(all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    tag = all_vars.get("ocr_release_tag")
    if isinstance(tag, str) and not RELEASE_TAG_RE.match(tag.strip()):
        findings.append(
            Finding(RELEASE_TAG_INVALID, "all.vars.ocr_release_tag", "expected a vMAJOR.MINOR.PATCH release tag")
        )
    explicit_names = (
        "ocr_release_commit",
        "ocr_source_repo_url",
        "ocr_wheel_url",
        "ocr_wheel_sha256",
    )
    explicit_count = sum(bool(all_vars.get(name)) for name in explicit_names)
    manifest_url = all_vars.get("ocr_release_manifest_url")
    source_repo = all_vars.get("ocr_source_repo_url")
    if explicit_count not in (0, len(explicit_names)):
        findings.append(
            Finding(
                RELEASE_IDENTITY_PARTIAL,
                "all.vars",
                "release identity must be tag-only manifest mode or a complete explicit commit/repository/wheel/digest set",
            )
        )
        return findings
    if explicit_count == 0:
        if manifest_url and (
            not isinstance(manifest_url, str)
            or not HTTPS_URL_RE.match(manifest_url.strip())
            or not manifest_url.strip().endswith(".json")
        ):
            findings.append(
                Finding(
                    RELEASE_MANIFEST_URL_INVALID,
                    "all.vars.ocr_release_manifest_url",
                    "expected an https:// deployment manifest JSON URL",
                )
            )
        return findings

    if manifest_url:
        findings.append(
            Finding(
                RELEASE_IDENTITY_PARTIAL,
                "all.vars.ocr_release_manifest_url",
                "manifest URL cannot be mixed with a complete explicit release identity",
            )
        )
    commit = all_vars.get("ocr_release_commit")
    if not isinstance(commit, str) or not COMMIT_RE.match(commit.strip()):
        findings.append(
            Finding(
                RELEASE_COMMIT_INVALID,
                "all.vars.ocr_release_commit",
                "expected a full 40-character lowercase hex commit id",
            )
        )
    repo_url = source_repo
    if not isinstance(repo_url, str) or not GIT_REPO_URL_RE.match(repo_url.strip()):
        findings.append(
            Finding(SOURCE_REPO_URL_INVALID, "all.vars.ocr_source_repo_url", "expected a valid source repository URL")
        )
    wheel_url = all_vars.get("ocr_wheel_url")
    if not isinstance(wheel_url, str) or not HTTPS_URL_RE.match(wheel_url.strip()):
        findings.append(
            Finding(WHEEL_URL_INVALID, "all.vars.ocr_wheel_url", "expected an https:// wheel URL")
        )
    elif not wheel_url.strip().endswith(".whl"):
        findings.append(
            Finding(WHEEL_URL_INVALID, "all.vars.ocr_wheel_url", "wheel URL must end with .whl")
        )
    digest = all_vars.get("ocr_wheel_sha256")
    if not isinstance(digest, str) or not SHA256_RE.match(digest.strip()):
        findings.append(
            Finding(
                WHEEL_SHA256_INVALID,
                "all.vars.ocr_wheel_sha256",
                "expected a 64-character lowercase hex sha256 digest",
            )
        )
    return findings


def effective_all_vars(provided: dict[str, Any]) -> dict[str, Any]:
    """Apply the same safe defaults and path derivations as the Ansible roles."""

    effective = dict(DEFAULT_ALL_VARS)
    effective.update(provided)
    shared_root = effective.get("ocr_shared_root")
    if isinstance(shared_root, str) and shared_root:
        normalized = shared_root.rstrip("/") or "/"
        effective.setdefault("ocr_input_root", posixpath.join(normalized, "input"))
        effective.setdefault("ocr_output_root", posixpath.join(normalized, "output"))
        effective.setdefault("ocr_platform_root", posixpath.join(normalized, "ocr-platform"))
    return effective


def _check_paths(all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for name in ABSOLUTE_PATH_VARS:
        value = all_vars.get(name)
        if not isinstance(value, str) or not value.startswith("/"):
            findings.append(
                Finding(PATH_NOT_ABSOLUTE, f"all.vars.{name}", "expected an absolute POSIX path")
            )
            continue
        if ".." in value.split("/"):
            findings.append(
                Finding(PATH_TRAVERSAL, f"all.vars.{name}", "path must not contain '..' segments")
            )
    shared_root = all_vars.get("ocr_shared_root")
    if isinstance(shared_root, str) and shared_root.startswith("/"):
        for name in SHARED_SUBTREE_VARS:
            value = all_vars.get(name)
            if not isinstance(value, str) or not value.startswith("/"):
                continue
            if ".." in value.split("/"):
                continue
            if not _is_within(value, shared_root):
                findings.append(
                    Finding(
                        PATH_ESCAPES_SHARED_ROOT,
                        f"all.vars.{name}",
                        "path must resolve inside ocr_shared_root",
                    )
                )
    return findings


def _check_external_dependencies(all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    port = _as_int(all_vars.get("ocr_postgres_port"))
    if port is None or not 1 <= port <= 65535:
        findings.append(
            Finding(POSTGRES_PORT_INVALID, "all.vars.ocr_postgres_port", "expected a TCP port between 1 and 65535")
        )
    return findings


def _check_rollout(all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    serial = _as_int(all_vars.get("ocr_rollout_serial"))
    if serial is None or serial < 1:
        findings.append(
            Finding(
                ROLLOUT_SERIAL_INVALID,
                "all.vars.ocr_rollout_serial",
                "expected a rollout batch size of at least 1",
            )
        )
    percentage = _as_int(all_vars.get("ocr_rollout_max_fail_percentage"))
    if percentage is None or not 0 <= percentage <= 100:
        findings.append(
            Finding(
                ROLLOUT_FAILURE_PERCENTAGE_INVALID,
                "all.vars.ocr_rollout_max_fail_percentage",
                "expected a failure percentage between 0 and 100",
            )
        )
    return findings


def _check_canary(all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    enabled = all_vars.get("ocr_canary_enabled")
    if not isinstance(enabled, bool):
        findings.append(
            Finding(
                CANARY_FLAG_INVALID,
                "all.vars.ocr_canary_enabled",
                "expected an explicit YAML boolean",
            )
        )
        return findings
    if not enabled:
        return findings
    for name in CANARY_CONTRACT_VARS:
        value = all_vars.get(name)
        if not isinstance(value, str) or not value.strip():
            findings.append(
                Finding(
                    CANARY_CONTRACT_INCOMPLETE,
                    f"all.vars.{name}",
                    "canary engine, model endpoint, model name, and model key env-var "
                    "reference are all required, even when the canary is disabled",
                )
            )
    return findings


def _check_control(inventory_all: dict[str, Any], all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    group = find_group(inventory_all, CONTROL_GROUP)
    if group is None:
        return [Finding(CONTROL_GROUP_MISSING, f"all.children.{CONTROL_GROUP}", "control group is missing")]
    hosts = group_hosts(group)
    if len(hosts) != 1:
        findings.append(
            Finding(
                CONTROL_GROUP_NOT_SINGLE_HOST,
                f"all.children.{CONTROL_GROUP}.hosts",
                "this bundle deploys exactly one Control host",
            )
        )
    for host, host_vars in sorted(hosts.items()):
        base = f"all.children.{CONTROL_GROUP}.hosts.{host}"
        merged = dict(all_vars)
        merged.update(host_vars)
        for name in REQUIRED_CONTROL_VARS:
            if merged.get(name) in (None, ""):
                findings.append(Finding(CONTROL_VAR_MISSING, f"{base}.{name}", "required Control variable is missing"))
        port = _as_int(merged.get("ocr_control_listen_port"))
        if port is None or not 1 <= port <= 65535:
            findings.append(
                Finding(
                    CONTROL_PORT_INVALID,
                    f"{base}.ocr_control_listen_port",
                    "expected a TCP port between 1 and 65535",
                )
            )
        url = merged.get("ocr_control_url")
        if not isinstance(url, str) or not CONTROL_URL_RE.match(url.strip()):
            findings.append(
                Finding(
                    CONTROL_URL_INVALID,
                    f"{base}.ocr_control_url",
                    "expected an http:// or https:// URL reachable from every worker",
                )
            )
        listen_host = merged.get("ocr_control_listen_host")
        token_ref = merged.get("ocr_api_token_secret_var")
        is_loopback = isinstance(listen_host, str) and listen_host.strip() in LOOPBACK_HOSTS
        has_token_ref = isinstance(token_ref, str) and bool(ENV_VAR_NAME_RE.match(token_ref.strip()))
        if not is_loopback and not has_token_ref:
            findings.append(
                Finding(
                    CONTROL_TOKEN_SECRET_REQUIRED,
                    f"{base}.ocr_api_token_secret_var",
                    "a Control that listens off-loopback requires an API token secret reference",
                )
            )
    return findings


def _check_workers(inventory_all: dict[str, Any], all_vars: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    group = find_group(inventory_all, WORKER_GROUP)
    if group is None:
        return [Finding(WORKER_GROUP_MISSING, f"all.children.{WORKER_GROUP}", "worker group is missing")]
    hosts = group_hosts(group)
    if not hosts:
        return [Finding(WORKER_GROUP_EMPTY, f"all.children.{WORKER_GROUP}.hosts", "worker group has no hosts")]

    seen_server_ids: dict[str, str] = {}
    for host, host_vars in sorted(hosts.items()):
        base = f"all.children.{WORKER_GROUP}.hosts.{host}"
        merged = dict(all_vars)
        merged.update(host_vars)
        server_id = merged.get("ocr_worker_server_id") or host
        merged["ocr_worker_server_id"] = server_id
        merged.setdefault("ocr_worker_work_dir", f"/var/lib/ocr-agent/{server_id}/work")
        merged.setdefault("ocr_worker_log_dir", f"/var/log/ocr-agent/{server_id}")
        merged.setdefault("ocr_worker_spool_dir", f"/var/lib/ocr-agent/{server_id}/spool")
        for name in REQUIRED_WORKER_VARS:
            if merged.get(name) in (None, ""):
                findings.append(Finding(WORKER_VAR_MISSING, f"{base}.{name}", "required worker variable is missing"))
        for name in ("ocr_worker_work_dir", "ocr_worker_log_dir", "ocr_worker_spool_dir"):
            value = merged.get(name)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or not value.startswith("/"):
                findings.append(Finding(PATH_NOT_ABSOLUTE, f"{base}.{name}", "expected an absolute POSIX path"))
            elif ".." in value.split("/"):
                findings.append(Finding(PATH_TRAVERSAL, f"{base}.{name}", "path must not contain '..' segments"))
        if not isinstance(server_id, str) or not WORKER_SERVER_ID_RE.fullmatch(server_id):
            findings.append(
                Finding(
                    WORKER_SERVER_ID_INVALID,
                    f"{base}.ocr_worker_server_id",
                    "expected 1-128 letters, digits, dots, underscores, or hyphens",
                )
            )
        if isinstance(server_id, str) and server_id:
            previous = seen_server_ids.get(server_id)
            if previous is not None:
                findings.append(
                    Finding(
                        WORKER_SERVER_ID_DUPLICATE,
                        f"{base}.ocr_worker_server_id",
                        f"server id is already used by host {previous}",
                    )
                )
            else:
                seen_server_ids[server_id] = host
    return findings


def validate_documents(
    inventory: dict[str, Any],
    *,
    inventory_source: str = "inventory",
    extra_documents: Sequence[tuple[str, dict[str, Any]]] = (),
) -> list[Finding]:
    """Validate an already-parsed inventory plus optional group_vars documents."""

    findings: list[Finding] = []
    for source, document in ((inventory_source, inventory), *extra_documents):
        findings.extend(check_secret_hygiene(document, source))
        findings.extend(check_placeholders(document, source))

    inventory_all = inventory.get("all")
    if not isinstance(inventory_all, dict):
        findings.append(Finding(MISSING_ALL_GROUP, "all", "inventory must define a top-level 'all' group"))
        return _sorted(findings)

    provided_all_vars: dict[str, Any] = {}
    for _source, document in extra_documents:
        if isinstance(document, dict):
            provided_all_vars.update(document)
    inventory_vars = inventory_all.get("vars")
    if isinstance(inventory_vars, dict):
        provided_all_vars.update(inventory_vars)

    for name in REQUIRED_ALL_VARS:
        if name not in provided_all_vars or provided_all_vars[name] is None or provided_all_vars[name] == "":
            findings.append(
                Finding(MISSING_REQUIRED_VAR, f"all.vars.{name}", "required inventory variable is missing")
            )

    findings.extend(_check_release_identity(provided_all_vars))
    all_vars = effective_all_vars(provided_all_vars)
    findings.extend(_check_paths(all_vars))
    findings.extend(_check_external_dependencies(all_vars))
    findings.extend(_check_rollout(all_vars))
    findings.extend(_check_canary(all_vars))
    findings.extend(_check_control(inventory_all, all_vars))
    findings.extend(_check_workers(inventory_all, all_vars))
    return _sorted(findings)


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda item: (item.code, item.location, item.message))


def validate_paths(inventory_path: Path, group_vars_paths: Sequence[Path] = ()) -> list[Finding]:
    try:
        inventory = load_yaml_mapping(inventory_path)
    except InventoryLoadError as exc:
        return [exc.finding]
    extra: list[tuple[str, dict[str, Any]]] = []
    for path in group_vars_paths:
        try:
            extra.append((path.name, load_yaml_mapping(path)))
        except InventoryLoadError as exc:
            return [exc.finding]
    return validate_documents(inventory, inventory_source=inventory_path.name, extra_documents=extra)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an OcrParser production inventory against the deployment contract.",
    )
    parser.add_argument("inventory", type=Path, help="Path to the Ansible inventory YAML file.")
    parser.add_argument(
        "--group-vars",
        dest="group_vars",
        action="append",
        default=[],
        type=Path,
        help="Additional group_vars YAML file merged as all-level vars. Repeatable.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    findings = validate_paths(args.inventory, args.group_vars)
    ok = not findings
    if args.json:
        payload = {
            "ok": ok,
            "inventory": args.inventory.name,
            "findings": [finding.to_dict() for finding in findings],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    elif ok:
        print(f"{args.inventory.name}: inventory contract satisfied.")
    else:
        print(f"{args.inventory.name}: inventory contract violated.", file=sys.stderr)
        for finding in findings:
            print(f"{finding.code} {finding.location}: {finding.message}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
