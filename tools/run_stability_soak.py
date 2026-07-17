#!/usr/bin/env python3
"""Run auditable production-like stability cycles without persisting secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from email.parser import Parser
from pathlib import Path
from typing import Any, Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.3.0"
EXPECTED_REVISION = "47e1c0399db97f4ec48715548b8c937bc77c20ba"
SUCCESS_JOB_STATUSES = {"succeeded"}
SUCCESS_DOCUMENT_STATUSES = {"success", "success_fallback_text", "success_fallback_image"}
KNOWN_STAGES = {
    "layout",
    "recognition",
    "primary_inference",
    "postprocess",
    "text_fallback",
    "image_fallback",
    "single_stage_ocr",
    "output",
}
KNOWN_STAGE_STATUSES = {"success", "failed", "skipped"}
KNOWN_FALLBACK_REASONS = {
    "layout_unavailable",
    "layout_empty",
    "layout_output_unusable",
    "primary_stage_failed",
    "text_fallback_unavailable",
    "multiple",
    "other",
}


@dataclass(frozen=True)
class GateResult:
    name: str
    status: str
    detail: str
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in {"pass", "skip"}


@dataclass(frozen=True)
class ResourceSample:
    label: str
    pid: int | None
    rss_kib: int | None
    fd_count: int | None
    captured_at: float
    status: str


@dataclass(frozen=True)
class FaultResult:
    name: str
    cycle: int
    status: str
    returncode: int | None
    duration_seconds: float
    detail: str


@dataclass
class CycleResult:
    cycle: int
    input_mode: str
    shared_root: str
    document_count: int
    status: str
    duration_seconds: float
    job_id: str | None = None
    job_summary: dict[str, Any] = field(default_factory=dict)
    manifest_integrity: dict[str, Any] = field(default_factory=dict)
    output_audit: dict[str, Any] = field(default_factory=dict)
    stage_counts: dict[str, int] = field(default_factory=dict)
    fallback_counts: dict[str, int] = field(default_factory=dict)
    unknown_labels: list[str] = field(default_factory=list)
    failed_samples: list[dict[str, Any]] = field(default_factory=list)
    fault_results: list[FaultResult] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        integrity_ok = bool(self.manifest_integrity.get("ok"))
        audit_ok = bool(self.output_audit.get("ok"))
        return (
            self.status == "pass"
            and self.job_summary.get("status") in SUCCESS_JOB_STATUSES
            and integrity_ok
            and audit_ok
            and not self.unknown_labels
            and not self.failed_samples
            and all(item.status == "pass" for item in self.fault_results)
        )


@dataclass(frozen=True)
class FaultHook:
    name: str
    cycle: int
    after_seconds: float
    argv: tuple[str, ...]


def _tail(text: str, limit: int = 4000) -> str:
    return text[-limit:]


def redact_text(text: str, *, secret_values: Iterable[str] = ()) -> str:
    redacted = re.sub(r"(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)", r"\1***\2", text)
    for value in secret_values:
        if value:
            redacted = redacted.replace(value, "***")
    return redacted


def verify_release_wheel(path: Path, *, expected_version: str, expected_revision: str) -> GateResult:
    started = time.monotonic()
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
            metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
            provenance = json.loads(archive.read("ocr_platform/_build_info.json"))
        actual_version = metadata.get("Version")
        actual_revision = str(provenance.get("source_revision") or "")
        dirty = provenance.get("dirty")
        if actual_version != expected_version:
            raise ValueError(f"wheel version {actual_version!r} != {expected_version!r}")
        if actual_revision != expected_revision:
            raise ValueError(f"wheel revision {actual_revision or '<missing>'} != {expected_revision}")
        if dirty is not False:
            raise ValueError("wheel provenance is not a clean release build")
        detail = f"version={actual_version} revision={actual_revision} dirty=false"
        return GateResult("release_wheel", "pass", detail, time.monotonic() - started)
    except (OSError, KeyError, StopIteration, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        return GateResult("release_wheel", "fail", str(exc), time.monotonic() - started)


def request_json(
    url: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object from {url}")
    return payload


def verify_source_offer(url: str, *, expected_version: str, expected_revision: str) -> GateResult:
    started = time.monotonic()
    try:
        payload = request_json(url)
        if payload.get("version") != expected_version:
            raise ValueError(f"source version {payload.get('version')!r} != {expected_version!r}")
        if payload.get("source_revision") != expected_revision:
            raise ValueError("source revision does not match the release wheel")
        if payload.get("release_build") is not True:
            raise ValueError("source offer is not reporting release_build=true")
        return GateResult(
            "source_offer",
            "pass",
            f"version={expected_version} revision={expected_revision} release_build=true",
            time.monotonic() - started,
        )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return GateResult("source_offer", "fail", str(exc), time.monotonic() - started)


def run_command_gate(
    name: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env_overrides: dict[str, str] | None = None,
    secret_values: Iterable[str] = (),
    timeout: float = 600.0,
) -> GateResult:
    started = time.monotonic()
    try:
        env = os.environ.copy()
        env.update(env_overrides or {})
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GateResult(name, "fail", str(exc), time.monotonic() - started)
    combined = redact_text(_tail(result.stdout + "\n" + result.stderr), secret_values=secret_values).strip()
    return GateResult(
        name,
        "pass" if result.returncode == 0 else "fail",
        combined or f"exit={result.returncode}",
        time.monotonic() - started,
    )


def load_fault_plan(path: Path | None) -> list[FaultHook]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    hooks: list[FaultHook] = []
    for item in payload.get("hooks", []):
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("every fault hook requires a non-empty string argv list")
        hooks.append(
            FaultHook(
                name=str(item["name"]),
                cycle=int(item["cycle"]),
                after_seconds=max(float(item.get("after_seconds", 0)), 0.0),
                argv=tuple(argv),
            )
        )
    return sorted(hooks, key=lambda item: (item.cycle, item.after_seconds, item.name))


def collect_resource_sample(label: str, pid_file: Path) -> ResourceSample:
    captured_at = time.time()
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        status_text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        match = re.search(r"^VmRSS:\s+(\d+)\s+kB$", status_text, re.MULTILINE)
        rss_kib = int(match.group(1)) if match else None
        fd_count = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        return ResourceSample(label, pid, rss_kib, fd_count, captured_at, "ok")
    except (OSError, ValueError):
        return ResourceSample(label, None, None, None, captured_at, "unavailable")


def scan_sidecars(output_dir: Path) -> tuple[dict[str, int], dict[str, int], list[str], list[dict[str, Any]]]:
    stage_counts: Counter[str] = Counter()
    fallback_counts: Counter[str] = Counter()
    unknown: set[str] = set()
    failed_samples: list[dict[str, Any]] = []
    for path in sorted(output_dir.rglob(".ocr_status.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed_samples.append({"path": str(path), "reason": "sidecar_unreadable"})
            continue
        status = str(payload.get("status") or "unknown")
        if status not in SUCCESS_DOCUMENT_STATUSES:
            failed_samples.append(
                {
                    "path": str(path),
                    "status": status,
                    "failure_category": payload.get("failure_category"),
                }
            )
        for stage in payload.get("stages") or []:
            if not isinstance(stage, dict):
                unknown.add("stage:not_object")
                continue
            stage_name = str(stage.get("stage") or "")
            stage_status = str(stage.get("status") or "")
            stage_counts[f"{stage_name}:{stage_status}"] += 1
            if stage_name not in KNOWN_STAGES:
                unknown.add(f"stage:{stage_name or '<empty>'}")
            if stage_status not in KNOWN_STAGE_STATUSES:
                unknown.add(f"stage_status:{stage_status or '<empty>'}")
        fallback = payload.get("fallback")
        if isinstance(fallback, dict) and fallback.get("used"):
            source = str(fallback.get("source_stage") or "")
            reason = str(fallback.get("reason") or "")
            fallback_counts[f"{source}:{reason}"] += 1
            if source not in KNOWN_STAGES:
                unknown.add(f"fallback_source:{source or '<empty>'}")
            if reason not in KNOWN_FALLBACK_REASONS:
                unknown.add(f"fallback_reason:{reason or '<empty>'}")
    return dict(sorted(stage_counts.items())), dict(sorted(fallback_counts.items())), sorted(unknown), failed_samples[:20]


def audit_directory_outputs(*, input_dir: Path, output_dir: Path) -> dict[str, Any]:
    expected_stems = {path.stem for path in input_dir.glob("*.pdf")}
    missing_sidecars: list[str] = []
    missing_markdown: list[str] = []
    failed_documents: list[dict[str, str]] = []
    for stem in sorted(expected_stems):
        document_dir = output_dir / stem
        sidecar_path = document_dir / ".ocr_status.json"
        if not sidecar_path.is_file():
            missing_sidecars.append(stem)
        else:
            try:
                payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
                status = str(payload.get("status") or "unknown")
            except (OSError, json.JSONDecodeError):
                status = "unreadable"
            if status not in SUCCESS_DOCUMENT_STATUSES:
                failed_documents.append({"stem": stem, "status": status})
        combined = [
            path
            for path in document_dir.rglob(f"{stem}.md")
            if not path.name.startswith("page_")
        ]
        if len(combined) != 1:
            missing_markdown.append(stem)
    actual_stems = {path.parent.name for path in output_dir.glob("*/.ocr_status.json")}
    unexpected_stems = sorted(actual_stems - expected_stems)
    ok = not (missing_sidecars or missing_markdown or failed_documents or unexpected_stems)
    return {
        "ok": ok,
        "mode": "directory",
        "expected_document_count": len(expected_stems),
        "actual_sidecar_count": len(actual_stems),
        "missing_sidecars": missing_sidecars,
        "missing_or_duplicate_combined_markdown": missing_markdown,
        "failed_documents": failed_documents,
        "unexpected_stems": unexpected_stems,
    }


def parse_final_summary(text: str) -> tuple[str | None, dict[str, Any]]:
    job_id: str | None = None
    summary: dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith("JOB_ID "):
            job_id = line.split(" ", 1)[1].strip()
        elif line.startswith("FINAL_SUMMARY "):
            payload = json.loads(line.split(" ", 1)[1])
            if isinstance(payload, dict):
                summary = payload
    return job_id, summary


def execute_cycle(
    *,
    cycle: int,
    mode: str,
    args: argparse.Namespace,
    token: str,
    hooks: Sequence[FaultHook],
    report_dir: Path,
) -> CycleResult:
    cycle_root = Path(args.shared_root) / f"cycle-{cycle:03d}-{mode}"
    runtime_repo = Path(args.runtime_repo_dir)
    argv = [
        args.runtime_python,
        str(runtime_repo / "tools" / "run_distributed_walkthrough.py"),
        "--control-url",
        args.control_url,
        "--api-token-env-var",
        args.control_token_env_var,
        "--shared-root",
        str(cycle_root),
        "--worker-id",
        args.worker_ids[0],
        "--input-mode",
        mode,
        "--document-count",
        str(args.documents_per_cycle),
        "--target-files-per-shard",
        str(args.target_files_per_shard),
        "--max-shard-attempts",
        str(args.max_shard_attempts),
        "--engine",
        args.engine,
        "--ocr-host",
        args.ocr_host,
        "--ocr-port",
        str(args.ocr_port),
        "--model-name",
        args.model_name,
        "--polls",
        str(args.polls),
        "--interval",
        str(args.poll_interval),
    ]
    for worker_id in args.worker_ids:
        argv.extend(["--allowed-worker-id", worker_id])
    if args.model_api_key_env_var:
        argv.extend(["--api-key-env-var", args.model_api_key_env_var])
    if args.disable_process_pool:
        argv.append("--disable-process-pool")

    stdout_path = report_dir / f"cycle-{cycle:03d}.stdout.log"
    stderr_path = report_dir / f"cycle-{cycle:03d}.stderr.log"
    started = time.monotonic()
    fault_results: list[FaultResult] = []
    env = os.environ.copy()
    with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open("w", encoding="utf-8") as stderr_file:
        process = subprocess.Popen(argv, cwd=runtime_repo, env=env, stdout=stdout_file, stderr=stderr_file, text=True)
        pending_hooks = [hook for hook in hooks if hook.cycle == cycle]
        while process.poll() is None:
            elapsed = time.monotonic() - started
            ready = [hook for hook in pending_hooks if hook.after_seconds <= elapsed]
            for hook in ready:
                hook_started = time.monotonic()
                hook_env = env.copy()
                hook_env.update({"OCR_SOAK_CYCLE": str(cycle), "OCR_SOAK_REPORT_DIR": str(report_dir)})
                try:
                    result = subprocess.run(
                        list(hook.argv),
                        cwd=runtime_repo,
                        env=hook_env,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=args.fault_timeout,
                        check=False,
                    )
                    detail = redact_text(_tail(result.stdout + "\n" + result.stderr), secret_values=[token]).strip()
                    fault_results.append(
                        FaultResult(
                            hook.name,
                            cycle,
                            "pass" if result.returncode == 0 else "fail",
                            result.returncode,
                            time.monotonic() - hook_started,
                            detail or f"exit={result.returncode}",
                        )
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    fault_results.append(FaultResult(hook.name, cycle, "fail", None, time.monotonic() - hook_started, str(exc)))
                pending_hooks.remove(hook)
            time.sleep(0.2)
        returncode = process.wait()
        for hook in pending_hooks:
            fault_results.append(
                FaultResult(
                    hook.name,
                    cycle,
                    "fail",
                    None,
                    time.monotonic() - started,
                    "cycle finished before the scheduled fault hook ran",
                )
            )
    duration = time.monotonic() - started
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    job_id, summary = parse_final_summary(stdout)
    integrity: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    failed_samples: list[dict[str, Any]] = []
    if job_id:
        try:
            integrity = request_json(
                f"{args.control_url.rstrip('/')}/api/jobs/{job_id}/manifest/integrity",
                token=token,
            )
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            integrity = {"ok": False, "error": str(exc)}
        if mode == "directory" and integrity.get("status") == "missing_manifest":
            integrity = {
                "ok": True,
                "status": "not_applicable",
                "reason": "directory mode does not create a manifest",
            }
        manifest_path = integrity.get("manifest_path")
        if manifest_path and Path(str(manifest_path)).is_file():
            try:
                from ocr_parser.infra.output_audit import audit_manifest_outputs

                audit = audit_manifest_outputs(
                    manifest_path=Path(str(manifest_path)),
                    output_dir=cycle_root / "output",
                    check_input=True,
                ).to_dict()
            except (OSError, ValueError) as exc:
                audit = {"ok": False, "error": str(exc)}
        elif mode in {"directory", "distributed_remote_folder_snapshot"}:
            audit = audit_directory_outputs(
                input_dir=cycle_root / "input",
                output_dir=cycle_root / "output",
            )
    stages, fallbacks, unknown, sidecar_failures = scan_sidecars(cycle_root / "output")
    failed_samples.extend(sidecar_failures)
    status = "pass" if returncode == 0 else "fail"
    return CycleResult(
        cycle=cycle,
        input_mode=mode,
        shared_root=str(cycle_root),
        document_count=args.documents_per_cycle,
        status=status,
        duration_seconds=duration,
        job_id=job_id,
        job_summary=summary,
        manifest_integrity=integrity,
        output_audit=audit,
        stage_counts=stages,
        fallback_counts=fallbacks,
        unknown_labels=unknown,
        failed_samples=failed_samples,
        fault_results=fault_results,
        stdout_tail=redact_text(_tail(stdout), secret_values=[token]),
        stderr_tail=redact_text(_tail(stderr), secret_values=[token]),
    )


def analyze_resource_growth(samples: Sequence[ResourceSample], *, limit: float = 0.20) -> GateResult:
    by_label: dict[str, list[ResourceSample]] = {}
    for sample in samples:
        if sample.status == "ok":
            by_label.setdefault(sample.label, []).append(sample)
    failures: list[str] = []
    observations: list[str] = []
    for label, rows in sorted(by_label.items()):
        if len(rows) < 2:
            continue
        baseline, final = rows[0], rows[-1]
        if baseline.rss_kib and final.rss_kib:
            growth = (final.rss_kib - baseline.rss_kib) / baseline.rss_kib
            observations.append(f"{label}:rss_growth={growth:.3f}")
            if growth > limit:
                failures.append(f"{label} RSS growth {growth:.1%} exceeds {limit:.0%}")
        if baseline.fd_count and final.fd_count:
            growth = (final.fd_count - baseline.fd_count) / baseline.fd_count
            observations.append(f"{label}:fd_growth={growth:.3f}")
            if growth > limit:
                failures.append(f"{label} FD growth {growth:.1%} exceeds {limit:.0%}")
    if not observations:
        return GateResult("resource_growth", "skip", "fewer than two usable samples per process")
    return GateResult("resource_growth", "fail" if failures else "pass", "; ".join(failures or observations))


def analyze_throughput(cycles: Sequence[CycleResult], *, max_regression: float = 0.10) -> GateResult:
    successful = [cycle for cycle in cycles if cycle.ok and cycle.duration_seconds > 0]
    if len(successful) < 4:
        return GateResult("mock_throughput", "skip", "at least four successful cycles are required")
    quartile = max(len(successful) // 4, 1)
    first = successful[:quartile]
    last = successful[-quartile:]
    first_rate = sum(item.document_count for item in first) / sum(item.duration_seconds for item in first)
    last_rate = sum(item.document_count for item in last) / sum(item.duration_seconds for item in last)
    regression = (first_rate - last_rate) / first_rate if first_rate else 0.0
    detail = f"first={first_rate:.4f} docs/s last={last_rate:.4f} docs/s regression={regression:.3%}"
    return GateResult("mock_throughput", "pass" if regression <= max_regression else "fail", detail)


def write_reports(
    report_dir: Path,
    *,
    configuration: dict[str, Any],
    gates: Sequence[GateResult],
    cycles: Sequence[CycleResult],
    resources: Sequence[ResourceSample],
) -> dict[str, Any]:
    overall_ok = all(gate.ok for gate in gates) and all(cycle.ok for cycle in cycles)
    payload = {
        "schema_version": 1,
        "status": "pass" if overall_ok else "fail",
        "configuration": configuration,
        "gates": [asdict(item) for item in gates],
        "cycles": [asdict(item) | {"ok": item.ok} for item in cycles],
        "resources": [asdict(item) for item in resources],
        "failed_samples": [sample for cycle in cycles for sample in cycle.failed_samples][:100],
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# OcrParser Stability Soak Report",
        "",
        f"- Status: **{payload['status'].upper()}**",
        f"- Version: `{configuration['expected_version']}`",
        f"- Revision: `{configuration['expected_revision']}`",
        f"- Cycles: {len(cycles)}",
        "",
        "## Gates",
        "",
        "| Gate | Status | Detail |",
        "| --- | --- | --- |",
    ]
    for gate in gates:
        lines.append(f"| {gate.name} | {gate.status} | {gate.detail.replace('|', '/')} |")
    lines.extend(["", "## Cycles", "", "| Cycle | Mode | Job | Status | Seconds |", "| ---: | --- | --- | --- | ---: |"])
    for cycle in cycles:
        lines.append(
            f"| {cycle.cycle} | {cycle.input_mode} | {cycle.job_id or '-'} | "
            f"{'pass' if cycle.ok else 'fail'} | {cycle.duration_seconds:.3f} |"
        )
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an auditable OcrParser stability soak.")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--expected-revision", default=EXPECTED_REVISION)
    parser.add_argument("--source-json-url", required=True)
    parser.add_argument("--database-url-env-var", required=True)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--control-token-env-var", required=True)
    parser.add_argument("--model-api-key-env-var")
    parser.add_argument("--runtime-python", default=sys.executable)
    parser.add_argument("--runtime-repo-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--shared-root", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--worker-id", dest="worker_ids", action="append", required=True)
    parser.add_argument("--engine", choices=["dotsocr", "mineru", "paddleocr-vl"], default="dotsocr")
    parser.add_argument("--engine-profile", choices=["mock", "dotsocr", "mineru", "paddleocr-vl"], default="mock")
    parser.add_argument("--ocr-host", default="127.0.0.1")
    parser.add_argument("--ocr-port", type=int, default=18000)
    parser.add_argument("--model-name", default="mock-ocr")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--duration-seconds", type=float, default=86400.0)
    parser.add_argument("--documents-per-cycle", type=int, default=100)
    parser.add_argument(
        "--input-modes",
        default="directory,existing_manifest,distributed_remote_folder_snapshot",
    )
    parser.add_argument("--target-files-per-shard", type=int, default=10)
    parser.add_argument("--max-shard-attempts", type=int, default=3)
    parser.add_argument("--polls", type=int, default=7200)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--claim-stress-shards", type=int, default=200)
    parser.add_argument("--claim-stress-workers", type=int, default=16)
    parser.add_argument("--fault-plan", type=Path)
    parser.add_argument("--fault-timeout", type=float, default=300.0)
    parser.add_argument(
        "--resource-pid-file",
        action="append",
        default=[],
        metavar="LABEL=PATH",
    )
    parser.add_argument("--disable-process-pool", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cycles < 1 or args.documents_per_cycle < 1:
        print("--cycles and --documents-per-cycle must be at least 1", file=sys.stderr)
        return 2
    token = os.getenv(args.control_token_env_var, "")
    database_url = os.getenv(args.database_url_env_var, "")
    if not args.dry_run and not token:
        print(f"environment variable {args.control_token_env_var} is required", file=sys.stderr)
        return 2
    if not args.dry_run and not database_url:
        print(f"environment variable {args.database_url_env_var} is required", file=sys.stderr)
        return 2
    if not args.dry_run and args.model_api_key_env_var and not os.getenv(args.model_api_key_env_var, ""):
        print(f"environment variable {args.model_api_key_env_var} is required", file=sys.stderr)
        return 2
    modes = [item.strip() for item in args.input_modes.split(",") if item.strip()]
    valid_modes = {"directory", "existing_manifest", "distributed_remote_folder_snapshot"}
    if not modes or any(mode not in valid_modes for mode in modes):
        print(f"--input-modes must use only: {', '.join(sorted(valid_modes))}", file=sys.stderr)
        return 2
    try:
        hooks = load_fault_plan(args.fault_plan)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid fault plan: {exc}", file=sys.stderr)
        return 2
    resource_pid_files: list[tuple[str, Path]] = []
    for item in args.resource_pid_file:
        if "=" not in item:
            print("--resource-pid-file must use LABEL=PATH", file=sys.stderr)
            return 2
        label, path = item.split("=", 1)
        resource_pid_files.append((label, Path(path)))

    configuration = {
        "expected_version": args.expected_version,
        "expected_revision": args.expected_revision,
        "engine_profile": args.engine_profile,
        "cycles": args.cycles,
        "duration_seconds": args.duration_seconds,
        "documents_per_cycle": args.documents_per_cycle,
        "worker_count": len(args.worker_ids),
        "input_modes": modes,
        "control_token_env_var": args.control_token_env_var,
        "database_url_env_var": args.database_url_env_var,
        "model_api_key_env_var": args.model_api_key_env_var,
    }
    if args.dry_run:
        print(json.dumps(configuration | {"fault_hooks": [hook.name for hook in hooks]}, indent=2, sort_keys=True))
        return 0

    args.report_dir.mkdir(parents=True, exist_ok=True)
    gates = [
        verify_release_wheel(
            args.wheel,
            expected_version=args.expected_version,
            expected_revision=args.expected_revision,
        ),
        verify_source_offer(
            args.source_json_url,
            expected_version=args.expected_version,
            expected_revision=args.expected_revision,
        ),
    ]
    if not all(gate.ok for gate in gates):
        payload = write_reports(args.report_dir, configuration=configuration, gates=gates, cycles=[], resources=[])
        return 0 if payload["status"] == "pass" else 1

    gates.append(
        run_command_gate(
            "migration_verify",
            [
                args.runtime_python,
                "-m",
                "ocr_platform.control.migrate_cli",
                "verify",
            ],
            cwd=args.runtime_repo_dir,
            env_overrides={"OCR_PLATFORM_DATABASE_URL": database_url},
            secret_values=[token, database_url],
        )
    )
    gates.append(
        run_command_gate(
            "postgres_claim_stress",
            [
                args.runtime_python,
                str(args.runtime_repo_dir / "tools" / "pg_claim_stress.py"),
                "--database-url-env-var",
                args.database_url_env_var,
                "--shards",
                str(args.claim_stress_shards),
                "--workers",
                str(args.claim_stress_workers),
                "--json",
            ],
            cwd=args.runtime_repo_dir,
            secret_values=[token, database_url],
        )
    )
    if not all(gate.ok for gate in gates):
        payload = write_reports(args.report_dir, configuration=configuration, gates=gates, cycles=[], resources=[])
        return 0 if payload["status"] == "pass" else 1

    resources: list[ResourceSample] = []
    cycles: list[CycleResult] = []
    soak_started = time.monotonic()
    for cycle_index in range(1, args.cycles + 1):
        for label, pid_file in resource_pid_files:
            resources.append(collect_resource_sample(label, pid_file))
        mode = modes[(cycle_index - 1) % len(modes)]
        cycles.append(
            execute_cycle(
                cycle=cycle_index,
                mode=mode,
                args=args,
                token=token,
                hooks=hooks,
                report_dir=args.report_dir,
            )
        )
        if cycle_index < args.cycles and args.duration_seconds > 0:
            target = soak_started + (args.duration_seconds * cycle_index / args.cycles)
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    for label, pid_file in resource_pid_files:
        resources.append(collect_resource_sample(label, pid_file))
    gates.append(analyze_resource_growth(resources))
    if args.engine_profile == "mock":
        gates.append(analyze_throughput(cycles))
    payload = write_reports(args.report_dir, configuration=configuration, gates=gates, cycles=cycles, resources=resources)
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
