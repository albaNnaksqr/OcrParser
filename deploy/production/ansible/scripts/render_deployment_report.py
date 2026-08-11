#!/usr/bin/env python3
"""Render a redacted deployment report from verify/canary structured facts.

The report is built by allow-list: only release identity, migration state,
worker counts, canary outcome, a timestamp, and a fixed set of status codes ever
reach the output. Everything else in the input is dropped. As defence in depth,
every value that survives the allow-list is then passed through a recursive
redactor that removes addresses, filesystem paths, tokens, secret-like keys, and
anything long enough to be OCR body text.

Reports are written to a gitignored directory (``reports/`` by default) so a
real deployment record never lands in this public bundle.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


REPORT_SCHEMA = "ocrparser.deployment-report/1"
DEFAULT_OUTPUT_DIR = Path("reports")

REDACTED_SECRET = "[redacted:secret]"
REDACTED_ADDRESS = "[redacted:address]"
REDACTED_PATH = "[redacted:path]"
REDACTED_TEXT = "[redacted:text]"

MAX_SCALAR_LENGTH = 120

# Fixed status codes. Callers may branch on these; they are part of the contract.
CONTROL_HEALTHY = "control_healthy"
CONTROL_UNHEALTHY = "control_unhealthy"
CONTROL_READY = "control_ready"
CONTROL_NOT_READY = "control_not_ready"
CONTROL_SOURCE_REVISION_MATCH = "control_source_revision_match"
CONTROL_SOURCE_REVISION_MISMATCH = "control_source_revision_mismatch"
MIGRATIONS_CURRENT = "migrations_current"
MIGRATIONS_NOT_CURRENT = "migrations_not_current"
WORKERS_ALL_ONLINE = "workers_all_online"
WORKERS_OFFLINE = "workers_offline"
WORKER_SERVER_ID_DUPLICATE = "worker_server_id_duplicate"
WORKER_VERSION_MISMATCH = "worker_version_mismatch"
WORKER_SHARED_ROOT_UNAVAILABLE = "worker_shared_root_unavailable"
WORKER_SPOOL_QUARANTINE_PRESENT = "worker_spool_quarantine_present"
CANARY_DISABLED = "canary_disabled"
CANARY_SUCCEEDED = "canary_succeeded"
CANARY_FAILED = "canary_failed"
CANARY_ARTIFACTS_MISSING = "canary_artifacts_missing"
RELEASE_IDENTITY_INCOMPLETE = "release_identity_incomplete"

REPORT_CODES: tuple[str, ...] = (
    CANARY_ARTIFACTS_MISSING,
    CANARY_DISABLED,
    CANARY_FAILED,
    CANARY_SUCCEEDED,
    CONTROL_HEALTHY,
    CONTROL_NOT_READY,
    CONTROL_READY,
    CONTROL_SOURCE_REVISION_MATCH,
    CONTROL_SOURCE_REVISION_MISMATCH,
    CONTROL_UNHEALTHY,
    MIGRATIONS_CURRENT,
    MIGRATIONS_NOT_CURRENT,
    RELEASE_IDENTITY_INCOMPLETE,
    WORKERS_ALL_ONLINE,
    WORKERS_OFFLINE,
    WORKER_SERVER_ID_DUPLICATE,
    WORKER_SHARED_ROOT_UNAVAILABLE,
    WORKER_SPOOL_QUARANTINE_PRESENT,
    WORKER_VERSION_MISMATCH,
)

SECRET_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:api_?tokens?|api_?keys?|tokens?|secrets?|passwords?|passwd|pwd|"
    r"credentials?|private_key|database_url|db_url|dsn|authorization|cookie)(?:_|$)"
)
TEXT_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:text|content|contents|markdown|body|stdout|stderr|output|"
    r"log|logs|message|messages|excerpt|sample|samples)(?:_|$)"
)
ADDRESS_KEY_RE = re.compile(
    r"(?i)(?:^|_)(?:host|hosts|hostname|address|addr|url|urls|endpoint|endpoints|"
    r"ip|fqdn|inventory_hostname)(?:_|$)"
)
PATH_KEY_RE = re.compile(r"(?i)(?:^|_)(?:path|paths|dir|dirs|directory|root|roots|mount|file|files)(?:_|$)")

URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
IPV6_RE = re.compile(r"^[0-9A-Fa-f:]*::[0-9A-Fa-f:]*$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.[A-Za-z]{2,}$")
USER_AT_HOST_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
PATH_RE = re.compile(r"(^|\s)/[^\s]*|^[A-Za-z]:\\")


def _redact_string(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return value
    if len(stripped) > MAX_SCALAR_LENGTH:
        return REDACTED_TEXT
    if "\n" in stripped:
        return REDACTED_TEXT
    if URL_RE.match(stripped) or USER_AT_HOST_RE.match(stripped):
        return REDACTED_ADDRESS
    if IPV4_RE.match(stripped) or IPV6_RE.match(stripped):
        return REDACTED_ADDRESS
    if HOSTNAME_RE.match(stripped):
        return REDACTED_ADDRESS
    if PATH_RE.search(stripped):
        return REDACTED_PATH
    return value


def redact(node: Any, key: str | None = None) -> Any:
    """Recursively redact addresses, paths, secrets, and free text."""

    if key is not None:
        if SECRET_KEY_RE.search(key):
            return REDACTED_SECRET
        if TEXT_KEY_RE.search(key) and not isinstance(node, (bool, int, float)):
            return REDACTED_TEXT
        if ADDRESS_KEY_RE.search(key) and isinstance(node, str):
            return REDACTED_ADDRESS
        if PATH_KEY_RE.search(key) and isinstance(node, str):
            return REDACTED_PATH
    if isinstance(node, dict):
        return {str(child_key): redact(child, str(child_key)) for child_key, child in node.items()}
    if isinstance(node, list):
        return [redact(child, key) for child in node]
    if isinstance(node, str):
        return _redact_string(node)
    if isinstance(node, (bool, int, float)) or node is None:
        return node
    return _redact_string(str(node))


def _mapping(payload: Any, name: str) -> dict[str, Any]:
    value = payload.get(name) if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def _flag(payload: dict[str, Any], name: str) -> bool:
    return payload.get(name) is True


def _count(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    return value.strip() if isinstance(value, str) else ""


def build_report(
    verify: dict[str, Any],
    canary: dict[str, Any] | None = None,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Extract the allow-listed report fields and derive the fixed status codes."""

    control = _mapping(verify, "control")
    migrations = _mapping(verify, "migrations")
    workers = _mapping(verify, "workers")
    canary_payload = canary if isinstance(canary, dict) else _mapping(verify, "canary")

    release = {
        "tag": _text(verify, "release_tag"),
        "commit": _text(verify, "release_commit"),
        "wheel_sha256": _text(verify, "wheel_sha256"),
    }
    worker_total = _count(workers, "total")
    worker_online = _count(workers, "online")
    worker_unique = _count(workers, "unique_server_ids")
    worker_shared_ok = _count(workers, "shared_root_ok")
    worker_quarantine = _count(workers, "spool_quarantine")
    worker_version_consistent = _flag(workers, "version_consistent")

    canary_enabled = _flag(canary_payload, "enabled")
    canary_status = _text(canary_payload, "status") or ("unknown" if canary_enabled else "skipped")
    canary_artifacts = _flag(canary_payload, "artifacts_present")

    codes: list[str] = []
    if not all(release.values()):
        codes.append(RELEASE_IDENTITY_INCOMPLETE)
    codes.append(CONTROL_HEALTHY if _flag(control, "healthy") else CONTROL_UNHEALTHY)
    codes.append(CONTROL_READY if _flag(control, "ready") else CONTROL_NOT_READY)
    codes.append(
        CONTROL_SOURCE_REVISION_MATCH
        if _flag(control, "source_revision_matches")
        else CONTROL_SOURCE_REVISION_MISMATCH
    )
    codes.append(MIGRATIONS_CURRENT if _flag(migrations, "verified") else MIGRATIONS_NOT_CURRENT)
    if worker_total > 0 and worker_online == worker_total:
        codes.append(WORKERS_ALL_ONLINE)
    else:
        codes.append(WORKERS_OFFLINE)
    if worker_unique != worker_total:
        codes.append(WORKER_SERVER_ID_DUPLICATE)
    if not worker_version_consistent:
        codes.append(WORKER_VERSION_MISMATCH)
    if worker_shared_ok != worker_total:
        codes.append(WORKER_SHARED_ROOT_UNAVAILABLE)
    if worker_quarantine > 0:
        codes.append(WORKER_SPOOL_QUARANTINE_PRESENT)
    if not canary_enabled:
        codes.append(CANARY_DISABLED)
    elif canary_status == "succeeded":
        codes.append(CANARY_SUCCEEDED)
        if not canary_artifacts:
            codes.append(CANARY_ARTIFACTS_MISSING)
    else:
        codes.append(CANARY_FAILED)

    unknown = [code for code in codes if code not in REPORT_CODES]
    if unknown:  # pragma: no cover - guards against future edits
        raise ValueError(f"report emitted codes outside the fixed set: {sorted(unknown)}")

    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": release,
        "migrations": {"verified": _flag(migrations, "verified")},
        "workers": {
            "total": worker_total,
            "online": worker_online,
            "unique_server_ids": worker_unique,
            "shared_root_ok": worker_shared_ok,
            "spool_quarantine": worker_quarantine,
            "version_consistent": worker_version_consistent,
        },
        "canary": {
            "enabled": canary_enabled,
            "status": canary_status,
            "artifacts_present": canary_artifacts,
        },
        "codes": sorted(set(codes)),
        "ok": not (set(codes) & _FAILURE_CODES),
    }
    return redact(report)


_FAILURE_CODES = frozenset(
    {
        CANARY_ARTIFACTS_MISSING,
        CANARY_FAILED,
        CONTROL_NOT_READY,
        CONTROL_SOURCE_REVISION_MISMATCH,
        CONTROL_UNHEALTHY,
        MIGRATIONS_NOT_CURRENT,
        RELEASE_IDENTITY_INCOMPLETE,
        WORKERS_OFFLINE,
        WORKER_SERVER_ID_DUPLICATE,
        WORKER_SHARED_ROOT_UNAVAILABLE,
        WORKER_SPOOL_QUARANTINE_PRESENT,
        WORKER_VERSION_MISMATCH,
    }
)


def render_markdown(report: dict[str, Any]) -> str:
    release = report["release"]
    workers = report["workers"]
    canary = report["canary"]
    lines = [
        "# OcrParser production deployment report",
        "",
        f"- Schema: `{report['schema']}`",
        f"- Generated at: {report['generated_at']}",
        f"- Overall: {'ok' if report['ok'] else 'failed'}",
        "",
        "## Release identity",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Release tag | `{release['tag']}` |",
        f"| Source commit | `{release['commit']}` |",
        f"| Wheel sha256 | `{release['wheel_sha256']}` |",
        "",
        "## Control database migrations",
        "",
        f"- Verified: {str(report['migrations']['verified']).lower()}",
        "",
        "## Workers",
        "",
        "| Metric | Count |",
        "| --- | --- |",
        f"| Total | {workers['total']} |",
        f"| Online | {workers['online']} |",
        f"| Unique server ids | {workers['unique_server_ids']} |",
        f"| Shared root usable | {workers['shared_root_ok']} |",
        f"| Spool quarantine entries | {workers['spool_quarantine']} |",
        "",
        f"- Version consistent: {str(workers['version_consistent']).lower()}",
        "",
        "## Canary",
        "",
        f"- Enabled: {str(canary['enabled']).lower()}",
        f"- Status: {canary['status']}",
        f"- Artifacts present: {str(canary['artifacts_present']).lower()}",
        "",
        "## Codes",
        "",
    ]
    lines.extend(f"- `{code}`" for code in report["codes"])
    lines.extend(
        [
            "",
            "Hostnames, URLs, filesystem paths, tokens, and document content are",
            "redacted by construction. Correlate with the private deployment",
            "repository if you need the concrete targets.",
            "",
        ]
    )
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render report.json and report.md from verify/canary structured facts.",
    )
    parser.add_argument("--verify", required=True, type=Path, help="Structured verify fact JSON file.")
    parser.add_argument("--canary", type=Path, help="Optional structured canary fact JSON file.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for report.json and report.md. Gitignored by default.",
    )
    parser.add_argument("--generated-at", help="Override the report timestamp, for reproducible output.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verify = _load_json(args.verify)
        canary = _load_json(args.canary) if args.canary else None
    except (OSError, ValueError) as exc:
        print(f"error: cannot read structured input: {type(exc).__name__}", file=sys.stderr)
        return 2

    report = build_report(verify, canary, generated_at=args.generated_at)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote report.json and report.md ({'ok' if report['ok'] else 'failed'})")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
