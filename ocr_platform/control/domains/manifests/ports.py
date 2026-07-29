"""Compatibility redirects for the pre-PR7b manifest construction helpers."""

from __future__ import annotations

from .construction import (
    ScanCompletionShard,
    existing_scan_unit_paths,
    fail_manifest_if_scan_complete,
    freeze_manifest_if_scan_complete,
    lock_manifest_for_scan_unit_completion,
    manifest_for_scan_unit_completion_select,
    materialize_scan_unit_completion,
    next_manifest_shard_index,
)

__all__ = [
    "ScanCompletionShard",
    "existing_scan_unit_paths",
    "fail_manifest_if_scan_complete",
    "freeze_manifest_if_scan_complete",
    "lock_manifest_for_scan_unit_completion",
    "manifest_for_scan_unit_completion_select",
    "materialize_scan_unit_completion",
    "next_manifest_shard_index",
]
