"""Async client for the OCR platform control API."""

from __future__ import annotations

import json
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


DEFAULT_EVENT_SPOOL_MAX_BYTES = 256 * 1024**2
JSONL_SPOOL_TRIM_TARGET_RATIO = 0.8


def _read_dropped_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(int(payload.get("dropped") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _increment_dropped_count(path: Path, *, count: int, max_pending_bytes: int) -> None:
    if count <= 0:
        return
    existing = _read_dropped_count(path)
    payload = {
        "dropped": existing + count,
        "last_drop_at": datetime.now(timezone.utc).isoformat(),
        "last_drop_reason": "pending_spool_max_bytes_exceeded",
        "max_pending_bytes": max_pending_bytes,
    }
    temp_path = path.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


def _enforce_jsonl_max_bytes(
    path: Path,
    *,
    dropped_path: Path,
    max_pending_bytes: int | None,
) -> None:
    if max_pending_bytes is None or max_pending_bytes <= 0 or not path.exists():
        return
    if path.stat().st_size <= max_pending_bytes:
        return
    lines = path.read_bytes().splitlines(keepends=True)
    target_bytes = int(max_pending_bytes * JSONL_SPOOL_TRIM_TARGET_RATIO)
    target_bytes = max(target_bytes, 0)
    kept_reversed: list[bytes] = []
    kept_bytes = 0
    for line in reversed(lines):
        line_size = len(line)
        if kept_bytes + line_size > target_bytes:
            continue
        kept_reversed.append(line)
        kept_bytes += line_size
    kept = list(reversed(kept_reversed))
    dropped_count = len(lines) - len(kept)
    temp_path = path.with_suffix(".jsonl.tmp")
    with temp_path.open("wb") as handle:
        for line in kept:
            handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)
    _increment_dropped_count(
        dropped_path,
        count=dropped_count,
        max_pending_bytes=max_pending_bytes,
    )


class EventSpool:
    def __init__(self, spool_dir: str | Path, *, max_pending_bytes: int | None = None) -> None:
        self.spool_dir = Path(spool_dir)
        self.path = self.spool_dir / "events.jsonl"
        self.failed_path = self.spool_dir / "events.failed.jsonl"
        self.dropped_path = self.spool_dir / "events.dropped.json"
        self.max_pending_bytes = max_pending_bytes

    def append(self, *, server_id: str, job_id: str, event: dict[str, Any]) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": str(uuid.uuid4()),
            "server_id": server_id,
            "job_id": job_id,
            "event": event,
            "spooled_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _enforce_jsonl_max_bytes(
            self.path,
            dropped_path=self.dropped_path,
            max_pending_bytes=self.max_pending_bytes,
        )

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    records.append(
                        {
                            "id": f"malformed-spool-line-{line_number}",
                            "server_id": "",
                            "job_id": "",
                            "event": None,
                            "raw_line": line.rstrip("\n"),
                            "spool_parse_error": str(exc),
                        }
                    )
                    continue
                if isinstance(record, dict):
                    records.append(record)
                    continue
                records.append(
                    {
                        "id": f"invalid-spool-line-{line_number}",
                        "server_id": "",
                        "job_id": "",
                        "event": None,
                        "raw_line": line.rstrip("\n"),
                        "spool_parse_error": "spool record must be a JSON object",
                    }
                )
        return records

    def replace_records(self, records: list[dict[str, Any]]) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".jsonl.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(self.path)

    def quarantine(self, record: dict[str, Any], exc: httpx.HTTPStatusError) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        quarantined = dict(record)
        quarantined["replay_error"] = {
            "status_code": exc.response.status_code,
            "message": str(exc),
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.failed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(quarantined, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def quarantine_invalid(self, record: dict[str, Any], message: str) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        quarantined = dict(record)
        quarantined["replay_error"] = {
            "error_type": "spool_parse_error",
            "message": message,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.failed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(quarantined, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class LogSpool:
    def __init__(self, spool_dir: str | Path, *, max_pending_bytes: int | None = None) -> None:
        self.spool_dir = Path(spool_dir)
        self.path = self.spool_dir / "logs.jsonl"
        self.failed_path = self.spool_dir / "logs.failed.jsonl"
        self.dropped_path = self.spool_dir / "logs.dropped.json"
        self.max_pending_bytes = max_pending_bytes

    def append(self, *, server_id: str, job_id: str, stream: str, line: str) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "id": str(uuid.uuid4()),
            "server_id": server_id,
            "job_id": job_id,
            "log": {
                "server_id": server_id,
                "stream": stream,
                "line": line,
            },
            "spooled_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _enforce_jsonl_max_bytes(
            self.path,
            dropped_path=self.dropped_path,
            max_pending_bytes=self.max_pending_bytes,
        )

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    records.append(
                        {
                            "id": f"malformed-log-spool-line-{line_number}",
                            "server_id": "",
                            "job_id": "",
                            "log": None,
                            "raw_line": line.rstrip("\n"),
                            "spool_parse_error": str(exc),
                        }
                    )
                    continue
                if isinstance(record, dict):
                    records.append(record)
                    continue
                records.append(
                    {
                        "id": f"invalid-log-spool-line-{line_number}",
                        "server_id": "",
                        "job_id": "",
                        "log": None,
                        "raw_line": line.rstrip("\n"),
                        "spool_parse_error": "spool record must be a JSON object",
                    }
                )
        return records

    def replace_records(self, records: list[dict[str, Any]]) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".jsonl.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(self.path)

    def quarantine(self, record: dict[str, Any], exc: httpx.HTTPStatusError) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        quarantined = dict(record)
        quarantined["replay_error"] = {
            "status_code": exc.response.status_code,
            "message": str(exc),
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.failed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(quarantined, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def quarantine_invalid(self, record: dict[str, Any], message: str) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        quarantined = dict(record)
        quarantined["replay_error"] = {
            "error_type": "spool_parse_error",
            "message": message,
            "quarantined_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.failed_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(quarantined, ensure_ascii=False, default=str))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


class ControlClient:
    def __init__(
        self,
        control_url: str,
        server_id: str,
        timeout: float = 30.0,
        api_token: str | None = None,
        event_spool_dir: str | Path | None = None,
        event_spool_max_bytes: int | None = DEFAULT_EVENT_SPOOL_MAX_BYTES,
    ) -> None:
        self.control_url = control_url.rstrip("/")
        self.server_id = server_id
        token = api_token or os.environ.get("OCR_CONTROL_API_TOKEN")
        headers = {"X-OCR-Platform-Token": token} if token else None
        self._client = httpx.AsyncClient(timeout=timeout, headers=headers)
        self._event_spool = (
            EventSpool(event_spool_dir, max_pending_bytes=event_spool_max_bytes)
            if event_spool_dir
            else None
        )
        self._log_spool = (
            LogSpool(event_spool_dir, max_pending_bytes=event_spool_max_bytes)
            if event_spool_dir
            else None
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def register(
        self, name: Optional[str] = None, host: Optional[str] = None
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.control_url}/api/servers/register",
            json={
                "id": self.server_id,
                "name": name or self.server_id,
                "host": host or self.server_id,
                "capacity_slots": 1,
                "capabilities": {"agent": "ocr-platform-mvp"},
            },
        )
        response.raise_for_status()
        return response.json()

    async def heartbeat(
        self,
        status: str = "idle",
        current_job_id: Optional[str] = None,
        capabilities: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "current_job_id": current_job_id,
            "capabilities": capabilities or {},
        }
        response = await self._client.post(
            f"{self.control_url}/api/servers/{self.server_id}/heartbeat",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def next_job(self) -> Optional[dict[str, Any]]:
        response = await self._client.post(
            f"{self.control_url}/api/agents/{self.server_id}/next-job"
        )
        response.raise_for_status()
        return response.json()

    async def get_job(self, job_id: str) -> dict[str, Any]:
        response = await self._client.get(f"{self.control_url}/api/jobs/{job_id}")
        response.raise_for_status()
        return response.json()

    async def get_job_summary(self, job_id: str) -> dict[str, Any]:
        response = await self._client.get(f"{self.control_url}/api/jobs/{job_id}/summary")
        response.raise_for_status()
        return response.json()

    async def _post_event_direct(self, job_id: str, event: dict[str, Any]) -> None:
        response = await self._client.post(
            f"{self.control_url}/api/jobs/{job_id}/events",
            json=event,
        )
        response.raise_for_status()

    def _spool_event(self, job_id: str, event: dict[str, Any]) -> None:
        if self._event_spool is not None:
            self._event_spool.append(server_id=self.server_id, job_id=job_id, event=event)

    async def post_event(self, job_id: str, event: dict[str, Any]) -> None:
        try:
            await self._post_event_direct(job_id, event)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                self._spool_event(job_id, event)
                return
            raise
        except httpx.RequestError:
            self._spool_event(job_id, event)
            return

    async def replay_spooled_events(self, limit: int | None = None) -> int:
        if self._event_spool is None:
            return 0
        records = self._event_spool.read_records()
        if not records:
            return 0
        replay_limit = len(records) if limit is None else max(limit, 0)
        replayed = 0
        remaining = list(records)
        for record in records[:replay_limit]:
            job_id = str(record.get("job_id") or "")
            event = record.get("event")
            if not job_id or not isinstance(event, dict):
                self._event_spool.quarantine_invalid(
                    record,
                    str(record.get("spool_parse_error") or "invalid spooled event record"),
                )
                remaining.pop(0)
                self._event_spool.replace_records(remaining)
                continue
            try:
                await self._post_event_direct(job_id, event)
            except httpx.RequestError:
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    break
                self._event_spool.quarantine(record, exc)
                remaining.pop(0)
                self._event_spool.replace_records(remaining)
                continue
            remaining.pop(0)
            replayed += 1
            self._event_spool.replace_records(remaining)
        return replayed

    async def _post_log_direct(
        self,
        job_id: str,
        stream: str,
        line: str,
        *,
        server_id: str | None = None,
    ) -> None:
        response = await self._client.post(
            f"{self.control_url}/api/jobs/{job_id}/logs",
            json={"server_id": server_id or self.server_id, "stream": stream, "line": line},
        )
        response.raise_for_status()

    def _spool_log(self, job_id: str, stream: str, line: str) -> None:
        if self._log_spool is not None:
            self._log_spool.append(
                server_id=self.server_id,
                job_id=job_id,
                stream=stream,
                line=line,
            )

    async def post_log(self, job_id: str, stream: str, line: str) -> None:
        try:
            await self._post_log_direct(job_id, stream, line)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                self._spool_log(job_id, stream, line)
                return
            raise
        except httpx.RequestError:
            self._spool_log(job_id, stream, line)
            return

    async def replay_spooled_logs(self, limit: int | None = None) -> int:
        if self._log_spool is None:
            return 0
        records = self._log_spool.read_records()
        if not records:
            return 0
        replay_limit = len(records) if limit is None else max(limit, 0)
        replayed = 0
        remaining = list(records)
        for record in records[:replay_limit]:
            job_id = str(record.get("job_id") or "")
            log = record.get("log")
            if not job_id or not isinstance(log, dict):
                self._log_spool.quarantine_invalid(
                    record,
                    str(record.get("spool_parse_error") or "invalid spooled log record"),
                )
                remaining.pop(0)
                self._log_spool.replace_records(remaining)
                continue
            try:
                await self._post_log_direct(
                    job_id,
                    str(log.get("stream") or "stdout"),
                    str(log.get("line") or ""),
                    server_id=str(log.get("server_id") or self.server_id),
                )
            except httpx.RequestError:
                break
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    break
                self._log_spool.quarantine(record, exc)
                remaining.pop(0)
                self._log_spool.replace_records(remaining)
                continue
            remaining.pop(0)
            replayed += 1
            self._log_spool.replace_records(remaining)
        return replayed

    async def claim_shard(self, job_id: str, server_id: str) -> Optional[dict[str, Any]]:
        response = await self._client.post(
            f"{self.control_url}/api/jobs/{job_id}/shards/claim",
            params={"server_id": server_id},
        )
        response.raise_for_status()
        return response.json()

    async def update_shard(self, shard_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.control_url}/api/shards/{shard_id}",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def register_manifest(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.control_url}/api/jobs/{job_id}/manifest",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def claim_scan_unit(self, server_id: str) -> Optional[dict[str, Any]]:
        response = await self._client.post(
            f"{self.control_url}/api/scan-units/claim",
            params={"server_id": server_id},
        )
        response.raise_for_status()
        return response.json()

    async def claim_manifest_integrity(self, server_id: str) -> Optional[dict[str, Any]]:
        response = await self._client.post(
            f"{self.control_url}/api/manifest-integrity/claim",
            params={"server_id": server_id},
        )
        response.raise_for_status()
        return response.json()

    async def complete_manifest_integrity(
        self,
        manifest_id: int,
        payload: dict[str, Any],
        server_id: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.control_url}/api/manifest-integrity/{manifest_id}/complete",
            params={"server_id": server_id},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def complete_scan_unit(self, scan_unit_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(
            f"{self.control_url}/api/scan-units/{scan_unit_id}/complete",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def fail_scan_unit(
        self,
        scan_unit_id: int,
        error_message: str,
        *,
        assigned_server_id: str | None = None,
        attempt_count: int | None = None,
        failure_category: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_message": error_message}
        if assigned_server_id is not None:
            payload["assigned_server_id"] = assigned_server_id
        if attempt_count is not None:
            payload["attempt_count"] = attempt_count
        if failure_category is not None:
            payload["failure_category"] = failure_category
        response = await self._client.post(
            f"{self.control_url}/api/scan-units/{scan_unit_id}/fail",
            json=payload,
        )
        response.raise_for_status()
        return response.json()
