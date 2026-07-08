import asyncio
import sys
from pathlib import Path
from types import ModuleType

import ocr_parser.cli as cli
from ocr_parser.cli import build_parser


def test_run_emits_job_failed_when_concurrent_file_fails(monkeypatch, tmp_path):
    events = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()

        async def initialize(self):
            return None

        async def parse_file(self, input_path, **kwargs):
            if Path(input_path).name == "b.pdf":
                return [{"status": "failed", "error": "ocr failed"}]
            return [{"status": "success", "error": None}]

        async def shutdown(self):
            return None

    args = build_parser().parse_args(
        [
            "--input_dir",
            str(tmp_path / "in"),
            "--output_dir",
            str(tmp_path / "out"),
            "--file_concurrency",
            "2",
        ]
    )
    monkeypatch.setattr(
        cli,
        "_collect_inputs",
        lambda args: [
            (Path("/tmp/a.pdf"), Path(".")),
            (Path("/tmp/b.pdf"), Path(".")),
        ],
    )

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert [item[0] for item in events] == ["job_started", "job_failed"]


def test_run_processes_input_dir_files_with_bounded_file_concurrency(monkeypatch, tmp_path):
    active = 0
    max_active = 0
    parsed = []

    class ParserStub:
        def __init__(self, **kwargs):
            pass

        async def initialize(self):
            return None

        async def parse_file(self, input_path, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            parsed.append(Path(input_path).name)
            return [{"status": "success", "error": None}]

        async def shutdown(self):
            return None

    args = build_parser().parse_args(
        [
            "--input_dir",
            str(tmp_path / "in"),
            "--output_dir",
            str(tmp_path / "out"),
            "--file_concurrency",
            "2",
        ]
    )
    monkeypatch.setattr(
        cli,
        "_collect_inputs",
        lambda args: [
            (Path("/tmp/a.pdf"), Path(".")),
            (Path("/tmp/b.pdf"), Path(".")),
            (Path("/tmp/c.pdf"), Path(".")),
        ],
    )

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert sorted(parsed) == ["a.pdf", "b.pdf", "c.pdf"]
    assert max_active == 2


def test_run_keeps_input_dir_serial_by_default(monkeypatch, tmp_path):
    active = 0
    max_active = 0

    class ParserStub:
        def __init__(self, **kwargs):
            pass

        async def initialize(self):
            return None

        async def parse_file(self, *_args, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return [{"status": "success", "error": None}]

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
    monkeypatch.setattr(
        cli,
        "_collect_inputs",
        lambda args: [
            (Path("/tmp/a.pdf"), Path(".")),
            (Path("/tmp/b.pdf"), Path(".")),
        ],
    )

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    assert asyncio.run(cli._run(args)) == 0
    assert max_active == 1
