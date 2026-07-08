from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _manifest_non_negative_int(payload: dict[str, Any], field_name: str) -> int:
    value = payload[field_name]
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _manifest_non_empty_str(payload: dict[str, Any], field_name: str) -> str:
    value = payload[field_name]
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ManifestItem:
    input_path: str
    relative_path: str
    size_bytes: int
    mtime_ns: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_path": self.input_path,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManifestItem":
        return cls(
            input_path=_manifest_non_empty_str(payload, "input_path"),
            relative_path=_manifest_non_empty_str(payload, "relative_path"),
            size_bytes=_manifest_non_negative_int(payload, "size_bytes"),
            mtime_ns=_manifest_non_negative_int(payload, "mtime_ns"),
        )

    @classmethod
    def from_json_line(cls, line: str) -> "ManifestItem":
        return cls.from_dict(json.loads(line))


@dataclass(frozen=True)
class ManifestScanResult:
    input_root: str
    items: list[ManifestItem]
    skipped_errors: list[dict[str, str]] = field(default_factory=list)
    skipped_error_count: int | None = None
    scanned_dir_count: int = 0

    @property
    def file_count(self) -> int:
        return len(self.items)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    @property
    def scan_error_count(self) -> int:
        if self.skipped_error_count is not None:
            return self.skipped_error_count
        return len(self.skipped_errors)


@dataclass(frozen=True)
class ShardSpec:
    index: int
    path: Path
    file_count: int


@dataclass(frozen=True)
class WrittenManifest:
    manifest_path: Path
    meta_path: Path
    shards: list[ShardSpec]
    file_count: int
    total_bytes: int
