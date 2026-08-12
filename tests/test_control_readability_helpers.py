from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ocr_parser.contracts import ManifestItem
from ocr_platform.control.domains.jobs.projection import _calculate_job_rates
from ocr_platform.control.domains.manifests.integrity import _read_manifest_jsonl
from ocr_platform.control.models import Job


def _job(*, status: str, created_at: datetime) -> Job:
    return Job(
        id="job-readable",
        input_dir="/shared/input",
        output_dir="/shared/output",
        engine="dotsocr",
        assigned_server_id="worker-1",
        status=status,
        created_at=created_at,
    )


def test_calculate_job_rates_is_a_pure_projection() -> None:
    started_at = datetime(2026, 8, 12, 8, tzinfo=timezone.utc)
    job = _job(status="running", created_at=started_at)
    job.started_at = started_at

    result = _calculate_job_rates(
        job,
        summary_now=started_at + timedelta(seconds=20),
        first_event_at=started_at,
        last_event_at=started_at + timedelta(seconds=19),
        last_heartbeat_at=started_at + timedelta(seconds=19),
        total_files=10,
        completed_files=4,
        failed_files=1,
        skipped_files=0,
        total_pages=20,
        completed_pages=10,
    )

    assert result == {
        "progress_percent": 50.0,
        "pages_per_second": 0.5,
        "files_per_minute": 15.0,
        "eta_seconds": 20,
        "is_stale": False,
    }


def test_read_manifest_jsonl_returns_rows_without_losing_order(tmp_path) -> None:
    path = tmp_path / "manifest.jsonl"
    items = [
        ManifestItem("/input/a.pdf", "a.pdf", 10, 1),
        ManifestItem("/input/b.pdf", "nested/b.pdf", 20, 2),
    ]
    path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )

    rows, error = _read_manifest_jsonl(path)

    assert error is None
    assert rows == (2, {"a.pdf", "nested/b.pdf"}, 30)


def test_read_manifest_jsonl_classifies_duplicate_paths(tmp_path) -> None:
    path = tmp_path / "manifest.jsonl"
    item = ManifestItem("/input/a.pdf", "a.pdf", 10, 1)
    path.write_text(
        f"{item.to_json_line()}\n{item.to_json_line()}\n",
        encoding="utf-8",
    )

    rows, error = _read_manifest_jsonl(path)

    assert rows is None
    assert error == "duplicate_relative_path"
