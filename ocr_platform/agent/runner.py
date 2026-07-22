"""Build and run OCR parser commands for platform jobs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import httpx

from ocr_parser.infra.failure_category import infer_failure_category
from ocr_platform.agent.client import ControlClient
from ocr_platform.agent.config import AgentConfig
from ocr_platform.agent.resources import collect_system_resources, evaluate_resource_pressure
from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import DEFAULT_SCAN_ERROR_SAMPLE_LIMIT, _record_skipped_error
from ocr_platform.manifest.sharder import write_folder_snapshot_streaming
from ocr_platform.manifest.sharder import write_manifest_snapshot_streaming


TERMINATION_TIMEOUT_SECONDS = 5.0
PENDING_TERMINAL_SHARD_STATUSES = {"succeeded", "failed", "stopped"}


def _env_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _with_shard_update_context(
    payload: dict[str, Any],
    shard_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not shard_context:
        return payload
    enriched = dict(payload)
    assigned_server_id = shard_context.get("assigned_server_id")
    attempt_count = shard_context.get("attempt_count")
    if assigned_server_id is not None:
        enriched["assigned_server_id"] = assigned_server_id
    if attempt_count is not None:
        enriched["attempt_count"] = attempt_count
    return enriched


MANIFEST_SCAN_PROGRESS_INTERVAL_FILES = _env_positive_int(
    "OCR_MANIFEST_SCAN_PROGRESS_INTERVAL_FILES",
    10000,
)


def resource_paths(config: AgentConfig) -> list[str]:
    return [config.work_dir, *(config.shared_roots or [])]


def resource_pressure(config: AgentConfig) -> dict[str, object]:
    if not config.resource_guard_enabled:
        return {"constrained": False, "level": "disabled", "reasons": []}
    resources = collect_system_resources(resource_paths(config))
    return evaluate_resource_pressure(
        resources,
        memory_percent_threshold=config.resource_guard_memory_percent,
        min_available_memory_bytes=config.resource_guard_min_available_memory_bytes,
        disk_percent_threshold=config.resource_guard_disk_percent,
        min_free_disk_bytes=config.resource_guard_min_free_disk_bytes,
    )


@dataclass
class ForwardedEventStatus:
    saw_file_failed: bool = False
    saw_job_failed: bool = False
    saw_job_stopped: bool = False
    terminal_files: set[str] = field(default_factory=set)
    failed_files: set[str] = field(default_factory=set)
    skipped_files: set[str] = field(default_factory=set)
    completed_pages: set[tuple[str, int]] = field(default_factory=set)
    api_inflight: int | None = None
    api_inflight_peak: int | None = None
    api_waiting: int | None = None
    oldest_api_inflight: float | None = None
    execution_paused: bool | None = None
    api_concurrency_limit: int | None = None
    execution_control_reason: str | None = None
    failure_category: str | None = None
    error_message: str | None = None

    @property
    def saw_failure(self) -> bool:
        return self.saw_job_failed or self.saw_file_failed

    @property
    def processed_file_count(self) -> int:
        return len(self.terminal_files)

    def shard_progress_payload(self, status: str = "running") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "processed_files": self.processed_file_count,
            "failed_files": len(self.failed_files),
            "skipped_files": len(self.skipped_files),
            "completed_pages": len(self.completed_pages),
        }
        if self.api_inflight is not None:
            payload["api_inflight"] = self.api_inflight
        if self.api_inflight_peak is not None:
            payload["api_inflight_peak"] = self.api_inflight_peak
        if self.api_waiting is not None:
            payload["api_waiting"] = self.api_waiting
        if self.oldest_api_inflight is not None:
            payload["oldest_api_inflight"] = self.oldest_api_inflight
        if self.execution_paused is not None:
            payload["execution_paused"] = self.execution_paused
        if self.api_concurrency_limit is not None:
            payload["api_concurrency_limit"] = self.api_concurrency_limit
        if self.execution_control_reason is not None:
            payload["execution_control_reason"] = self.execution_control_reason
        if status != "running" and self.failure_category:
            payload["failure_category"] = self.failure_category
        if status != "running" and self.error_message:
            payload["error_message"] = self.error_message
        return payload

    def observe(self, record: dict[str, Any]) -> bool:
        event_type = record.get("type")
        payload = record.get("payload") or {}
        changed = False

        if event_type == "page_done":
            file_key = _event_file_key(payload)
            page_no = payload.get("page_no")
            if file_key and page_no is not None:
                try:
                    page_key = (file_key, int(page_no))
                except (TypeError, ValueError):
                    page_key = None
                if page_key is not None and page_key not in self.completed_pages:
                    self.completed_pages.add(page_key)
                    changed = True
        elif event_type in {"file_done", "file_failed"}:
            file_key = _event_file_key(payload)
            if file_key and file_key not in self.terminal_files:
                self.terminal_files.add(file_key)
                changed = True
            if event_type == "file_failed" and file_key and file_key not in self.failed_files:
                self.failed_files.add(file_key)
                changed = True
            if (
                event_type == "file_done"
                and payload.get("status") == "skipped"
                and file_key
                and file_key not in self.skipped_files
            ):
                self.skipped_files.add(file_key)
                changed = True

        if event_type in {"job_failed", "file_failed"}:
            failure_category = payload.get("failure_category")
            error_message = payload.get("error_message") or payload.get("error")
            if failure_category and self.failure_category != str(failure_category):
                self.failure_category = str(failure_category)
            elif error_message and self.failure_category is None:
                self.failure_category = infer_failure_category({"error": error_message})
            if error_message and self.error_message != str(error_message):
                self.error_message = str(error_message)

        runtime_changed = self.observe_runtime(payload)
        execution_control_changed = self.observe_execution_control(payload)
        return runtime_changed or execution_control_changed or changed

    def observe_runtime(self, payload: dict[str, Any]) -> bool:
        runtime = payload.get("runtime")
        if not isinstance(runtime, dict):
            return False

        changed = False
        for attr_name, caster in (
            ("api_inflight", int),
            ("api_inflight_peak", int),
            ("api_waiting", int),
            ("oldest_api_inflight", float),
        ):
            raw_value = runtime.get(attr_name)
            if raw_value is None:
                continue
            try:
                value = caster(raw_value)
            except (TypeError, ValueError):
                continue
            if getattr(self, attr_name) != value:
                setattr(self, attr_name, value)
                changed = True

        return changed

    def observe_execution_control(self, payload: dict[str, Any]) -> bool:
        control = payload.get("execution_control")
        if not isinstance(control, dict):
            return False

        changed = False
        paused = bool(control.get("paused"))
        if self.execution_paused != paused:
            self.execution_paused = paused
            changed = True
        raw_limit = control.get("api_concurrency_limit")
        if raw_limit is not None:
            try:
                limit = max(1, int(raw_limit))
            except (TypeError, ValueError):
                limit = None
            if limit is not None and self.api_concurrency_limit != limit:
                self.api_concurrency_limit = limit
                changed = True
        reason = control.get("reason")
        if reason is not None and self.execution_control_reason != str(reason):
            self.execution_control_reason = str(reason)
            changed = True

        return changed


def _event_file_key(payload: dict[str, Any]) -> str | None:
    file_key = payload.get("file_path") or payload.get("filename")
    return str(file_key) if file_key else None


def _is_transient_control_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _pending_shard_update_path(config: AgentConfig, job_id: str, shard_id: int) -> Path:
    return (
        Path(config.work_dir)
        / "jobs"
        / str(job_id)
        / "pending-shard-updates"
        / f"shard-{shard_id}.json"
    )


@dataclass(frozen=True)
class PendingShardUpdateRef:
    path: Path
    raw_content: str
    generation: str | None = None


def _pending_shard_update_ref(
    path: Path,
    raw_content: str,
    record: dict[str, Any] | None = None,
) -> PendingShardUpdateRef:
    generation = record.get("generation") if isinstance(record, dict) else None
    return PendingShardUpdateRef(
        path=path,
        raw_content=raw_content,
        generation=str(generation) if generation else None,
    )


def _pending_shard_update_matches(ref: PendingShardUpdateRef) -> bool:
    try:
        current_raw = ref.path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    if ref.generation is None:
        return current_raw == ref.raw_content
    try:
        current = json.loads(current_raw)
    except json.JSONDecodeError:
        return False
    return isinstance(current, dict) and current.get("generation") == ref.generation


def _write_pending_shard_update(
    config: AgentConfig,
    *,
    job_id: str,
    shard_id: int,
    payload: dict[str, Any],
) -> PendingShardUpdateRef:
    path = _pending_shard_update_path(config, job_id, shard_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_raw = path.read_text(encoding="utf-8")
        existing_record = json.loads(existing_raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        existing_raw = None
        existing_record = None
    existing_payload = (
        existing_record.get("payload")
        if isinstance(existing_record, dict)
        else None
    )
    existing_status = (
        existing_payload.get("status")
        if isinstance(existing_payload, dict)
        else None
    )
    if (
        existing_raw is not None
        and existing_status in PENDING_TERMINAL_SHARD_STATUSES
        and payload.get("status") not in PENDING_TERMINAL_SHARD_STATUSES
    ):
        return _pending_shard_update_ref(path, existing_raw, existing_record)

    generation = uuid.uuid4().hex
    record = {
        "job_id": str(job_id),
        "shard_id": int(shard_id),
        "server_id": config.server_id,
        "generation": generation,
        "payload": payload,
    }
    raw_content = json.dumps(
        record,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(raw_content, encoding="utf-8")
    tmp_path.replace(path)
    return PendingShardUpdateRef(
        path=path,
        raw_content=raw_content,
        generation=generation,
    )


def _remove_pending_shard_update(
    ref: PendingShardUpdateRef,
) -> bool:
    if not _pending_shard_update_matches(ref):
        return False
    try:
        ref.path.unlink()
    except FileNotFoundError:
        return False
    return True


def _iter_pending_shard_update_paths(config: AgentConfig) -> Iterator[Path]:
    root = Path(config.work_dir) / "jobs"
    if not root.exists():
        return iter(())
    return iter(sorted(root.glob("*/pending-shard-updates/shard-*.json")))


def _quarantine_pending_shard_update(
    ref: PendingShardUpdateRef,
    record: dict[str, Any],
    exc: BaseException,
) -> bool:
    if not _pending_shard_update_matches(ref):
        return False
    failed_path = ref.path.with_suffix(ref.path.suffix + ".failed")
    payload = dict(record)
    payload["replay_error"] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    failed_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    with contextlib.suppress(FileNotFoundError):
        ref.path.unlink()
    return True


def _quarantine_invalid_pending_shard_update(
    path: Path,
    exc: BaseException,
    *,
    raw_content: str | None = None,
) -> None:
    raw_shard_id = path.stem.removeprefix("shard-")
    try:
        shard_id: int | str = int(raw_shard_id)
    except ValueError:
        shard_id = raw_shard_id
    record = {
        "job_id": path.parent.parent.name,
        "shard_id": shard_id,
    }
    if raw_content is not None:
        record["raw_content"] = raw_content
    if raw_content is None:
        try:
            raw_content = path.read_text(encoding="utf-8")
        except OSError:
            return
    _quarantine_pending_shard_update(
        _pending_shard_update_ref(path, raw_content),
        record,
        exc,
    )


async def replay_pending_shard_updates(
    config: AgentConfig,
    client: ControlClient,
    *,
    limit: int | None = None,
) -> int:
    replayed = 0
    paths = list(_iter_pending_shard_update_paths(config))
    replay_limit = len(paths) if limit is None else max(limit, 0)
    for path in paths[:replay_limit]:
        try:
            raw_content = path.read_text(encoding="utf-8")
        except OSError as exc:
            _quarantine_invalid_pending_shard_update(path, exc)
            continue
        try:
            record = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            _quarantine_invalid_pending_shard_update(path, exc, raw_content=raw_content)
            continue
        ref = _pending_shard_update_ref(
            path,
            raw_content,
            record if isinstance(record, dict) else None,
        )
        if not isinstance(record, dict):
            _quarantine_pending_shard_update(
                ref,
                {"job_id": path.parent.parent.name, "shard_id": path.stem},
                ValueError("pending shard update record must be a JSON object"),
            )
            continue
        record_server_id = record.get("server_id")
        if record_server_id and str(record_server_id) != config.server_id:
            continue
        shard_id = record.get("shard_id")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            _quarantine_pending_shard_update(
                ref,
                record,
                ValueError("pending shard update payload must be a JSON object"),
            )
            continue
        try:
            shard_id_int = int(shard_id)
        except (TypeError, ValueError):
            _quarantine_pending_shard_update(
                ref,
                record,
                ValueError("pending shard update shard_id must be an integer"),
            )
            continue
        try:
            await client.update_shard(shard_id_int, payload)
        except Exception as exc:
            if _is_transient_control_error(exc):
                break
            _quarantine_pending_shard_update(ref, record, exc)
            continue
        _remove_pending_shard_update(ref)
        replayed += 1
    return replayed


async def _update_shard_with_transient_retry(
    client: ControlClient,
    shard_id: int,
    payload: dict[str, Any],
    config: AgentConfig,
    *,
    job_id: str | None = None,
) -> dict[str, Any]:
    pending_ref: PendingShardUpdateRef | None = None
    pending_record: dict[str, Any] | None = None
    if job_id is not None:
        pending_record = {
            "job_id": str(job_id),
            "shard_id": int(shard_id),
            "server_id": config.server_id,
            "payload": payload,
        }
        pending_ref = _write_pending_shard_update(
            config,
            job_id=job_id,
            shard_id=shard_id,
            payload=payload,
        )
    delay = max(float(config.control_retry_initial_seconds), 0.1)
    max_delay = max(float(config.control_retry_max_seconds), delay)
    while True:
        try:
            response = await client.update_shard(shard_id, payload)
            if pending_ref is not None:
                _remove_pending_shard_update(pending_ref)
            return response
        except Exception as exc:
            if not _is_transient_control_error(exc):
                if pending_ref is not None and pending_record is not None:
                    _quarantine_pending_shard_update(pending_ref, pending_record, exc)
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


def _append_option(command: list[str], flag: str, value: Any) -> None:
    if value is None or value == "":
        return
    command.extend([flag, str(value)])


def _append_bool_flag(command: list[str], flag: str, enabled: bool) -> None:
    if enabled:
        command.append(flag)


def _ocr_subprocess_env(job: dict[str, Any]) -> dict[str, str] | None:
    api_key = (job.get("extra_args") or {}).get("api_key")
    env = dict(os.environ)
    if api_key:
        env["API_KEY"] = str(api_key)
    else:
        env.pop("API_KEY", None)
    return env


def _job_api_concurrency_baseline(job: dict[str, Any]) -> int:
    extra_args = job.get("extra_args") or {}
    for source, name in (
        (extra_args, "api_concurrency_start"),
        (extra_args, "api_concurrency_max"),
        (extra_args, "api_concurrency"),
        (job, "page_concurrency"),
    ):
        try:
            value = int(source.get(name) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 1


def resource_execution_control_payload(
    job: dict[str, Any],
    pressure: dict[str, Any],
) -> dict[str, Any]:
    if pressure.get("constrained"):
        reasons = pressure.get("reasons")
        reason = (
            str(reasons[0])
            if isinstance(reasons, list) and reasons
            else str(pressure.get("level") or "resource_pressure")
        )
        return {
            "paused": True,
            "api_concurrency_limit": 1,
            "reason": reason,
        }
    return {
        "paused": False,
        "api_concurrency_limit": _job_api_concurrency_baseline(job),
        "reason": "ready",
    }


def _initial_execution_control_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "paused": False,
        "api_concurrency_limit": _job_api_concurrency_baseline(job),
        "reason": "initial",
    }


def _execution_control_path(config: AgentConfig, job_id: str) -> Path:
    return Path(config.work_dir) / "jobs" / str(job_id) / "execution-control.json"


def _write_execution_control(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp_path.replace(path)


async def resource_execution_control_watcher(
    job: dict[str, Any],
    config: AgentConfig,
    control_path: Path,
    process: asyncio.subprocess.Process,
    *,
    client: ControlClient | None = None,
) -> None:
    last_payload: dict[str, Any] | None = None
    job_id = str(job.get("id") or "")
    shard = job.get("shard") or {}
    shard_id = shard.get("id")
    while process.returncode is None:
        payload = resource_execution_control_payload(job, resource_pressure(config))
        if payload != last_payload:
            _write_execution_control(control_path, payload)
            if client is not None and shard_id is not None:
                shard_payload = _with_shard_update_context(
                    {
                        "status": "running",
                        "execution_paused": bool(payload.get("paused")),
                        "api_concurrency_limit": payload.get("api_concurrency_limit"),
                        "execution_control_reason": payload.get("reason"),
                    },
                    shard,
                )
                try:
                    shard_id_int = int(shard_id)
                    await client.update_shard(shard_id_int, shard_payload)
                except Exception as exc:
                    if job_id and _is_transient_control_error(exc):
                        with contextlib.suppress(TypeError, ValueError):
                            _write_pending_shard_update(
                                config,
                                job_id=job_id,
                                shard_id=int(shard_id),
                                payload=shard_payload,
                            )
                    pass
            last_payload = dict(payload)
        remaining = max(float(config.poll_interval_seconds), 0.1)
        while process.returncode is None and remaining > 0:
            sleep_for = min(0.5, remaining)
            await asyncio.sleep(sleep_for)
            remaining -= sleep_for


def build_ocr_command(job: dict[str, Any], config: AgentConfig) -> tuple[list[str], Path]:
    job_id = str(job["id"])
    job_dir = Path(config.work_dir) / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    execution_control_file = _execution_control_path(config, job_id)

    shard = job.get("shard") or {}
    if shard.get("id") is not None:
        attempt_count = int(shard.get("attempt_count") or 0)
        event_file = job_dir / "events" / f"shard-{int(shard['id'])}-attempt-{attempt_count}-events.jsonl"
    else:
        event_file = job_dir / "events.jsonl"
    event_file.parent.mkdir(parents=True, exist_ok=True)
    event_file.write_text("", encoding="utf-8")

    is_manifest_shard = bool(shard.get("shard_path"))
    command = [config.python_executable, "-m", "ocr_parser"]
    if is_manifest_shard:
        _append_option(command, "--input_manifest", shard.get("shard_path"))
        _append_option(command, "--input_root", job.get("input_dir"))
    else:
        _append_option(command, "--input_dir", job.get("input_dir"))
    _append_option(command, "--output_dir", job.get("output_dir"))
    _append_option(command, "--engine", job.get("engine"))
    _append_option(command, "--engine_config", job.get("engine_config"))
    _append_option(command, "--ip", job.get("ip"))
    _append_option(command, "--port", job.get("port"))
    _append_option(command, "--model_name", job.get("model_name"))
    _append_option(command, "--page_concurrency", job.get("page_concurrency"))
    _append_option(command, "--job_id", job_id)
    _append_option(command, "--job_event_file", event_file)
    if config.resource_guard_enabled:
        _write_execution_control(execution_control_file, _initial_execution_control_payload(job))
        _append_option(command, "--execution_control_file", execution_control_file)
        _append_option(
            command,
            "--execution_control_poll_interval_seconds",
            min(max(config.poll_interval_seconds, 0.5), 5.0),
        )
    _append_bool_flag(command, "--force_reprocess", bool(job.get("force_reprocess")))

    for name, value in sorted((job.get("extra_args") or {}).items()):
        if _is_secret_extra_arg(name):
            continue
        if is_manifest_shard and name == "disable_resume":
            continue
        flag = f"--{name}"
        if isinstance(value, bool):
            _append_bool_flag(command, flag, value)
        else:
            _append_option(command, flag, value)

    command_file = job_dir / "command.json"
    command_file.write_text(
        json.dumps(command, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return command, event_file


def _is_secret_extra_arg(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    if normalized in {"api_key", "api_key_env_var", "authorization", "password"}:
        return True
    return normalized.endswith(("_token", "_secret", "_password"))


async def read_new_jsonl_events(
    path: Path, offset: int
) -> tuple[int, list[dict[str, Any]]]:
    def read_events() -> tuple[int, list[dict[str, Any]]]:
        if not path.exists():
            return offset, []

        records: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            handle.seek(offset)
            payload = handle.read()

        if not payload:
            return offset, []

        if payload.endswith(b"\n"):
            complete_payload = payload
        else:
            last_newline = payload.rfind(b"\n")
            if last_newline == -1:
                return offset, []
            complete_payload = payload[: last_newline + 1]

        next_offset = offset + len(complete_payload)
        for line in complete_payload.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        return next_offset, records

    return await asyncio.to_thread(read_events)


async def should_stop_job(job_id: str, client: ControlClient) -> bool:
    job = await client.get_job(job_id)
    return bool(job.get("stop_requested")) or job.get("status") == "stopping"


async def _should_stop_job_with_transient_retry(
    job_id: str,
    client: ControlClient,
    config: AgentConfig,
) -> bool:
    delay = max(float(config.control_retry_initial_seconds), 0.1)
    max_delay = max(float(config.control_retry_max_seconds), delay)
    while True:
        try:
            return await should_stop_job(job_id, client)
        except Exception as exc:
            if not _is_transient_control_error(exc):
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def _forward_stream(
    stream: asyncio.StreamReader,
    stream_name: str,
    job_id: str,
    client: ControlClient,
) -> None:
    buffered = b""
    max_line_bytes = 64 * 1024

    async def post_log_best_effort(line: str) -> None:
        try:
            await client.post_log(job_id, stream_name, line)
        except Exception as exc:
            if _is_transient_control_error(exc):
                return
            raise

    async def post_line(line: bytes) -> None:
        await post_log_best_effort(line.decode("utf-8", errors="replace").rstrip("\r\n"))

    while True:
        try:
            chunk = await stream.read(8192)
        except (ValueError, asyncio.LimitOverrunError) as exc:
            await post_log_best_effort(f"[log forwarding skipped oversized line: {exc}]")
            break
        if not chunk:
            if buffered:
                await post_line(buffered)
            break

        buffered += chunk
        while True:
            newline_index = buffered.find(b"\n")
            if newline_index == -1:
                break
            line = buffered[: newline_index + 1]
            buffered = buffered[newline_index + 1 :]
            await post_line(line)

        if len(buffered) > max_line_bytes:
            await post_log_best_effort(
                buffered[:max_line_bytes]
                .decode("utf-8", errors="replace")
                .rstrip("\r\n")
                + " [truncated oversized log line]",
            )
            buffered = b""


async def _forward_events_until_done(
    event_file: Path,
    job_id: str,
    client: ControlClient,
    process: asyncio.subprocess.Process,
    shard_id: int | None = None,
    shard_update_context: dict[str, Any] | None = None,
    config: AgentConfig | None = None,
) -> ForwardedEventStatus:
    offset = 0
    status = ForwardedEventStatus()

    async def forward_records(records: list[dict[str, Any]]) -> None:
        shard_progress_changed = False
        for record in records:
            event_type = record.get("type")
            if event_type == "file_failed":
                status.saw_file_failed = True
            elif event_type == "job_failed":
                status.saw_job_failed = True
            elif event_type == "job_stopped":
                status.saw_job_stopped = True
            if shard_id is not None:
                shard_progress_changed = status.observe(record) or shard_progress_changed
            await client.post_event(job_id, record)
        if shard_id is not None and shard_progress_changed:
            payload = _with_shard_update_context(
                status.shard_progress_payload("running"),
                shard_update_context,
            )
            try:
                await client.update_shard(shard_id, payload)
            except Exception as exc:
                if not _is_transient_control_error(exc):
                    raise
                if config is not None:
                    _write_pending_shard_update(
                        config,
                        job_id=job_id,
                        shard_id=shard_id,
                        payload=payload,
                    )

    while True:
        offset, records = await read_new_jsonl_events(event_file, offset)
        await forward_records(records)

        if process.returncode is not None:
            offset, records = await read_new_jsonl_events(event_file, offset)
            await forward_records(records)
            break

        await asyncio.sleep(1.0)
    return status


async def _terminate_process(
    process: asyncio.subprocess.Process,
    timeout_seconds: float | None = None,
) -> None:
    if process.returncode is not None:
        return
    timeout = (
        TERMINATION_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(timeout_seconds, 0.0)
    )

    def signal_group(sig: int) -> bool:
        if hasattr(os, "killpg") and getattr(process, "pid", None):
            try:
                os.killpg(process.pid, sig)
                return True
            except ProcessLookupError:
                return True
            except OSError:
                pass
        return False

    if not signal_group(signal.SIGTERM):
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        if not signal_group(signal.SIGKILL):
            process.kill()
        await process.wait()


async def _stop_watcher(
    job_id: str,
    client: ControlClient,
    process: asyncio.subprocess.Process,
    poll_interval_seconds: float = 2.0,
    termination_timeout_seconds: float | None = None,
) -> bool:
    poll_interval = max(poll_interval_seconds, 0.1)
    while process.returncode is None:
        try:
            should_stop = await should_stop_job(job_id, client)
        except Exception:
            await asyncio.sleep(poll_interval)
            continue

        if should_stop:
            await client.post_event(
                job_id,
                {
                    "type": "job_stopping",
                    "payload": {},
                },
            )
            await _terminate_process(process, termination_timeout_seconds)
            return True
        await asyncio.sleep(poll_interval)
    return False


async def _cancel_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _post_job_failed(
    client: ControlClient,
    job_id: str,
    payload: dict[str, Any],
) -> None:
    try:
        await client.post_event(
            job_id,
            {
                "type": "job_failed",
                "payload": payload,
            },
        )
    except Exception:
        pass


def failure_payload_for_return_code(return_code: int) -> dict[str, Any]:
    signal_number = None
    if return_code < 0:
        signal_number = abs(return_code)
    elif return_code in {128 + signal.SIGKILL, 128 + signal.SIGTERM}:
        signal_number = return_code - 128
    if signal_number is not None:
        return {
            "return_code": return_code,
            "failure_category": "process_killed",
            "error_message": f"process killed by signal {signal_number}",
        }
    return {
        "return_code": return_code,
        "failure_category": "process_failed",
        "error_message": f"process exited with code {return_code}",
    }


def _remote_manifest_output_dir(job: dict[str, Any]) -> Path:
    job_id = str(job["id"])
    manifest_root = job.get("manifest_root")
    if manifest_root:
        return Path(str(manifest_root)) / job_id
    return Path(str(job["output_dir"])) / "_manifest" / job_id


def _scan_unit_output_dir(job: dict[str, Any], unit: dict[str, Any]) -> Path:
    return _remote_manifest_output_dir(job) / "scan-units" / str(unit["id"])


def _iter_direct_directory_items(
    input_root: str | Path,
    scan_path: str | Path,
    child_paths: list[str],
    *,
    skipped_errors: list[dict[str, str]] | None = None,
    stats: dict[str, int] | None = None,
    max_skipped_error_samples: int = DEFAULT_SCAN_ERROR_SAMPLE_LIMIT,
) -> Iterator[ManifestItem]:
    root = Path(input_root).resolve()
    current = Path(scan_path).resolve()
    if not current.exists():
        raise FileNotFoundError(f"scan unit path not found: {current}")
    if not current.is_dir():
        raise NotADirectoryError(f"scan unit path is not a directory: {current}")

    with os.scandir(current) as entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_paths.append(str(Path(entry.path).resolve()))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as exc:
                _record_skipped_error(
                    skipped_errors,
                    stats,
                    entry.path,
                    exc,
                    max_samples=max_skipped_error_samples,
                )
                continue
            if not entry.name.lower().endswith(".pdf"):
                continue
            try:
                path = Path(entry.path).resolve()
                stat = entry.stat(follow_symlinks=False)
                yield ManifestItem(
                    input_path=str(path),
                    relative_path=path.relative_to(root).as_posix(),
                    size_bytes=int(stat.st_size),
                    mtime_ns=int(stat.st_mtime_ns),
                )
            except OSError as exc:
                _record_skipped_error(
                    skipped_errors,
                    stats,
                    Path(entry.path).resolve(),
                    exc,
                    max_samples=max_skipped_error_samples,
                )


def _write_scan_unit_manifest_streaming(
    *,
    job: dict[str, Any],
    unit: dict[str, Any],
    child_paths: list[str],
    progress_callback=None,
):
    skipped_errors: list[dict[str, str]] = []
    scan_stats = {"scanned_dirs": 1, "skipped_error_count": 0}
    return write_manifest_snapshot_streaming(
        job_id=str(job["id"]),
        input_root=str(Path(job["input_dir"]).resolve()),
        output_dir=_scan_unit_output_dir(job, unit),
        items=_iter_direct_directory_items(
            job["input_dir"],
            unit["path"],
            child_paths,
            skipped_errors=skipped_errors,
            stats=scan_stats,
        ),
        target_files_per_shard=int(job.get("target_files_per_shard") or 1000),
        input_mode="distributed_remote_folder_snapshot",
        skipped_errors=skipped_errors,
        skipped_error_count=lambda: scan_stats["skipped_error_count"],
        scanned_dir_count=lambda: scan_stats["scanned_dirs"],
        progress_interval_files=MANIFEST_SCAN_PROGRESS_INTERVAL_FILES,
        progress_callback=progress_callback,
        progress_context=lambda: {
            "child_dir_count": len(child_paths),
        },
    )


async def run_scan_unit(
    unit: dict[str, Any],
    config: AgentConfig,
    client: ControlClient,
) -> int:
    job = await client.get_job(str(unit["job_id"]))
    child_paths: list[str] = []
    loop = asyncio.get_running_loop()
    progress_futures: list[Any] = []

    def post_scan_unit_progress(payload: dict[str, object]) -> None:
        event_payload = {
            **payload,
            "server_id": config.server_id,
            "input_dir": job["input_dir"],
            "scan_unit_id": unit.get("id"),
            "scan_unit_path": unit.get("path"),
        }
        future = asyncio.run_coroutine_threadsafe(
            client.post_event(
                str(unit["job_id"]),
                {
                    "type": "manifest_scan_progress",
                    "payload": event_payload,
                },
            ),
            loop,
        )
        progress_futures.append(future)

    try:
        written = await asyncio.to_thread(
            _write_scan_unit_manifest_streaming,
            job=job,
            unit=unit,
            child_paths=child_paths,
            progress_callback=post_scan_unit_progress,
        )
        if progress_futures:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in progress_futures),
                return_exceptions=True,
            )
        child_paths.sort()
        await client.complete_scan_unit(
            int(unit["id"]),
            {
                "assigned_server_id": config.server_id,
                "attempt_count": unit.get("attempt_count"),
                "manifest_path": str(written.manifest_path),
                "meta_path": str(written.meta_path),
                "file_count": written.file_count,
                "total_bytes": written.total_bytes,
                "child_paths": child_paths,
                "shards": [
                    {
                        "shard_index": shard.index,
                        "shard_path": str(shard.path),
                        "file_count": shard.file_count,
                    }
                    for shard in written.shards
                ],
            },
        )
    except Exception as exc:
        error_message = str(exc)
        failure_category = infer_failure_category({"error": error_message})
        try:
            await client.fail_scan_unit(
                int(unit["id"]),
                error_message,
                assigned_server_id=config.server_id,
                attempt_count=unit.get("attempt_count"),
                failure_category=failure_category,
            )
        except Exception:
            pass
        await client.post_event(
            str(unit["job_id"]),
            {
                "type": "manifest_scan_failed",
                "payload": {
                    "scan_unit_id": unit.get("id"),
                    "server_id": config.server_id,
                    "error": error_message,
                    "failure_category": failure_category,
                },
            },
        )
        return 1
    return 0


async def run_remote_folder_snapshot_job(
    job: dict[str, Any],
    config: AgentConfig,
    client: ControlClient,
) -> int:
    job_id = str(job["id"])
    loop = asyncio.get_running_loop()
    progress_futures: list[Any] = []

    def post_scan_progress(payload: dict[str, object]) -> None:
        event_payload = {
            **payload,
            "server_id": config.server_id,
            "input_dir": job["input_dir"],
        }
        future = asyncio.run_coroutine_threadsafe(
            client.post_event(
                job_id,
                {
                    "type": "manifest_scan_progress",
                    "payload": event_payload,
                },
            ),
            loop,
        )
        progress_futures.append(future)

    try:
        written = await asyncio.to_thread(
            write_folder_snapshot_streaming,
            job_id=job_id,
            input_root=job["input_dir"],
            output_dir=_remote_manifest_output_dir(job),
            target_files_per_shard=int(job.get("target_files_per_shard") or 1000),
            input_mode="remote_folder_snapshot",
            progress_interval_files=MANIFEST_SCAN_PROGRESS_INTERVAL_FILES,
            progress_callback=post_scan_progress,
        )
        if progress_futures:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in progress_futures),
                return_exceptions=True,
            )
        await client.register_manifest(
            job_id,
            {
                "input_mode": "remote_folder_snapshot",
                "input_root": str(Path(job["input_dir"]).resolve()),
                "manifest_path": str(written.manifest_path),
                "meta_path": str(written.meta_path),
                "file_count": written.file_count,
                "total_bytes": written.total_bytes,
                "shards": [
                    {
                        "shard_index": shard.index,
                        "shard_path": str(shard.path),
                        "file_count": shard.file_count,
                    }
                    for shard in written.shards
                ],
            },
        )
    except Exception as exc:
        await _post_job_failed(
            client,
            job_id,
            {
                "failure_category": "manifest_scan_failed",
                "error_message": str(exc),
            },
        )
        return 1

    job["has_static_shards"] = True
    return await run_static_sharded_job(job, config, client)


def _summary_has_active_shards(summary: dict[str, Any]) -> bool:
    return bool(
        (summary.get("pending_shards") or 0)
        + (summary.get("running_shards") or 0)
        + (summary.get("retrying_shards") or 0)
        + (summary.get("stale_shards") or 0)
    )


def _summary_has_failed_shards(summary: dict[str, Any]) -> bool:
    return bool((summary.get("failed_shards") or 0) + (summary.get("stopped_shards") or 0))


async def run_job(
    job: dict[str, Any],
    config: AgentConfig,
    client: ControlClient,
) -> int:
    if job.get("has_static_shards"):
        return await run_static_sharded_job(job, config, client)
    if job.get("input_mode") == "remote_folder_snapshot":
        return await run_remote_folder_snapshot_job(job, config, client)

    job_id = str(job["id"])
    try:
        command, event_file = build_ocr_command(job, config)
        env = _ocr_subprocess_env(job)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:
        await _post_job_failed(
            client,
            job_id,
            {
                "failure_category": "runner_start_failed",
                "error_message": str(exc),
            },
        )
        return 1

    tasks: list[asyncio.Task[Any]] = []
    if config.resource_guard_enabled:
        tasks.append(
            asyncio.create_task(
                resource_execution_control_watcher(
                    job,
                    config,
                    _execution_control_path(config, job_id),
                    process,
                    client=client,
                )
            )
        )
    if process.stdout is not None:
        tasks.append(
            asyncio.create_task(
                _forward_stream(process.stdout, "stdout", job_id, client)
            )
        )
    if process.stderr is not None:
        tasks.append(
            asyncio.create_task(
                _forward_stream(process.stderr, "stderr", job_id, client)
            )
        )
    tasks.append(
        asyncio.create_task(
            _forward_events_until_done(
                event_file,
                job_id,
                client,
                process,
                shard_id=(job.get("shard") or {}).get("id"),
                shard_update_context=job.get("shard"),
                config=config,
            )
        )
    )
    event_task = tasks[-1]
    stop_task = asyncio.create_task(
        _stop_watcher(
            job_id,
            client,
            process,
            poll_interval_seconds=config.stop_poll_interval_seconds,
            termination_timeout_seconds=config.process_termination_timeout_seconds,
        )
    )
    tasks.append(stop_task)
    wait_task = asyncio.create_task(process.wait())
    all_tasks = tasks + [wait_task]

    try:
        done, pending = await asyncio.wait(
            all_tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            exception = task.exception()
            if exception is not None:
                await _terminate_process(
                    process,
                    config.process_termination_timeout_seconds,
                )
                await _cancel_tasks(list(pending))
                raise exception

        return_code = wait_task.result()
        forwarded_status = event_task.result()
        stopped = stop_task.done() and stop_task.result()
        if forwarded_status.saw_job_stopped:
            stopped = True
        if not stopped:
            try:
                stopped = await should_stop_job(job_id, client)
            except Exception:
                stopped = False
        effective_return_code = return_code
        if forwarded_status.saw_job_stopped and effective_return_code == 0:
            effective_return_code = -signal.SIGTERM
        elif forwarded_status.saw_failure and effective_return_code == 0:
            effective_return_code = 1
        if (job.get("shard") or {}).get("id") is not None:
            progress_status = "running"
            if stopped:
                progress_status = "stopped"
            elif effective_return_code != 0:
                progress_status = "failed"
            job["_shard_progress"] = forwarded_status.shard_progress_payload(progress_status)
        if stopped:
            if not forwarded_status.saw_job_stopped:
                await client.post_event(
                    job_id,
                    {
                        "type": "job_stopped",
                        "payload": {"return_code": effective_return_code},
                    },
                )
        elif effective_return_code != 0 and not forwarded_status.saw_job_failed:
            await _post_job_failed(
                client,
                job_id,
                failure_payload_for_return_code(effective_return_code),
            )
        return effective_return_code
    except asyncio.CancelledError:
        await _terminate_process(process, config.process_termination_timeout_seconds)
        await _cancel_tasks(all_tasks)
        raise
    except Exception:
        await _terminate_process(process, config.process_termination_timeout_seconds)
        await _cancel_tasks(all_tasks)
        raise


async def run_static_sharded_job(
    job: dict[str, Any],
    config: AgentConfig,
    client: ControlClient,
) -> int:
    final_code = 0
    final_failure_category: str | None = None
    final_error_message: str | None = None
    job_id = str(job["id"])
    while True:
        if await should_stop_job(job_id, client):
            await client.post_event(
                job_id,
                {
                    "type": "job_stopped",
                    "payload": {"static_shards_final": True},
                },
            )
            return final_code or 1

        pressure = resource_pressure(config)
        if pressure.get("constrained"):
            await client.post_event(
                job_id,
                {
                    "type": "resource_pressure",
                    "payload": {
                        "stage": "before_shard_claim",
                        "server_id": config.server_id,
                        "pressure": pressure,
                    },
                },
            )
            await asyncio.sleep(config.poll_interval_seconds)
            continue

        shard = await client.claim_shard(job_id, config.server_id)
        if shard is None:
            if await should_stop_job(job_id, client):
                await client.post_event(
                    job_id,
                    {
                        "type": "job_stopped",
                        "payload": {"static_shards_final": True},
                    },
                )
                return final_code or 1
            summary = await client.get_job_summary(job_id)
            if _summary_has_active_shards(summary):
                return final_code
            has_failed_shards = _summary_has_failed_shards(summary)
            if final_code == 0 and not has_failed_shards:
                await client.post_event(
                    job_id,
                    {
                        "type": "job_done",
                        "payload": {"static_shards_final": True},
                    },
                )
            else:
                if final_failure_category is None:
                    final_payload = (
                        failure_payload_for_return_code(final_code)
                        if final_code
                        else {
                            "failure_category": "shard_failed",
                            "error_message": "one or more shards failed",
                        }
                    )
                    final_failure_category = str(final_payload["failure_category"])
                    final_error_message = str(final_payload["error_message"])
                await client.post_event(
                    job_id,
                    {
                        "type": "job_failed",
                        "payload": {
                            "static_shards_final": True,
                            "return_code": final_code,
                            "failure_category": final_failure_category,
                            "error_message": final_error_message,
                        },
                    },
                )
            return final_code

        shard_job = dict(job)
        shard_job["shard"] = shard
        shard_job["has_static_shards"] = False
        if shard_job.get("input_mode") == "remote_folder_snapshot":
            shard_job["input_mode"] = "folder_snapshot"
        return_code = await run_job(shard_job, config, client)
        shard_progress = shard_job.get("_shard_progress") or {}
        stopped = await _should_stop_job_with_transient_retry(
            job_id,
            client,
            config,
        )
        if stopped:
            await _update_shard_with_transient_retry(
                client,
                shard["id"],
                _with_shard_update_context(
                    {
                        "status": "stopped",
                        "processed_files": shard_progress.get("processed_files", 0),
                        "failed_files": shard_progress.get("failed_files", 0),
                        "skipped_files": shard_progress.get("skipped_files", 0),
                        "completed_pages": shard_progress.get("completed_pages", 0),
                        "failure_category": "operator_stopped",
                    },
                    shard,
                ),
                config,
                job_id=job_id,
            )
            await client.post_event(
                job_id,
                {
                    "type": "job_stopped",
                    "payload": {
                        "static_shards_final": True,
                        "return_code": return_code,
                    },
                },
            )
            return return_code or 1
        elif return_code == 0:
            await _update_shard_with_transient_retry(
                client,
                shard["id"],
                _with_shard_update_context(
                    {
                        "status": "succeeded",
                        "processed_files": max(
                            int(shard_progress.get("processed_files", 0)),
                            int(shard["file_count"]),
                        ),
                        "failed_files": shard_progress.get("failed_files", 0),
                        "skipped_files": shard_progress.get("skipped_files", 0),
                        "completed_pages": shard_progress.get("completed_pages", 0),
                    },
                    shard,
                ),
                config,
                job_id=job_id,
            )
        else:
            final_code = return_code
            if shard_progress.get("failure_category"):
                final_failure_category = str(shard_progress["failure_category"])
            elif return_code:
                final_failure_category = str(failure_payload_for_return_code(return_code)["failure_category"])
            else:
                final_failure_category = "shard_failed"
            if shard_progress.get("error_message"):
                final_error_message = str(shard_progress["error_message"])
            elif return_code:
                final_error_message = str(failure_payload_for_return_code(return_code)["error_message"])
            else:
                final_error_message = "one or more shards failed"
            await _update_shard_with_transient_retry(
                client,
                shard["id"],
                _with_shard_update_context(
                    {
                        "status": "failed",
                        "processed_files": shard_progress.get("processed_files", 0),
                        "failed_files": shard_progress.get("failed_files", 0),
                        "skipped_files": shard_progress.get("skipped_files", 0),
                        "completed_pages": shard_progress.get("completed_pages", 0),
                        "failure_category": final_failure_category,
                        "error_message": final_error_message,
                    },
                    shard,
                ),
                config,
                job_id=job_id,
            )
