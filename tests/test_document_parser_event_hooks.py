import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType

fitz_stub = ModuleType("fitz")
fitz_stub.Document = object
fitz_stub.open = lambda *args, **kwargs: None
sys.modules.setdefault("fitz", fitz_stub)

prometheus_stub = ModuleType("prometheus_client")


class _MetricStub:
    def __init__(self, *args, **kwargs):
        pass

    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        return None

    def dec(self, *args, **kwargs):
        return None


prometheus_stub.Counter = _MetricStub
prometheus_stub.Gauge = _MetricStub
prometheus_stub.Histogram = _MetricStub
prometheus_stub.start_http_server = lambda *args, **kwargs: None
sys.modules.setdefault("prometheus_client", prometheus_stub)

aiofiles_stub = ModuleType("aiofiles")


class _AsyncFile:
    def __init__(self, path, mode="r", encoding=None):
        self.path = path
        self.mode = mode
        self.encoding = encoding
        self.handle = None

    async def __aenter__(self):
        self.handle = open(self.path, self.mode, encoding=self.encoding)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.handle.close()

    async def write(self, text):
        return self.handle.write(text)

    async def read(self):
        return self.handle.read()


def _aiofiles_open(path, mode="r", encoding=None):
    return _AsyncFile(path, mode=mode, encoding=encoding)


aiofiles_stub.open = _aiofiles_open
sys.modules.setdefault("aiofiles", aiofiles_stub)

format_transformer_stub = ModuleType("dots_ocr.utils.format_transformer_v3")
format_transformer_stub.ensure_section_headers_have_markers = lambda *args, **kwargs: None
format_transformer_stub.filter_blocks_by_keywords = lambda cells, *args, **kwargs: cells
format_transformer_stub.layoutjson2md_full_robust = lambda *args, **kwargs: ""
format_transformer_stub.layoutjson2md_simple_extract = lambda *args, **kwargs: ""
format_transformer_stub.normalize_superscript_citations = lambda text, **kwargs: text
format_transformer_stub.unescape_basic_sequences = lambda text, **kwargs: text
sys.modules.setdefault("dots_ocr.utils.format_transformer_v3", format_transformer_stub)


pdf_worker_stub = ModuleType("ocr_parser.domain.pdf_worker")
pdf_worker_stub.process_pdf_page_worker = lambda *args, **kwargs: None
sys.modules.setdefault("ocr_parser.domain.pdf_worker", pdf_worker_stub)

import ocr_parser.pipeline.document_parser as document_parser
from ocr_parser.pipeline.document_parser import parse_file


class EventRecorder:
    def __init__(self):
        self.events = []

    def emit(self, event_type, **payload):
        self.events.append((event_type, payload))


class ParserStub:
    output_dir = "/tmp/out"
    enable_resume = False
    force_reprocess = False
    SUCCESS_STATUSES = {"success", "success_fallback_text", "success_fallback_image", "skipped_blank"}

    def __init__(self):
        self.event_writer = EventRecorder()
        self.calls = []

    def _console_write(self, message, level="info"):
        self.calls.append((level, message))

    async def _flush_document_page_json(self, save_dir):
        self.calls.append(("flush", save_dir))


def test_parse_file_emits_file_failed_for_unsupported_extension(tmp_path):
    input_file = tmp_path / "sample.txt"
    input_file.write_text("not a pdf", encoding="utf-8")
    parser = ParserStub()

    result = asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert result[0]["error"].startswith("File extension")
    event_types = [item[0] for item in parser.event_writer.events]
    assert "file_started" in event_types
    assert "file_failed" in event_types
    failed_payload = parser.event_writer.events[-1][1]
    assert failed_payload["file_path"] == str(input_file)
    assert failed_payload["filename"] == "sample"


def test_parse_file_ignores_event_writer_failure_for_unsupported_extension(tmp_path):
    class FailingEventWriter:
        def emit(self, event_type, **payload):
            raise RuntimeError("event sink unavailable")

    input_file = tmp_path / "sample.txt"
    input_file.write_text("not a pdf", encoding="utf-8")
    parser = ParserStub()
    parser.event_writer = FailingEventWriter()

    result = asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert result[0]["error"].startswith("File extension")
    assert any(
        level == "warning" and "Failed to emit OCR event" in message
        for level, message in parser.calls
    )


def test_parse_file_emits_failed_when_later_result_row_fails(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success",
                "error": None,
                "filename": "sample",
            },
            {
                "page_no": 2,
                "file_path": str(input_file),
                "status": "error",
                "error": "page failed",
                "filename": "sample",
            },
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    result = asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert result[0]["status"] == "success"
    assert parser.event_writer.events[-1][0] == "file_failed"
    assert parser.event_writer.events[-1][1]["error"] == "page failed"


def test_parse_file_force_reprocess_cleans_existing_output_before_ocr(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    output_dir = tmp_path / "out"
    save_dir = output_dir / "sample"
    save_dir.mkdir(parents=True)
    stale_artifact = save_dir / "stale.partial.json"
    stale_artifact.write_text('{"partial": true}', encoding="utf-8")
    parser = ParserStub()
    parser.force_reprocess = True

    async def parse_pdf_stub(_parser, _input_path, _filename, _prompt_mode, active_save_dir, **kwargs):
        assert Path(active_save_dir) == save_dir
        assert not stale_artifact.exists()
        md_path = Path(active_save_dir) / "sample.md"
        md_path.write_text("fresh output", encoding="utf-8")
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success",
                "error": None,
                "filename": "sample",
                "output_md_path": str(md_path),
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    result = asyncio.run(parse_file(parser, str(input_file), output_dir=str(output_dir)))

    assert result[0]["status"] == "success"
    assert not stale_artifact.exists()
    assert (save_dir / "sample.md").read_text(encoding="utf-8") == "fresh output"


def test_parse_file_classifies_page_error_in_failed_event(monkeypatch, tmp_path):
    input_file = tmp_path / "unreachable.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "error",
                "error": "Connection refused while connecting to model server",
                "filename": "unreachable",
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    failed_payload = parser.event_writer.events[-1][1]
    assert failed_payload["failure_category"] == "model_unreachable"


def test_parse_file_done_event_includes_runtime_snapshot(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()
    parser.runtime_snapshot = {
        "api_limit": 80,
        "api_inflight_peak": 12,
        "api_call_count": 24,
    }

    def get_runtime_snapshot():
        return parser.runtime_snapshot

    parser.get_runtime_snapshot = get_runtime_snapshot

    async def parse_pdf_stub(*args, **kwargs):
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success",
                "error": None,
                "filename": "sample",
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert parser.event_writer.events[-1][0] == "file_done"
    assert parser.event_writer.events[-1][1]["runtime"] == parser.runtime_snapshot


def test_parse_file_done_event_includes_aggregated_execution_trace(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success_fallback_text",
                "error": None,
                "filename": "sample",
                "stages": [
                    {"stage": "layout", "status": "failed", "failure_category": "model_unreachable"},
                    {"stage": "single_stage_ocr", "status": "success"},
                ],
                "fallback": {
                    "used": True,
                    "reason": "layout_unavailable",
                    "source_stage": "layout",
                },
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    payload = parser.event_writer.events[-1][1]
    assert payload["fallback"] == {
        "used": True,
        "reason": "layout_unavailable",
        "source_stage": "layout",
    }
    assert payload["stages"][0]["page_no"] == 1


def test_parse_file_writes_success_status_sidecar(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()
    parser.engine = "dotsocr"
    parser.model_name = "DotsOCR"
    parser.ip = "127.0.0.1"
    parser.port = 13080
    parser.page_concurrency = 80
    parser.file_concurrency = 8
    parser.api_key = "secret-should-not-leak"

    async def parse_pdf_stub(*args, **kwargs):
        save_dir = args[4]
        output_md = f"{save_dir}/sample.md"
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success",
                "error": None,
                "filename": "sample",
                "output_md_path": output_md,
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    sidecar = tmp_path / "out" / "sample" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["file_path"] == str(input_file)
    assert payload["filename"] == "sample"
    assert payload["output_md_path"].endswith("sample.md")
    stat = input_file.stat()
    assert payload["input_size_bytes"] == stat.st_size
    assert payload["input_mtime_ns"] == stat.st_mtime_ns
    assert payload["duration_seconds"] >= 0
    assert payload["failure_category"] is None
    assert payload["model_config"] == {
        "engine": "dotsocr",
        "model_name": "DotsOCR",
        "ip": "127.0.0.1",
        "port": 13080,
        "page_concurrency": 80,
        "file_concurrency": 8,
    }
    assert "api_key" not in json.dumps(payload["model_config"])


def test_parse_file_writes_manifest_snapshot_to_success_sidecar(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF-1.4\n")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        output_md = str(tmp_path / "out" / "sample" / "sample.md")
        Path(output_md).parent.mkdir(parents=True, exist_ok=True)
        Path(output_md).write_text("ok", encoding="utf-8")
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success",
                "error": None,
                "filename": "sample",
                "output_md_path": output_md,
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(
        parse_file(
            parser,
            str(input_file),
            output_dir=str(tmp_path / "out"),
            manifest_input_size_bytes=123,
            manifest_input_mtime_ns=456,
        )
    )

    sidecar = tmp_path / "out" / "sample" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["manifest_input_size_bytes"] == 123
    assert payload["manifest_input_mtime_ns"] == 456


def test_parse_file_writes_failed_status_sidecar_with_failure_category(tmp_path):
    input_file = tmp_path / "sample.txt"
    input_file.write_text("not a pdf", encoding="utf-8")
    parser = ParserStub()
    parser.engine = "dotsocr"

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    sidecar = tmp_path / "out" / "sample" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "input_invalid"
    assert payload["duration_seconds"] >= 0
    assert payload["model_config"] == {"engine": "dotsocr"}


def test_parse_file_classifies_timeout_exception_in_event_and_sidecar(monkeypatch, tmp_path):
    input_file = tmp_path / "timeout.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        raise TimeoutError("model request timed out after 180s")

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    failed_event = parser.event_writer.events[-1]
    assert failed_event[0] == "file_failed"
    assert failed_event[1]["failure_category"] == "api_timeout"
    sidecar = tmp_path / "out" / "timeout" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "api_timeout"
    assert payload["error_type"] == "TimeoutError"


def test_parse_file_copies_page_error_type_to_failed_sidecar(monkeypatch, tmp_path):
    input_file = tmp_path / "page-error.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "error",
                "error": "model response json was invalid",
                "error_type": "NonStandardModelOutputError",
                "filename": "page-error",
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    sidecar = tmp_path / "out" / "page-error" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failure_category"] == "model_output_invalid"
    assert payload["error_type"] == "NonStandardModelOutputError"


def test_parse_file_classifies_no_space_left_as_output_unwritable(monkeypatch, tmp_path):
    input_file = tmp_path / "full-disk.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        raise OSError("[Errno 28] No space left on device")

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    failed_event = parser.event_writer.events[-1]
    assert failed_event[0] == "file_failed"
    assert failed_event[1]["failure_category"] == "output_unwritable"
    sidecar = tmp_path / "out" / "full-disk" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["failure_category"] == "output_unwritable"


def test_parse_file_classifies_cuda_out_of_memory_as_resource_exhausted(monkeypatch, tmp_path):
    input_file = tmp_path / "oom.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    failed_event = parser.event_writer.events[-1]
    assert failed_event[0] == "file_failed"
    assert failed_event[1]["failure_category"] == "resource_exhausted"
    sidecar = tmp_path / "out" / "oom" / ".ocr_status.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["failure_category"] == "resource_exhausted"


def test_parse_file_skips_only_when_success_sidecar_exists(monkeypatch, tmp_path):
    input_file = tmp_path / "sample.pdf"
    input_file.write_bytes(b"%PDF")
    output_dir = tmp_path / "out"
    save_dir = output_dir / "sample"
    save_dir.mkdir(parents=True)
    (save_dir / "sample.md").write_text("done", encoding="utf-8")
    (save_dir / ".ocr_status.json").write_text(
        json.dumps({"status": "running", "file_path": str(input_file), "filename": "sample"}),
        encoding="utf-8",
    )
    parser = ParserStub()
    parser.enable_resume = True
    parse_calls = []

    async def parse_pdf_stub(*args, **kwargs):
        parse_calls.append(args)
        save_dir = args[4]
        output_md = f"{save_dir}/sample.md"
        import pathlib
        pathlib.Path(output_md).write_text("done", encoding="utf-8")
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "success",
                "error": None,
                "filename": "sample",
                "output_md_path": output_md,
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(output_dir)))

    assert len(parse_calls) == 1
    assert parser.event_writer.events[-1][0] == "file_done"

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(output_dir)))

    assert len(parse_calls) == 1
    assert parser.event_writer.events[-1][1]["status"] == "skipped"


def test_parse_file_emits_failed_for_empty_results(monkeypatch, tmp_path):
    input_file = tmp_path / "empty.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return []

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    result = asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert result == []
    assert parser.event_writer.events[-1][0] == "file_failed"
    assert parser.event_writer.events[-1][1]["error"] == "No content could be generated"


def test_parse_file_emits_failed_for_missing_status_row(monkeypatch, tmp_path):
    input_file = tmp_path / "missing-status.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return [{"page_no": 1, "file_path": str(input_file), "filename": "missing-status"}]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert parser.event_writer.events[-1][0] == "file_failed"
    assert parser.event_writer.events[-1][1]["error"] == "Missing page status"


def test_parse_file_emits_failed_for_all_skipped_blank_rows(monkeypatch, tmp_path):
    input_file = tmp_path / "blank.pdf"
    input_file.write_bytes(b"%PDF")
    parser = ParserStub()

    async def parse_pdf_stub(*args, **kwargs):
        return [
            {
                "page_no": 1,
                "file_path": str(input_file),
                "status": "skipped_blank",
                "error": None,
                "filename": "blank",
            }
        ]

    monkeypatch.setattr(document_parser, "parse_pdf", parse_pdf_stub)

    asyncio.run(parse_file(parser, str(input_file), output_dir=str(tmp_path / "out")))

    assert parser.event_writer.events[-1][0] == "file_failed"
    assert parser.event_writer.events[-1][1]["error"] == "No content could be generated"
