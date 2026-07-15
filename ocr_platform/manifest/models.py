from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from ocr_parser.contracts import ManifestItem


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
