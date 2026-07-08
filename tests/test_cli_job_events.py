import asyncio
import sys
from types import ModuleType
from pathlib import Path

import pytest

import ocr_parser.cli as cli
from ocr_parser.cli import build_parser
from ocr_parser.config import ParserConfig


def test_parser_accepts_job_event_flags():
    args = build_parser().parse_args(
        [
            "--input_dir",
            "/tmp/in",
            "--output_dir",
            "/tmp/out",
            "--job_id",
            "job-123",
            "--job_event_file",
            "/tmp/job-123/events.jsonl",
        ]
    )

    assert args.job_id == "job-123"
    assert args.job_event_file == "/tmp/job-123/events.jsonl"


def test_config_preserves_job_event_fields():
    config = ParserConfig.from_kwargs(
        job_id="job-123",
        job_event_file="/tmp/job-123/events.jsonl",
    )

    assert config.job_id == "job-123"
    assert config.job_event_file == "/tmp/job-123/events.jsonl"


def test_run_emits_job_failed_for_initialize_failure(monkeypatch, tmp_path):
    events = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()

        async def initialize(self):
            raise RuntimeError("init failed")

        async def shutdown(self):
            return None

    args = build_parser().parse_args(
        [
            "--input_dir",
            str(tmp_path / "in"),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )
    monkeypatch.setattr(cli, "_collect_inputs", lambda args: [(Path("/tmp/a.pdf"), Path("."))])

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    with pytest.raises(RuntimeError, match="init failed"):
        asyncio.run(cli._run(args))

    event_types = [item[0] for item in events]
    assert event_types == ["job_started", "job_failed"]
    assert events[-1][1]["error"] == "init failed"


def test_job_event_emit_is_best_effort():
    calls = []

    class FailingEventWriter:
        def emit(self, event_type, **payload):
            raise RuntimeError("sink failed")

    class ParserStub:
        event_writer = FailingEventWriter()

        def _console_write(self, message, level="info"):
            calls.append((level, message))

    cli._emit_job_event(ParserStub(), "job_started", output_dir="/tmp/out")

    assert calls == [("warning", "Failed to emit OCR event job_started: sink failed")]


def test_run_emits_job_failed_when_shutdown_fails(monkeypatch, tmp_path):
    events = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()

        async def initialize(self):
            return None

        async def parse_file(self, *args, **kwargs):
            return [{"status": "success", "error": None}]

        async def shutdown(self):
            raise RuntimeError("shutdown failed")

    args = build_parser().parse_args(
        [
            "--input_dir",
            str(tmp_path / "in"),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )
    monkeypatch.setattr(cli, "_collect_inputs", lambda args: [(Path("/tmp/a.pdf"), Path("."))])

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    with pytest.raises(RuntimeError, match="shutdown failed"):
        asyncio.run(cli._run(args))

    assert [item[0] for item in events] == ["job_started", "job_failed"]
    assert events[-1][1]["error"] == "shutdown failed"


def test_run_emits_job_failed_for_empty_inputs(monkeypatch, tmp_path):
    events = []
    input_dir = tmp_path / "in"
    input_dir.mkdir()

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()

    args = build_parser().parse_args(
        [
            "--input_dir",
            str(input_dir),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )
    monkeypatch.setattr(cli, "_collect_inputs", lambda args: [])

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    result = asyncio.run(cli._run(args))

    assert result == 1
    assert [item[0] for item in events] == ["job_started", "job_failed"]
    assert events[-1][1]["error"] == "No PDF files found."
    assert events[-1][1]["failure_category"] == "input_empty"


def test_run_emits_input_missing_for_missing_input_dir(monkeypatch, tmp_path):
    events = []
    missing_dir = tmp_path / "missing"

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()

    args = build_parser().parse_args(
        [
            "--input_dir",
            str(missing_dir),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )
    monkeypatch.setattr(cli, "_collect_inputs", lambda args: [])

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    result = asyncio.run(cli._run(args))

    assert result == 1
    assert [item[0] for item in events] == ["job_started", "job_failed"]
    assert events[-1][1]["error"] == "No PDF files found."
    assert events[-1][1]["failure_category"] == "input_missing"
