import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

import ocr_parser.cli as cli
from ocr_parser.cli import build_parser
from ocr_platform.manifest.models import ManifestItem


def test_collect_manifest_inputs_preserves_relative_parent(tmp_path):
    pdf_root = tmp_path / "pdfs"
    nested = pdf_root / "nested" / "leaf"
    nested.mkdir(parents=True)
    first = pdf_root / "root.pdf"
    second = nested / "child.pdf"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")

    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            [
                ManifestItem(
                    input_path=str(first),
                    relative_path="root.pdf",
                    size_bytes=first.stat().st_size,
                    mtime_ns=first.stat().st_mtime_ns,
                ).to_json_line(),
                "",
                ManifestItem(
                    input_path=str(second),
                    relative_path="nested/leaf/child.pdf",
                    size_bytes=second.stat().st_size,
                    mtime_ns=second.stat().st_mtime_ns,
                ).to_json_line(),
            ]
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    assert cli._collect_inputs(args) == [
        (first.resolve(), Path(".")),
        (second.resolve(), Path("nested/leaf")),
    ]


def test_parser_accepts_manifest_input_and_root(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")

    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--input_root",
            str(tmp_path / "pdfs"),
        ]
    )

    assert args.input_manifest == str(manifest)
    assert args.input_root == str(tmp_path / "pdfs")


def test_classify_empty_inputs_reports_missing_manifest(tmp_path):
    args = build_parser().parse_args(["--input_manifest", str(tmp_path / "missing.jsonl")])

    assert cli._classify_empty_inputs(args) == "input_missing"
    assert cli._collect_inputs(args) == []


@pytest.mark.parametrize("relative_path", ["/outside.pdf", "nested/../outside.pdf"])
def test_collect_manifest_inputs_rejects_unsafe_relative_path(tmp_path, relative_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(pdf),
            relative_path=relative_path,
            size_bytes=pdf.stat().st_size,
            mtime_ns=pdf.stat().st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(ValueError, match=r"manifest .* line 1 .* relative_path"):
        cli._collect_inputs(args)


@pytest.mark.parametrize("relative_path", [".", "nested", "nested/", "nested/file.txt"])
def test_collect_manifest_inputs_rejects_relative_path_without_pdf_filename(tmp_path, relative_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    payload = {
        "input_path": str(pdf),
        "relative_path": relative_path,
        "size_bytes": pdf.stat().st_size,
        "mtime_ns": pdf.stat().st_mtime_ns,
    }
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(
        ValueError,
        match=r"manifest .* line 1 .*relative_path must point to a PDF file",
    ):
        cli._collect_inputs(args)


def test_collect_manifest_inputs_rejects_backslash_relative_path(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    payload = {
        "input_path": str(pdf),
        "relative_path": "nested\\input.pdf",
        "size_bytes": pdf.stat().st_size,
        "mtime_ns": pdf.stat().st_mtime_ns,
    }
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(
        ValueError,
        match=r"manifest .* line 1 .*relative_path must use POSIX '/' separators",
    ):
        cli._collect_inputs(args)


def test_collect_manifest_inputs_rejects_relative_input_path(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    payload = {
        "input_path": "relative/input.pdf",
        "relative_path": "input.pdf",
        "size_bytes": pdf.stat().st_size,
        "mtime_ns": pdf.stat().st_mtime_ns,
    }
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(
        ValueError,
        match=r"manifest .* line 1 .*input_path must be absolute",
    ):
        cli._collect_inputs(args)


def test_collect_manifest_inputs_reports_malformed_row_with_line_context(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"input_path": "/tmp/a.pdf", "relative_path": "a.pdf"}\n', encoding="utf-8")
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(ValueError, match=r"manifest .* line 1"):
        cli._collect_inputs(args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_path", ""),
        ("input_path", "   "),
        ("input_path", None),
        ("relative_path", ""),
        ("relative_path", "   "),
        ("relative_path", None),
    ],
)
def test_collect_manifest_inputs_rejects_blank_path_fields(tmp_path, field, value):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    stat = pdf.stat()
    payload = {
        "input_path": str(pdf),
        "relative_path": "input.pdf",
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    payload[field] = value
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(
        ValueError,
        match=rf"manifest .* line 1: {field} must be a non-empty string",
    ):
        cli._collect_inputs(args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size_bytes", -1),
        ("mtime_ns", -1),
        ("size_bytes", "12"),
        ("mtime_ns", 12.5),
    ],
)
def test_collect_manifest_inputs_rejects_invalid_snapshot_numbers(tmp_path, field, value):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    stat = pdf.stat()
    payload = {
        "input_path": str(pdf),
        "relative_path": "input.pdf",
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    payload[field] = value
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(
        ValueError,
        match=rf"manifest .* line 1: {field} must be a non-negative integer",
    ):
        cli._collect_inputs(args)


def test_collect_manifest_inputs_rejects_duplicate_relative_path(tmp_path):
    first = tmp_path / "a" / "same.pdf"
    second = tmp_path / "b" / "same.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            [
                ManifestItem(
                    input_path=str(first),
                    relative_path="same.pdf",
                    size_bytes=first.stat().st_size,
                    mtime_ns=first.stat().st_mtime_ns,
                ).to_json_line(),
                ManifestItem(
                    input_path=str(second),
                    relative_path="same.pdf",
                    size_bytes=second.stat().st_size,
                    mtime_ns=second.stat().st_mtime_ns,
                ).to_json_line(),
            ]
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--input_manifest", str(manifest)])

    with pytest.raises(ValueError, match=r"duplicate relative_path.*same\.pdf.*line 2"):
        cli._collect_inputs(args)


def test_run_emits_input_missing_for_missing_input_manifest(monkeypatch, tmp_path):
    events = []
    missing_manifest = tmp_path / "missing.jsonl"

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()

    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(missing_manifest),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)

    result = asyncio.run(cli._run(args))

    assert result == 1
    assert [item[0] for item in events] == ["job_started", "job_failed"]
    assert events[-1][1]["error"] == "No PDF files found."
    assert events[-1][1]["failure_category"] == "input_missing"


def test_parser_rejects_rename_with_input_manifest(tmp_path):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_manifest", str(tmp_path / "manifest.jsonl"), "--rename", "renamed"])

    assert exc.value.code == 2


def test_parser_rejects_flatten_output_with_input_manifest(tmp_path):
    parser = build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--input_manifest", str(tmp_path / "manifest.jsonl"), "--flatten_output"])

    assert exc.value.code == 2


def test_run_manifest_preserves_relative_output_dirs_for_duplicate_basenames(monkeypatch, tmp_path):
    first_dir = tmp_path / "input" / "a"
    second_dir = tmp_path / "input" / "b"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    first = first_dir / "same.pdf"
    second = second_dir / "same.pdf"
    first.write_bytes(b"%PDF-1.4\n")
    second.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        "\n".join(
            [
                ManifestItem(
                    input_path=str(first),
                    relative_path="a/same.pdf",
                    size_bytes=first.stat().st_size,
                    mtime_ns=first.stat().st_mtime_ns,
                ).to_json_line(),
                ManifestItem(
                    input_path=str(second),
                    relative_path="b/same.pdf",
                    size_bytes=second.stat().st_size,
                    mtime_ns=second.stat().st_mtime_ns,
                ).to_json_line(),
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = None
            self.enable_api_autotune = False

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            calls.append((Path(input_path), Path(output_dir)))
            return [{"status": "success", "error": None}]

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert calls == [
        (first.resolve(), tmp_path / "out" / "a"),
        (second.resolve(), tmp_path / "out" / "b"),
    ]


def test_run_manifest_uses_relative_path_stem_for_output_filename(monkeypatch, tmp_path):
    input_file = tmp_path / "input" / "source-name.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="nested/canonical-name.pdf",
            size_bytes=input_file.stat().st_size,
            mtime_ns=input_file.stat().st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    calls = []

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = None
            self.enable_api_autotune = False

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            calls.append(
                (
                    Path(input_path),
                    Path(output_dir),
                    kwargs.get("rename_to"),
                    kwargs.get("manifest_input_size_bytes"),
                    kwargs.get("manifest_input_mtime_ns"),
                    kwargs.get("manifest_relative_path"),
                )
            )
            return [{"status": "success", "error": None}]

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    stat = input_file.stat()
    assert calls == [
        (
            input_file.resolve(),
            tmp_path / "out" / "nested",
            "canonical-name",
            stat.st_size,
            stat.st_mtime_ns,
            "nested/canonical-name.pdf",
        )
    ]


def test_run_manifest_rejects_changed_input_before_ocr(monkeypatch, tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    original_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="input.pdf",
            size_bytes=original_stat.st_size,
            mtime_ns=original_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    input_file.write_bytes(b"%PDF-1.4 changed\n")
    events = []
    parse_calls = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()
            self.enable_api_autotune = False

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append(input_path)
            return [{"status": "success", "error": None}]

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == []
    assert events[-2][0] == "file_failed"
    assert events[-2][1]["failure_category"] == "input_changed"
    assert events[-1][0] == "job_failed"
    sidecar = tmp_path / "out" / "input" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "input_changed"
    assert payload["error_type"] == "InputChanged"
    assert payload["file_path"] == str(input_file.resolve())
    assert payload["input_size_bytes"] == input_file.stat().st_size
    assert payload["input_mtime_ns"] == input_file.stat().st_mtime_ns
    assert payload["manifest_input_size_bytes"] == original_stat.st_size
    assert payload["manifest_input_mtime_ns"] == original_stat.st_mtime_ns


def test_run_manifest_promotes_file_failure_category_to_job_failed_event(monkeypatch, tmp_path):
    input_file = tmp_path / "input.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    original_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="input.pdf",
            size_bytes=original_stat.st_size,
            mtime_ns=original_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    input_file.write_bytes(b"%PDF-1.4 changed\n")
    events = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()
            self.enable_api_autotune = False

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            raise AssertionError("changed manifest inputs must fail before OCR")

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert events[-1][0] == "job_failed"
    assert events[-1][1]["failure_category"] == "input_changed"
    assert "input file changed since manifest snapshot" in events[-1][1]["error"]


def test_run_manifest_requires_sidecar_input_snapshot_before_skip(monkeypatch, tmp_path):
    missing_input = tmp_path / "input" / "done.pdf"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(missing_input),
            relative_path="nested/done.pdf",
            size_bytes=123,
            mtime_ns=456,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    save_dir = output_dir / "nested" / "done"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "done.md"
    md_path.write_text("already done", encoding="utf-8")
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
    events = []
    parse_calls = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append((input_path, output_dir))
            return [{"status": "success", "error": None}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == []
    assert [event_type for event_type, _payload in events] == [
        "job_started",
        "file_failed",
        "job_failed",
    ]
    assert events[1][1]["failure_category"] == "input_missing"


def test_run_manifest_reprocesses_when_success_sidecar_input_snapshot_differs(monkeypatch, tmp_path):
    input_file = tmp_path / "input" / "done.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    manifest_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="nested/done.pdf",
            size_bytes=manifest_stat.st_size,
            mtime_ns=manifest_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    save_dir = output_dir / "nested" / "done"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "done.md"
    md_path.write_text("old output for a different input snapshot", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
                "input_size_bytes": manifest_stat.st_size + 100,
                "input_mtime_ns": manifest_stat.st_mtime_ns,
            }
        ),
        encoding="utf-8",
    )
    parse_calls = []

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = None
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append((input_path, output_dir))
            return [{"status": "success", "error": None}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == [(str(input_file.resolve()), str(output_dir / "nested"))]


def test_run_manifest_reprocesses_when_success_sidecar_relative_path_differs(monkeypatch, tmp_path):
    input_file = tmp_path / "input" / "done.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    manifest_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="nested/done.pdf",
            size_bytes=manifest_stat.st_size,
            mtime_ns=manifest_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    save_dir = output_dir / "nested" / "done"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "done.md"
    md_path.write_text("old output copied from another manifest key", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
                "input_size_bytes": manifest_stat.st_size,
                "input_mtime_ns": manifest_stat.st_mtime_ns,
                "manifest_relative_path": "other/done.pdf",
            }
        ),
        encoding="utf-8",
    )
    parse_calls = []

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = None
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append((input_path, output_dir, kwargs.get("manifest_relative_path")))
            return [{"status": "success", "error": None}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == [
        (str(input_file.resolve()), str(output_dir / "nested"), "nested/done.pdf")
    ]


def test_run_manifest_skip_reports_existing_output_path_for_shard_rerun(monkeypatch, tmp_path):
    input_file = tmp_path / "input" / "done.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    manifest_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="nested/done.pdf",
            size_bytes=manifest_stat.st_size,
            mtime_ns=manifest_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    save_dir = output_dir / "nested" / "done"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "done.md"
    md_path.write_text("already done", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
                "input_size_bytes": manifest_stat.st_size,
                "input_mtime_ns": manifest_stat.st_mtime_ns,
            }
        ),
        encoding="utf-8",
    )
    events = []
    parse_calls = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append((input_path, output_dir))
            return [{"status": "success", "error": None}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == []
    assert [event_type for event_type, _payload in events] == [
        "job_started",
        "file_done",
        "job_done",
    ]
    assert events[1][1]["status"] == "skipped"
    assert events[1][1]["output_path"] == str(md_path)


def test_run_manifest_rejects_changed_input_even_when_success_sidecar_matches_manifest(
    monkeypatch, tmp_path
):
    input_file = tmp_path / "input" / "done.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    manifest_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="nested/done.pdf",
            size_bytes=manifest_stat.st_size,
            mtime_ns=manifest_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    save_dir = output_dir / "nested" / "done"
    save_dir.mkdir(parents=True)
    md_path = save_dir / "done.md"
    md_path.write_text("old output for manifest snapshot", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success",
                "output_md_path": str(md_path),
                "artifacts": [{"kind": "document_markdown", "path": str(md_path)}],
                "input_size_bytes": manifest_stat.st_size,
                "input_mtime_ns": manifest_stat.st_mtime_ns,
            }
        ),
        encoding="utf-8",
    )
    input_file.write_bytes(b"%PDF-1.4 changed after manifest\n")
    events = []
    parse_calls = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append((input_path, output_dir))
            return [{"status": "success", "error": None}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == []
    assert [event_type for event_type, _payload in events] == [
        "job_started",
        "file_failed",
        "job_failed",
    ]
    assert events[1][1]["failure_category"] == "input_changed"
    payload = json.loads((save_dir / ".ocr_status.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "input_changed"
    assert payload["manifest_input_size_bytes"] == manifest_stat.st_size
    assert payload["manifest_input_mtime_ns"] == manifest_stat.st_mtime_ns


def test_run_manifest_requires_success_sidecar_before_skip(monkeypatch, tmp_path):
    missing_input = tmp_path / "input" / "legacy.pdf"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(missing_input),
            relative_path="nested/legacy.pdf",
            size_bytes=123,
            mtime_ns=456,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    save_dir = output_dir / "nested" / "legacy"
    save_dir.mkdir(parents=True)
    (save_dir / "legacy.md").write_text("legacy output without status sidecar", encoding="utf-8")
    events = []
    parse_calls = []

    class EventRecorder:
        def emit(self, event_type, **payload):
            events.append((event_type, payload))

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = EventRecorder()
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append((input_path, output_dir))
            return [{"status": "success", "error": None}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == []
    assert events[-2][0] == "file_failed"
    assert events[-2][1]["failure_category"] == "input_missing"
    sidecar = save_dir / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "input_missing"
    assert payload["error_type"] == "InputMissing"


def test_run_manifest_writes_sidecar_for_missing_input_before_ocr(monkeypatch, tmp_path):
    missing_file = tmp_path / "missing.pdf"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(missing_file),
            relative_path="nested/missing.pdf",
            size_bytes=10,
            mtime_ns=20,
        ).to_json_line(),
        encoding="utf-8",
    )
    parse_calls = []

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = None
            self.enable_api_autotune = False

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            parse_calls.append(input_path)
            return [{"status": "success", "error": None}]

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(tmp_path / "out"),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_calls == []
    sidecar = tmp_path / "out" / "nested" / "missing" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "input_missing"
    assert payload["file_path"] == str(missing_file.resolve())


def test_run_manifest_cleans_legacy_output_without_sidecar_before_parser_resume(
    monkeypatch, tmp_path
):
    input_file = tmp_path / "input" / "legacy.pdf"
    input_file.parent.mkdir()
    input_file.write_bytes(b"%PDF-1.4\n")
    manifest_stat = input_file.stat()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        ManifestItem(
            input_path=str(input_file),
            relative_path="nested/legacy.pdf",
            size_bytes=manifest_stat.st_size,
            mtime_ns=manifest_stat.st_mtime_ns,
        ).to_json_line(),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    legacy_save_dir = output_dir / "nested" / "legacy"
    legacy_save_dir.mkdir(parents=True)
    legacy_md = legacy_save_dir / "legacy.md"
    legacy_md.write_text("legacy output without status sidecar", encoding="utf-8")
    parse_modes = []

    class ParserStub:
        SUCCESS_STATUSES = {"success"}

        def __init__(self, **kwargs):
            self.event_writer = None
            self.enable_api_autotune = False
            self.enable_resume = kwargs.get("enable_resume", True)
            self.force_reprocess = kwargs.get("force_reprocess", False)

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def parse_file(self, input_path, output_dir, **kwargs):
            md_path = Path(output_dir) / "legacy" / "legacy.md"
            if md_path.exists():
                parse_modes.append("inner_resume_skip")
                return [{"status": "success", "error": None, "skipped": True}]
            parse_modes.append("reprocessed")
            return [{"status": "success", "error": None, "skipped": False}]

        def _console_write(self, message, level="info"):
            pass

    parser_module = ModuleType("ocr_parser.parser")
    parser_module.DotsOCRParser = ParserStub
    monkeypatch.setitem(sys.modules, "ocr_parser.parser", parser_module)
    args = build_parser().parse_args(
        [
            "--input_manifest",
            str(manifest),
            "--output_dir",
            str(output_dir),
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == 0
    assert parse_modes == ["reprocessed"]
