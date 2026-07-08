import json
from datetime import datetime

import pytest

import ocr_platform.manifest.scanner as scanner
from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.sharder import (
    write_folder_snapshot_streaming,
    write_manifest_snapshot,
    write_manifest_snapshot_streaming,
)


def make_item(index: int) -> ManifestItem:
    return ManifestItem(
        input_path=f"/shared/input/{index}.pdf",
        relative_path=f"{index}.pdf",
        size_bytes=index + 10,
        mtime_ns=index + 100,
    )


def test_write_manifest_snapshot_creates_meta_manifest_and_shards(tmp_path):
    result = write_manifest_snapshot(
        job_id="job-1",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=[make_item(0), make_item(1), make_item(2)],
        target_files_per_shard=2,
        scanned_dir_count=4,
    )

    assert result.manifest_path.exists()
    assert result.meta_path.exists()
    assert [shard.path.name for shard in result.shards] == [
        "shard-000001.jsonl",
        "shard-000002.jsonl",
    ]
    assert [shard.file_count for shard in result.shards] == [2, 1]
    assert result.file_count == 3
    assert result.total_bytes == 33

    manifest_lines = result.manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(manifest_lines) == 3
    assert json.loads(manifest_lines[0])["relative_path"] == "0.pdf"

    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert meta["job_id"] == "job-1"
    assert meta["input_root"] == "/shared/input"
    assert meta["file_count"] == 3
    assert meta["total_bytes"] == 33
    assert meta["scanned_dir_count"] == 4
    scan_started_at = datetime.fromisoformat(meta["scan_started_at"])
    scan_finished_at = datetime.fromisoformat(meta["scan_finished_at"])
    assert scan_finished_at >= scan_started_at


def test_write_manifest_snapshot_preserves_input_order_in_manifest_and_shards(tmp_path):
    items = [make_item(2), make_item(0), make_item(1)]

    result = write_manifest_snapshot(
        job_id="job-order",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=items,
        target_files_per_shard=2,
    )

    manifest_paths = [
        json.loads(line)["relative_path"]
        for line in result.manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    shard_paths = [
        [
            json.loads(line)["relative_path"]
            for line in shard.path.read_text(encoding="utf-8").splitlines()
        ]
        for shard in result.shards
    ]

    assert manifest_paths == ["2.pdf", "0.pdf", "1.pdf"]
    assert shard_paths == [["2.pdf", "0.pdf"], ["1.pdf"]]


def test_write_manifest_snapshot_streaming_consumes_items_once(tmp_path):
    consumed = 0

    def one_shot_items():
        nonlocal consumed
        for index in range(5):
            consumed += 1
            yield make_item(index)

    result = write_manifest_snapshot_streaming(
        job_id="job-streaming",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=one_shot_items(),
        target_files_per_shard=2,
    )

    assert consumed == 5
    assert result.file_count == 5
    assert result.total_bytes == 60
    assert [shard.file_count for shard in result.shards] == [2, 2, 1]

    manifest_paths = [
        json.loads(line)["relative_path"]
        for line in result.manifest_path.read_text(encoding="utf-8").splitlines()
    ]
    assert manifest_paths == ["0.pdf", "1.pdf", "2.pdf", "3.pdf", "4.pdf"]


def test_write_manifest_snapshot_streaming_reports_progress(tmp_path):
    progress = []

    result = write_manifest_snapshot_streaming(
        job_id="job-progress",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=(make_item(index) for index in range(3)),
        target_files_per_shard=2,
        progress_interval_files=2,
        progress_callback=progress.append,
    )

    assert result.file_count == 3
    assert [item["scanned_files"] for item in progress] == [2, 3]
    assert progress[0]["status"] == "running"
    assert progress[-1]["status"] == "done"
    assert progress[-1]["shard_count"] == 2
    assert progress[-1]["current_path"].endswith("2.pdf")
    started = datetime.fromisoformat(progress[-1]["scan_started_at"])
    finished = datetime.fromisoformat(progress[-1]["scan_finished_at"])
    assert started.tzinfo is not None
    assert finished >= started


def test_write_manifest_snapshot_progress_estimates_remaining_when_total_is_known(
    tmp_path, monkeypatch
):
    progress = []
    monotonic_values = iter([100.0, 102.0, 104.0, 106.0])

    monkeypatch.setattr(
        "ocr_platform.manifest.sharder.time.monotonic",
        lambda: next(monotonic_values),
    )

    write_manifest_snapshot_streaming(
        job_id="job-progress-eta",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=(make_item(index) for index in range(5)),
        target_files_per_shard=10,
        progress_interval_files=2,
        progress_callback=progress.append,
        estimated_total_files=10,
    )

    assert progress[0]["status"] == "running"
    assert progress[0]["scanned_files"] == 2
    assert progress[0]["estimated_total_files"] == 10
    assert progress[0]["remaining_files"] == 8
    assert progress[0]["estimated_remaining_seconds"] == 8
    assert progress[-1]["status"] == "done"
    assert progress[-1]["scanned_files"] == 5
    assert progress[-1]["remaining_files"] == 5
    assert progress[-1]["estimated_remaining_seconds"] == 6


def test_write_manifest_snapshot_progress_marks_eta_unknown_without_total(tmp_path):
    progress = []

    write_manifest_snapshot_streaming(
        job_id="job-progress-no-eta",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=(make_item(index) for index in range(1)),
        target_files_per_shard=1,
        progress_interval_files=1,
        progress_callback=progress.append,
    )

    assert progress[-1]["estimated_total_files"] is None
    assert progress[-1]["remaining_files"] is None
    assert progress[-1]["estimated_remaining_seconds"] is None


def test_write_manifest_snapshot_streaming_progress_includes_error_samples(tmp_path):
    progress = []
    skipped_errors = [
        {
            "path": "/shared/input/bad",
            "reason": "permission denied",
            "failure_category": "input_invalid",
        },
        {
            "path": "/shared/input/missing",
            "reason": "not found",
            "failure_category": "input_missing",
        },
    ]

    write_manifest_snapshot_streaming(
        job_id="job-progress-errors",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=(make_item(index) for index in range(1)),
        target_files_per_shard=1,
        skipped_errors=skipped_errors,
        progress_interval_files=1,
        progress_callback=progress.append,
    )

    assert progress[-1]["skipped_error_count"] == 2
    assert progress[-1]["skipped_errors"] == skipped_errors
    assert progress[-1]["files_per_second"] >= 0
    assert progress[-1]["elapsed_seconds"] >= 0


def test_write_manifest_snapshot_streaming_reports_zero_file_scan_errors(tmp_path):
    progress = []
    skipped_errors = [
        {
            "path": "/shared/input/blocked",
            "reason": "permission denied",
            "failure_category": "input_invalid",
        }
    ]

    result = write_manifest_snapshot_streaming(
        job_id="job-empty-progress-errors",
        input_root="/shared/input",
        output_dir=tmp_path,
        items=iter(()),
        target_files_per_shard=1,
        skipped_errors=skipped_errors,
        progress_interval_files=1,
        progress_callback=progress.append,
    )

    assert result.file_count == 0
    assert progress[-1]["status"] == "done"
    assert progress[-1]["scanned_files"] == 0
    assert progress[-1]["current_path"] == "/shared/input"
    assert progress[-1]["skipped_error_count"] == 1
    assert progress[-1]["skipped_errors"] == skipped_errors


def test_write_folder_snapshot_streaming_progress_includes_scanned_directory_count(tmp_path):
    input_root = tmp_path / "input"
    nested = input_root / "nested"
    nested.mkdir(parents=True)
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (nested / "b.pdf").write_bytes(b"%PDF-1.4\n")
    progress = []

    write_folder_snapshot_streaming(
        job_id="job-progress-dirs",
        input_root=input_root,
        output_dir=tmp_path / "manifest",
        target_files_per_shard=1,
        progress_interval_files=1,
        progress_callback=progress.append,
    )

    assert progress[-1]["status"] == "done"
    assert progress[-1]["scanned_files"] == 2
    assert progress[-1]["scanned_dirs"] >= 2


def test_write_folder_snapshot_streaming_bounds_scan_error_samples_in_meta(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(8):
        (input_root / f"bad-{index}.pdf").write_bytes(b"%PDF-1.4\n")

    def stat_fails(path):
        raise PermissionError(f"cannot stat {path.name}")

    monkeypatch.setattr(scanner, "_stat_manifest_file", stat_fails)
    progress = []

    result = write_folder_snapshot_streaming(
        job_id="job-bounded-errors",
        input_root=input_root,
        output_dir=tmp_path / "manifest",
        target_files_per_shard=1,
        progress_interval_files=1,
        progress_callback=progress.append,
    )

    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert result.file_count == 0
    assert meta["skipped_error_count"] == 8
    assert len(meta["skipped_errors"]) == 5
    assert progress[-1]["skipped_error_count"] == 8
    assert len(progress[-1]["skipped_errors"]) == 5


def test_write_manifest_snapshot_rejects_invalid_target_files_per_shard(tmp_path):
    with pytest.raises(ValueError, match="target_files_per_shard must be positive"):
        write_manifest_snapshot(
            job_id="job-1",
            input_root="/shared/input",
            output_dir=tmp_path,
            items=[make_item(0)],
            target_files_per_shard=0,
        )
