import json

from ocr_parser.infra.resume import check_output_artifacts, is_file_already_processed


def no_console(*_args, **_kwargs):
    pass


def test_check_output_artifacts_accepts_success_sidecar_with_declared_artifacts(tmp_path):
    save_dir = tmp_path / "out" / "nested" / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    json_path = save_dir / "sample.json"
    native_path = save_dir / "native" / "dotsocr" / "page_0001_raw.json"
    native_path.parent.mkdir(parents=True)
    md_path.write_text("done", encoding="utf-8")
    json_path.write_text("{}", encoding="utf-8")
    native_path.write_text("{}", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/nested/sample.pdf",
                "filename": "nested/sample",
                "output_md_path": str(md_path),
                "artifacts": [
                    {"kind": "document_markdown", "path": str(md_path)},
                    {"kind": "document_json", "path": str(json_path)},
                    {"kind": "native_raw", "path": str(native_path)},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "nested/sample", no_console)

    assert report.ok is True
    assert report.status == "success"
    assert report.output_md_path == str(md_path)
    assert report.missing_artifacts == []
    assert report.invalid_artifacts == []


def test_check_output_artifacts_rejects_success_sidecar_when_markdown_missing(tmp_path):
    save_dir = tmp_path / "out" / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/sample.pdf",
                "filename": "sample",
                "output_md_path": str(md_path),
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "sample", no_console)

    assert report.ok is False
    assert report.failure_category == "artifact_missing"
    assert report.missing_artifacts == [str(md_path)]


def test_check_output_artifacts_rejects_malformed_sidecar_as_sidecar_invalid(tmp_path):
    save_dir = tmp_path / "out" / "sample"
    save_dir.mkdir(parents=True)
    sidecar_path = save_dir / ".ocr_status.json"
    sidecar_path.write_text("{not-json", encoding="utf-8")

    report = check_output_artifacts(tmp_path / "out", "sample", no_console)

    assert report.ok is False
    assert report.status == "invalid"
    assert report.failure_category == "sidecar_invalid"
    assert report.error_message == "invalid OCR status sidecar"
    assert report.invalid_artifacts == [{"path": str(sidecar_path), "reason": "invalid_sidecar"}]


def test_check_output_artifacts_rejects_truncated_json_artifact(tmp_path):
    save_dir = tmp_path / "out" / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    json_path = save_dir / "sample.json"
    md_path.write_text("done", encoding="utf-8")
    json_path.write_text("{", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/sample.pdf",
                "filename": "sample",
                "output_md_path": str(md_path),
                "artifacts": [
                    {"kind": "document_markdown", "path": str(md_path)},
                    {"kind": "document_json", "path": str(json_path)},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "sample", no_console)

    assert report.ok is False
    assert report.failure_category == "artifact_invalid"
    assert report.invalid_artifacts == [{"path": str(json_path), "reason": "invalid_json"}]


def test_check_output_artifacts_rejects_truncated_jsonl_artifact(tmp_path):
    save_dir = tmp_path / "out" / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    jsonl_path = save_dir / "native" / "page_events.jsonl"
    jsonl_path.parent.mkdir(parents=True)
    md_path.write_text("done", encoding="utf-8")
    jsonl_path.write_text('{"page": 1}\n{"page": ', encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/sample.pdf",
                "filename": "sample",
                "output_md_path": str(md_path),
                "artifacts": [
                    {"kind": "document_markdown", "path": str(md_path)},
                    {"kind": "native_jsonl", "path": str(jsonl_path)},
                ],
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "sample", no_console)

    assert report.ok is False
    assert report.failure_category == "artifact_invalid"
    assert report.invalid_artifacts == [{"path": str(jsonl_path), "reason": "invalid_jsonl"}]


def test_check_output_artifacts_rejects_artifact_path_that_is_not_a_file(tmp_path):
    save_dir = tmp_path / "out" / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    md_path.mkdir()
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/sample.pdf",
                "filename": "sample",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "sample", no_console)

    assert report.ok is False
    assert report.failure_category == "artifact_invalid"
    assert report.invalid_artifacts == [{"path": str(md_path), "reason": "not_file"}]


def test_check_output_artifacts_rejects_success_sidecar_with_external_artifact_path(tmp_path):
    save_dir = tmp_path / "out" / "nested" / "sample"
    save_dir.mkdir(parents=True)
    external_md = tmp_path / "other" / "sample.md"
    external_md.parent.mkdir()
    external_md.write_text("belongs to another output key", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/nested/sample.pdf",
                "filename": "nested/sample",
                "output_md_path": str(external_md),
                "artifacts": [{"kind": "document_markdown", "path": str(external_md)}],
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "nested/sample", no_console)

    assert report.ok is False
    assert report.failure_category == "artifact_invalid"
    assert report.invalid_artifacts == [
        {"path": str(external_md), "reason": "outside_output_dir"},
    ]


def test_check_output_artifacts_rejects_success_sidecar_with_failed_pages(tmp_path):
    save_dir = tmp_path / "out" / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    md_path.write_text("done", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": "/input/sample.pdf",
                "filename": "sample",
                "output_md_path": str(md_path),
                "failed_pages": 1,
                "page_status_counts": {"error": 1, "success": 2},
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
            }
        ),
        encoding="utf-8",
    )

    report = check_output_artifacts(tmp_path / "out", "sample", no_console)

    assert report.ok is False
    assert report.failure_category == "page_failure"
    assert report.error_message == "OCR status sidecar reports failed pages despite success status"


def test_resume_skip_uses_artifact_completeness_report(tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    output_dir = tmp_path / "out"
    save_dir = output_dir / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    md_path.write_text("", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": str(input_file),
                "filename": "sample",
                "output_md_path": str(md_path),
            }
        ),
        encoding="utf-8",
    )

    is_processed, existing_path = is_file_already_processed(input_file, output_dir, "sample", no_console)

    assert is_processed is False
    assert existing_path == str(md_path)


def test_resume_skip_can_require_success_sidecar_for_manifest_shards(tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    output_dir = tmp_path / "out"
    save_dir = output_dir / "sample"
    save_dir.mkdir(parents=True)
    legacy_md = save_dir / "sample.md"
    legacy_md.write_text("legacy output without status sidecar", encoding="utf-8")

    is_processed, existing_path = is_file_already_processed(
        input_file,
        output_dir,
        "sample",
        no_console,
        require_status_sidecar=True,
    )

    assert is_processed is False
    assert existing_path == str(legacy_md)


def test_resume_skip_can_require_sidecar_input_snapshot_for_manifest_shards(tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    output_dir = tmp_path / "out"
    save_dir = output_dir / "sample"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "sample.md"
    md_path.write_text("done", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "file_path": str(input_file),
                "filename": "sample",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
            }
        ),
        encoding="utf-8",
    )

    ordinary_skip, _ = is_file_already_processed(input_file, output_dir, "sample", no_console)
    manifest_skip, existing_path = is_file_already_processed(
        input_file,
        output_dir,
        "sample",
        no_console,
        require_status_sidecar=True,
        expected_input_size_bytes=input_file.stat().st_size,
        expected_input_mtime_ns=input_file.stat().st_mtime_ns,
        require_input_snapshot=True,
    )

    assert ordinary_skip is True
    assert manifest_skip is False
    assert existing_path == str(md_path)
