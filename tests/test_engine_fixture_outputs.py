import json
from pathlib import Path

from tools import check_engine_fixture_outputs as checker


def write_fixture_output(tmp_path: Path, *, markdown: str, fallback_used: bool = False):
    document_dir = tmp_path / "sample"
    native_dir = document_dir / "native" / "mineru"
    native_dir.mkdir(parents=True)
    (native_dir / "sample.md").write_text(markdown, encoding="utf-8")
    (document_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success_fallback_text",
                "total_pages": 1,
                "stages": [{"stage": "layout", "status": "success"}],
                "fallback": {
                    "used": fallback_used,
                    "reason": "layout_empty" if fallback_used else None,
                    "source_stage": "layout" if fallback_used else None,
                },
            }
        ),
        encoding="utf-8",
    )


def expectations(tmp_path: Path) -> Path:
    path = tmp_path / "expected.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "fixtures": [
                    {
                        "filename": "sample.pdf",
                        "pages": 1,
                        "required_fields": ["Case ID: MIX-001"],
                        "reading_order": ["Applicant", "Item", "Verified"],
                        "table_cells": ["A. Liu", "Complete"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_fixture_checker_accepts_normalized_fields_order_and_no_fallback(tmp_path):
    write_fixture_output(
        tmp_path,
        markdown="Case ID: MIX-001\nApplicant\nItem | Complete | A. Liu\nVerified",
    )

    report = checker.check_outputs(expectations(tmp_path), output_dir=tmp_path, engine="mineru")

    assert report["status"] == "pass"
    assert report["passed_fixture_count"] == 1


def test_fixture_checker_rejects_missing_table_quality_and_fallback(tmp_path):
    write_fixture_output(
        tmp_path,
        markdown="Case ID: MIX-001\nApplicant\nItem\nVerified",
        fallback_used=True,
    )

    report = checker.check_outputs(expectations(tmp_path), output_dir=tmp_path, engine="mineru")

    assert report["status"] == "fail"
    fixture = report["fixtures"][0]
    assert fixture["missing_table_cells"] == ["A. Liu", "Complete"]
    assert "fallback_used" in fixture["problems"]


def test_text_normalization_handles_punctuation_spacing_and_case():
    assert checker.normalize_text("Total: 1,972.90") == checker.normalize_text("TOTAL 1972.90")
