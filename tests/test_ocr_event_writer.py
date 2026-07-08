from collections import Counter
import json
from pathlib import Path
import threading

from ocr_parser.infra.events import OCREventWriter, NullOCREventWriter


def test_event_writer_appends_jsonl_records(tmp_path):
    event_path = tmp_path / "events" / "job.jsonl"
    writer = OCREventWriter(path=str(event_path), job_id="job-1")

    writer.emit(
        "file_started",
        file_path="/shared/input/a.pdf",
        filename="a",
        total_pages=3,
    )
    writer.emit(
        "page_done",
        file_path="/shared/input/a.pdf",
        filename="a",
        page_no=1,
        status="success",
    )

    rows = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]

    assert rows[0]["job_id"] == "job-1"
    assert rows[0]["type"] == "file_started"
    assert rows[0]["payload"]["total_pages"] == 3
    assert rows[1]["type"] == "page_done"
    assert rows[1]["payload"]["page_no"] == 1
    assert rows[1]["payload"]["status"] == "success"
    assert "created_at" in rows[1]


def test_null_event_writer_is_noop(tmp_path):
    writer = NullOCREventWriter()
    writer.emit("job_started", job_id="ignored")
    assert list(tmp_path.iterdir()) == []


def test_event_writer_instances_append_valid_jsonl_to_same_file_concurrently(tmp_path):
    event_path = tmp_path / "events" / "job.jsonl"
    writer_count = 4
    records_per_writer = 50
    start_barrier = threading.Barrier(writer_count)
    errors: list[BaseException] = []
    error_lock = threading.Lock()

    def emit_records(writer_index: int) -> None:
        writer = OCREventWriter(
            path=str(event_path),
            job_id=f"job-{writer_index}",
        )
        try:
            start_barrier.wait(timeout=5)
            for record_index in range(records_per_writer):
                writer.emit(
                    "page_done",
                    writer_index=writer_index,
                    record_index=record_index,
                )
        except BaseException as exc:
            with error_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=emit_records, args=(writer_index,))
        for writer_index in range(writer_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)

    lines = event_path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]

    assert len(rows) == writer_count * records_per_writer
    assert all(line.startswith("{") and line.endswith("}") for line in lines)
    assert Counter(row["job_id"] for row in rows) == {
        f"job-{writer_index}": records_per_writer
        for writer_index in range(writer_count)
    }
    assert {
        (row["job_id"], row["type"])
        for row in rows
    } == {
        (f"job-{writer_index}", "page_done")
        for writer_index in range(writer_count)
    }
    assert {
        (row["payload"]["writer_index"], row["payload"]["record_index"])
        for row in rows
    } == {
        (writer_index, record_index)
        for writer_index in range(writer_count)
        for record_index in range(records_per_writer)
    }
