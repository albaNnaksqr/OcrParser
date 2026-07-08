import json

import pytest

from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import scan_folder_snapshot


def test_manifest_item_round_trips_jsonl_record():
    item = ManifestItem(
        input_path="/shared/input/a/001.pdf",
        relative_path="a/001.pdf",
        size_bytes=123,
        mtime_ns=456,
    )

    encoded = item.to_json_line()
    decoded = ManifestItem.from_json_line(encoded)

    assert json.loads(encoded) == {
        "input_path": "/shared/input/a/001.pdf",
        "relative_path": "a/001.pdf",
        "size_bytes": 123,
        "mtime_ns": 456,
    }
    assert decoded == item


def test_scan_folder_snapshot_recurses_and_preserves_relative_paths(tmp_path):
    root = tmp_path / "input"
    nested = root / "b" / "inner"
    nested.mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (nested / "001.PDF").write_bytes(b"%PDF-1.4\n")
    (nested / "note.txt").write_text("ignore me")

    result = scan_folder_snapshot(root)

    assert [item.relative_path for item in result.items] == [
        "a.pdf",
        "b/inner/001.PDF",
    ]
    assert all(item.input_path.startswith(str(root)) for item in result.items)
    assert result.file_count == 2
    assert result.total_bytes > 0
    assert result.scanned_dir_count == 3


def test_scan_folder_snapshot_reports_missing_root(tmp_path):
    with pytest.raises(FileNotFoundError, match="input root not found"):
        scan_folder_snapshot(tmp_path / "missing")


def test_scan_folder_snapshot_records_skipped_errors(monkeypatch, tmp_path):
    root = tmp_path / "input"
    blocked = root / "blocked"
    blocked.mkdir(parents=True)
    (root / "ok.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "z_bad.pdf").write_bytes(b"%PDF-1.4\n")
    (blocked / "hidden.pdf").write_bytes(b"%PDF-1.4\n")

    from ocr_platform.manifest import scanner

    original_scandir = scanner.os.scandir
    original_stat_manifest_file = scanner._stat_manifest_file

    def fake_scandir(path):
        if path == blocked.resolve():
            raise PermissionError("cannot read directory")
        return original_scandir(path)

    def fake_stat_manifest_file(path):
        if path.name == "z_bad.pdf":
            raise OSError("cannot stat file")
        return original_stat_manifest_file(path)

    monkeypatch.setattr(scanner.os, "scandir", fake_scandir)
    monkeypatch.setattr(scanner, "_stat_manifest_file", fake_stat_manifest_file)

    result = scan_folder_snapshot(root)

    assert [item.relative_path for item in result.items] == ["ok.pdf"]
    assert result.scan_error_count == 2
    assert result.skipped_errors == [
        {
            "path": str(blocked),
            "reason": "cannot read directory",
            "failure_category": "input_invalid",
        },
        {
            "path": str(root / "z_bad.pdf"),
            "reason": "cannot stat file",
            "failure_category": "input_invalid",
        },
    ]


def test_scan_folder_snapshot_bounds_skipped_error_samples_but_counts_all(monkeypatch, tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    for index in range(8):
        (root / f"bad-{index}.pdf").write_bytes(b"%PDF-1.4\n")

    from ocr_platform.manifest import scanner

    def stat_fails(path):
        raise PermissionError(f"cannot stat {path.name}")

    monkeypatch.setattr(scanner, "_stat_manifest_file", stat_fails)

    result = scan_folder_snapshot(root)

    assert result.file_count == 0
    assert result.scan_error_count == 8
    assert len(result.skipped_errors) == 5
