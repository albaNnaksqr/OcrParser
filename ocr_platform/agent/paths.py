"""Shared filesystem path checks for OCR platform agents."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def check_shared_paths(paths: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    checked_at = datetime.now(timezone.utc).isoformat()
    for raw_path in paths:
        path = Path(raw_path)
        try:
            exists = path.exists()
            is_dir = path.is_dir()
            readable = os.access(path, os.R_OK) if exists else False
            writable = os.access(path, os.W_OK) if exists else False
            error = None
        except OSError as exc:
            exists = False
            is_dir = False
            readable = False
            writable = False
            error = str(exc)
        item: dict[str, Any] = {
            "path": str(path),
            "exists": exists,
            "is_dir": is_dir,
            "readable": readable,
            "writable": writable,
            "checked_at": checked_at,
        }
        if error:
            item["error"] = error
        results.append(item)
    return results
