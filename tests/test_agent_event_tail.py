import asyncio
import json

from ocr_platform.agent.runner import read_new_jsonl_events


def test_read_new_jsonl_events_returns_same_offset_for_missing_file(tmp_path):
    missing_file = tmp_path / "missing-events.jsonl"

    offset, records = asyncio.run(read_new_jsonl_events(missing_file, 42))

    assert offset == 42
    assert records == []


def test_read_new_jsonl_events_returns_offset_and_records(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        json.dumps({"type": "job_started", "payload": {}}) + "\n",
        encoding="utf-8",
    )

    offset, records = asyncio.run(read_new_jsonl_events(event_file, 0))

    assert offset > 0
    assert records == [{"type": "job_started", "payload": {}}]

    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "job_done", "payload": {}}) + "\n")

    next_offset, next_records = asyncio.run(read_new_jsonl_events(event_file, offset))

    assert next_offset > offset
    assert next_records == [{"type": "job_done", "payload": {}}]


def test_read_new_jsonl_events_retries_incomplete_trailing_line(tmp_path):
    event_file = tmp_path / "events.jsonl"
    partial = '{"type": "job_started", "payload": {}'
    event_file.write_text(partial, encoding="utf-8")

    offset, records = asyncio.run(read_new_jsonl_events(event_file, 0))

    assert offset == 0
    assert records == []

    with event_file.open("a", encoding="utf-8") as handle:
        handle.write("}\n")

    next_offset, next_records = asyncio.run(read_new_jsonl_events(event_file, offset))

    assert next_offset == len(partial) + 2
    assert next_records == [{"type": "job_started", "payload": {}}]
