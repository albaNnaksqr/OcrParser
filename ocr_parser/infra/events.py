from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


_PATH_LOCKS: dict[Path, threading.Lock] = {}
_PATH_LOCKS_LOCK = threading.Lock()


class EventWriter(Protocol):
    def emit(self, event_type: str, **payload: Any) -> None:
        raise NotImplementedError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_for_path(path: Path) -> threading.Lock:
    lock_path = path.expanduser().resolve(strict=False)
    with _PATH_LOCKS_LOCK:
        lock = _PATH_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[lock_path] = lock
        return lock


@dataclass
class OCREventWriter:
    path: str
    job_id: str = ""

    def __post_init__(self) -> None:
        self._path = Path(self.path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _lock_for_path(self._path)

    def emit(self, event_type: str, **payload: Any) -> None:
        record = {
            "type": event_type,
            "job_id": self.job_id,
            "created_at": utc_now_iso(),
            "payload": payload,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


class NullOCREventWriter:
    def emit(self, event_type: str, **payload: Any) -> None:
        return None


def build_event_writer(job_event_file: str | None, job_id: str | None = None) -> EventWriter:
    if not job_event_file:
        return NullOCREventWriter()
    return OCREventWriter(path=job_event_file, job_id=job_id or "")
