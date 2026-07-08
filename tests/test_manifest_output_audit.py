import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ocr_platform.manifest.models import ManifestItem


def _write_success_sidecar(output_root: Path, relative_pdf: str) -> None:
    relative_path = Path(relative_pdf)
    save_dir = output_root / relative_path.parent / relative_path.stem
    save_dir.mkdir(parents=True)
    md_path = save_dir / f"{relative_path.stem}.md"
    md_path.write_text("done", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
            }
        ),
        encoding="utf-8",
    )


def _write_success_sidecar_with_input_snapshot(
    output_root: Path,
    relative_pdf: str,
    *,
    size_bytes: int,
    mtime_ns: int,
    manifest_relative_path: Optional[str] = None,
) -> None:
    relative_path = Path(relative_pdf)
    save_dir = output_root / relative_path.parent / relative_path.stem
    save_dir.mkdir(parents=True)
    md_path = save_dir / f"{relative_path.stem}.md"
    md_path.write_text("done", encoding="utf-8")
    payload = {
        "status": "success",
        "output_md_path": str(md_path),
        "input_size_bytes": size_bytes,
        "input_mtime_ns": mtime_ns,
        "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
    }
    if manifest_relative_path is not None:
        payload["manifest_relative_path"] = manifest_relative_path
    (save_dir / ".ocr_status.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_success_sidecar_with_failed_page_summary(
    output_root: Path,
    relative_pdf: str,
    *,
    size_bytes: int,
    mtime_ns: int,
) -> None:
    relative_path = Path(relative_pdf)
    save_dir = output_root / relative_path.parent / relative_path.stem
    save_dir.mkdir(parents=True)
    md_path = save_dir / f"{relative_path.stem}.md"
    md_path.write_text("partial", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "input_size_bytes": size_bytes,
                "input_mtime_ns": mtime_ns,
                "failed_pages": 1,
                "page_status_counts": {"error": 1, "success": 2},
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
            }
        ),
        encoding="utf-8",
    )


def _write_success_sidecar_with_missing_artifact(output_root: Path, relative_pdf: str) -> None:
    relative_path = Path(relative_pdf)
    save_dir = output_root / relative_path.parent / relative_path.stem
    save_dir.mkdir(parents=True)
    md_path = save_dir / f"{relative_path.stem}.md"
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
            }
        ),
        encoding="utf-8",
    )


def _write_success_sidecar_with_external_artifact(
    output_root: Path,
    relative_pdf: str,
    external_path: Path,
    *,
    size_bytes: int,
    mtime_ns: int,
) -> None:
    relative_path = Path(relative_pdf)
    save_dir = output_root / relative_path.parent / relative_path.stem
    save_dir.mkdir(parents=True)
    external_path.parent.mkdir(parents=True, exist_ok=True)
    external_path.write_text("belongs to another output key", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(external_path),
                "input_size_bytes": size_bytes,
                "input_mtime_ns": mtime_ns,
                "artifacts": [{"kind": "document_markdown", "path": str(external_path)}],
            }
        ),
        encoding="utf-8",
    )


def _write_failed_sidecar(
    output_root: Path,
    relative_pdf: str,
    *,
    failure_category: str,
    error_type: str,
) -> None:
    relative_path = Path(relative_pdf)
    save_dir = output_root / relative_path.parent / relative_path.stem
    save_dir.mkdir(parents=True)
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failure_category": failure_category,
                "error_type": error_type,
                "error": "model request timed out after 180s",
            }
        ),
        encoding="utf-8",
    )


def _manifest_item(input_root: Path, relative_pdf: str) -> ManifestItem:
    input_path = input_root / relative_pdf
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"%PDF-1.4\n")
    stat = input_path.stat()
    return ManifestItem(
        input_path=str(input_path),
        relative_path=relative_pdf,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def test_audit_manifest_outputs_reports_missing_and_incomplete_artifacts(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "shard.jsonl"
    items = [
        _manifest_item(input_root, "nested/a.pdf"),
        _manifest_item(input_root, "missing/b.pdf"),
        _manifest_item(input_root, "broken/c.pdf"),
    ]
    manifest_path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "nested/a.pdf",
        size_bytes=items[0].size_bytes,
        mtime_ns=items[0].mtime_ns,
    )
    _write_success_sidecar_with_missing_artifact(output_root, "broken/c.pdf")

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        check_input=False,
    )

    assert report.total_items == 3
    assert report.ok_items == 1
    assert report.issue_count == 2
    assert report.issues_by_category == {
        "artifact_missing": 1,
        "sidecar_missing": 1,
    }
    assert [sample["relative_path"] for sample in report.issue_samples] == [
        "missing/b.pdf",
        "broken/c.pdf",
    ]


def test_audit_manifest_outputs_checks_input_freshness_when_requested(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "changed.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    _write_success_sidecar(output_root, "changed.pdf")
    (input_root / "changed.pdf").write_bytes(b"%PDF-1.4\nchanged\n")

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        check_input=True,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"input_changed": 1}
    assert report.issue_samples[0]["relative_path"] == "changed.pdf"


def test_audit_manifest_outputs_reports_malformed_sidecar_as_sidecar_invalid(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "broken.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    save_dir = output_root / "broken"
    save_dir.mkdir(parents=True)
    sidecar_path = save_dir / ".ocr_status.json"
    sidecar_path.write_text("{not-json", encoding="utf-8")

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"sidecar_invalid": 1}
    assert report.issue_samples[0]["relative_path"] == "broken.pdf"
    assert report.issue_samples[0]["invalid_artifacts"] == [
        {"path": str(sidecar_path), "reason": "invalid_sidecar"}
    ]


def test_audit_manifest_outputs_rejects_sidecar_input_snapshot_mismatch(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "stale.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "stale.pdf",
        size_bytes=item.size_bytes + 100,
        mtime_ns=item.mtime_ns + 1,
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        check_input=False,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"sidecar_input_mismatch": 1}
    assert report.issue_samples[0]["relative_path"] == "stale.pdf"
    assert report.issue_samples[0]["sidecar_input_size_bytes"] == item.size_bytes + 100
    assert report.issue_samples[0]["manifest_size_bytes"] == item.size_bytes


def test_audit_manifest_outputs_rejects_sidecar_relative_path_mismatch(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "nested/current.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "nested/current.pdf",
        size_bytes=item.size_bytes,
        mtime_ns=item.mtime_ns,
        manifest_relative_path="other/copied.pdf",
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        check_input=False,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"sidecar_relative_path_mismatch": 1}
    assert report.issue_samples[0]["relative_path"] == "nested/current.pdf"
    assert report.issue_samples[0]["sidecar_manifest_relative_path"] == "other/copied.pdf"


def test_audit_manifest_outputs_rejects_success_sidecar_without_input_snapshot(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "legacy.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    _write_success_sidecar(output_root, "legacy.pdf")

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        check_input=False,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"sidecar_input_missing": 1}
    assert report.issue_samples[0]["relative_path"] == "legacy.pdf"
    assert report.issue_samples[0]["sidecar_path"].endswith(".ocr_status.json")


def test_audit_manifest_outputs_rejects_success_sidecar_with_failed_pages(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "partial.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    _write_success_sidecar_with_failed_page_summary(
        output_root,
        "partial.pdf",
        size_bytes=item.size_bytes,
        mtime_ns=item.mtime_ns,
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        check_input=False,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"page_failure": 1}
    assert report.issue_samples[0]["relative_path"] == "partial.pdf"
    assert report.issue_samples[0]["sidecar_failure_category"] == "page_failure"


def test_audit_manifest_outputs_rejects_external_artifact_paths(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "nested/external.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")
    external_path = tmp_path / "other" / "external.md"
    _write_success_sidecar_with_external_artifact(
        output_root,
        "nested/external.pdf",
        external_path,
        size_bytes=item.size_bytes,
        mtime_ns=item.mtime_ns,
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
    )

    assert report.total_items == 1
    assert report.ok_items == 0
    assert report.issues_by_category == {"artifact_invalid": 1}
    assert report.issue_samples[0]["invalid_artifacts"] == [
        {"path": str(external_path), "reason": "outside_output_dir"},
    ]


def test_audit_manifest_outputs_includes_failed_sidecar_error_type_in_sample(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    first = _manifest_item(input_root, "timeout.pdf")
    second = _manifest_item(input_root, "missing-input.pdf")
    manifest_path.write_text(
        "\n".join([first.to_json_line(), second.to_json_line()]) + "\n",
        encoding="utf-8",
    )
    _write_failed_sidecar(
        output_root,
        "timeout.pdf",
        failure_category="api_timeout",
        error_type="TimeoutError",
    )
    _write_failed_sidecar(
        output_root,
        "missing-input.pdf",
        failure_category="input_missing",
        error_type="InputMissing",
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
    )

    assert report.total_items == 2
    assert report.ok_items == 0
    assert report.issues_by_category == {"api_timeout": 1, "input_missing": 1}
    assert report.issues_by_failure_category == {"api_timeout": 1, "input_missing": 1}
    assert report.issues_by_error_type == {"InputMissing": 1, "TimeoutError": 1}
    assert report.issue_samples[0]["sidecar_failure_category"] == "api_timeout"
    assert report.issue_samples[0]["sidecar_error_type"] == "TimeoutError"
    assert report.to_dict()["issues_by_failure_category"] == {
        "api_timeout": 1,
        "input_missing": 1,
    }
    assert report.to_dict()["issues_by_error_type"] == {
        "InputMissing": 1,
        "TimeoutError": 1,
    }


def test_audit_manifest_outputs_marks_report_truncated_when_max_items_limits_scan(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    items = [
        _manifest_item(input_root, "a.pdf"),
        _manifest_item(input_root, "b.pdf"),
    ]
    manifest_path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "a.pdf",
        size_bytes=items[0].size_bytes,
        mtime_ns=items[0].mtime_ns,
    )
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "b.pdf",
        size_bytes=items[1].size_bytes,
        mtime_ns=items[1].mtime_ns,
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        max_items=1,
    )
    payload = report.to_dict()

    assert report.ok is True
    assert payload["audited_items"] == 1
    assert payload["max_items"] == 1
    assert payload["truncated"] is True


def test_audit_manifest_outputs_rejects_duplicate_relative_path(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    first = _manifest_item(input_root, "a/original.pdf")
    second = _manifest_item(input_root, "b/duplicate.pdf")
    first = ManifestItem(
        input_path=first.input_path,
        relative_path="same/output.pdf",
        size_bytes=first.size_bytes,
        mtime_ns=first.mtime_ns,
    )
    second = ManifestItem(
        input_path=second.input_path,
        relative_path="same/output.pdf",
        size_bytes=second.size_bytes,
        mtime_ns=second.mtime_ns,
    )
    manifest_path.write_text(
        "\n".join([first.to_json_line(), second.to_json_line()]) + "\n",
        encoding="utf-8",
    )
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "same/output.pdf",
        size_bytes=first.size_bytes,
        mtime_ns=first.mtime_ns,
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
    )

    assert report.ok is False
    assert report.total_items == 2
    assert report.ok_items == 1
    assert report.issues_by_category == {"duplicate_relative_path": 1}
    assert report.issue_samples[0]["relative_path"] == "same/output.pdf"
    assert report.issue_samples[0]["first_line_number"] == 1
    assert report.issue_samples[0]["line_number"] == 2


def test_audit_manifest_outputs_rejects_invalid_relative_path_shape(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    manifest_path = tmp_path / "manifest.jsonl"
    items = [
        _manifest_item(input_root, "nested\\bad.pdf"),
        _manifest_item(input_root, "nested/file.txt"),
    ]
    manifest_path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=tmp_path / "output",
    )

    assert report.ok is False
    assert report.total_items == 2
    assert report.issues_by_category == {"invalid_relative_path": 2}
    assert [sample["line_number"] for sample in report.issue_samples] == [1, 2]


def test_audit_manifest_outputs_marks_issue_samples_truncated(tmp_path):
    from ocr_parser.infra.output_audit import audit_manifest_outputs

    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    items = [
        _manifest_item(input_root, "missing-a.pdf"),
        _manifest_item(input_root, "missing-b.pdf"),
    ]
    manifest_path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )

    report = audit_manifest_outputs(
        manifest_path=manifest_path,
        output_dir=output_root,
        sample_limit=1,
    )
    payload = report.to_dict()

    assert report.issue_count == 2
    assert len(report.issue_samples) == 1
    assert payload["issue_sample_limit"] == 1
    assert payload["issue_samples_truncated"] is True


def test_audit_manifest_outputs_tool_returns_json_and_nonzero_on_issues(tmp_path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    item = _manifest_item(input_root, "missing.pdf")
    manifest_path.write_text(item.to_json_line() + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "tools/audit_manifest_outputs.py",
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["total_items"] == 1
    assert payload["issues_by_category"] == {"sidecar_missing": 1}


def test_audit_manifest_outputs_tool_marks_max_items_report_as_truncated(tmp_path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    items = [
        _manifest_item(input_root, "a.pdf"),
        _manifest_item(input_root, "b.pdf"),
    ]
    manifest_path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "a.pdf",
        size_bytes=items[0].size_bytes,
        mtime_ns=items[0].mtime_ns,
    )
    _write_success_sidecar_with_input_snapshot(
        output_root,
        "b.pdf",
        size_bytes=items[1].size_bytes,
        mtime_ns=items[1].mtime_ns,
    )

    result = subprocess.run(
        [
            sys.executable,
            "tools/audit_manifest_outputs.py",
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_root),
            "--max-items",
            "1",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["audited_items"] == 1
    assert payload["max_items"] == 1
    assert payload["truncated"] is True


def test_audit_manifest_outputs_tool_marks_issue_samples_truncated(tmp_path):
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    manifest_path = tmp_path / "manifest.jsonl"
    items = [
        _manifest_item(input_root, "missing-a.pdf"),
        _manifest_item(input_root, "missing-b.pdf"),
    ]
    manifest_path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "tools/audit_manifest_outputs.py",
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_root),
            "--sample-limit",
            "1",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["issue_count"] == 2
    assert len(payload["issue_samples"]) == 1
    assert payload["issue_sample_limit"] == 1
    assert payload["issue_samples_truncated"] is True
