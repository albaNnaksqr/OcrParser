"""Tests for PaddleOCRVLEngine (two-stage layout+VLM pipeline)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from ocr_parser.engines.paddleocr_vl import PaddleOCRVLEngine, _looks_like_otsl
from ocr_parser.runtime import get_runtime_snapshot


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_vlm_client(text: str):
    choice = SimpleNamespace(message=SimpleNamespace(content=text))
    completion = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=completion)
    return client


class FakeMonitor:
    def record_inference_time(self, _): pass
    def record_error(self, _): pass
    def record_retry(self, _): pass


class FakeParser:
    model_name = "paddleocr-vl"
    temperature = 0.1
    top_p = 0.9
    max_completion_tokens = 512
    max_retries = 1
    retry_delay = 0.0
    layout_detection_url = "http://localhost:30002"
    paddle_layout_concurrency = 0
    paddle_block_backpressure_high_watermark = 0
    paddle_block_backpressure_low_watermark = 0

    def __init__(self, vlm_response: str = "recognized text"):
        self.client = _make_vlm_client(vlm_response)
        self.monitor = FakeMonitor()

    def _is_transient_inference_error(self, exc) -> bool:
        return False


def _make_page_data(tmp_path, page_no: int = 1) -> dict:
    img_path = tmp_path / "page.jpg"
    Image.new("RGB", (400, 600), color=(255, 255, 255)).save(str(img_path))
    return {
        "page_idx": 0,
        "original_page_num": page_no,
        "processed_image_path": str(img_path),
        "save_dir": str(tmp_path),
    }


def _fake_layout_response(boxes):
    """Build a mock httpx response returning a box list."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"boxes": boxes}
    return resp


# ── unit: _looks_like_otsl ────────────────────────────────────────────────────

def test_looks_like_otsl_positive():
    assert _looks_like_otsl("<fcel>A<lcel><nl>")

def test_looks_like_otsl_negative():
    assert not _looks_like_otsl("<table><tr><td>A</td></tr></table>")
    assert not _looks_like_otsl("plain text paragraph")


# ── unit: _assemble_markdown ──────────────────────────────────────────────────

def test_assemble_discards_header_footer():
    engine = PaddleOCRVLEngine(FakeParser())
    blocks = [
        {"label": "header",    "content": "Company Inc."},
        {"label": "doc_title", "content": "Annual Report"},
        {"label": "footer",    "content": "Page 1"},
    ]
    md = engine._assemble_markdown(blocks)
    assert "Company Inc." not in md
    assert "Annual Report" in md
    assert "Page 1" not in md


def test_assemble_title_levels():
    engine = PaddleOCRVLEngine(FakeParser())
    blocks = [
        {"label": "doc_title",       "content": "Main Title"},
        {"label": "paragraph_title", "content": "Sub Section"},
        {"label": "text",            "content": "Body text."},
    ]
    md = engine._assemble_markdown(blocks)
    assert md.startswith("# Main Title")
    assert "## Sub Section" in md
    assert "Body text." in md


def test_assemble_display_formula_wrapped():
    engine = PaddleOCRVLEngine(FakeParser())
    blocks = [{"label": "display_formula", "content": r"E = mc^2"}]
    md = engine._assemble_markdown(blocks)
    assert md == "$$\nE = mc^2\n$$"


def test_assemble_inline_formula_wrapped():
    engine = PaddleOCRVLEngine(FakeParser())
    blocks = [{"label": "inline_formula", "content": r"\alpha + \beta"}]
    md = engine._assemble_markdown(blocks)
    assert md == r"$\alpha + \beta$"


def test_assemble_table_otsl_converted_to_html():
    engine = PaddleOCRVLEngine(FakeParser())
    otsl = "<fcel>Name<lcel><nl><fcel>Alice<lcel><nl>"
    blocks = [{"label": "table", "content": otsl}]
    md = engine._assemble_markdown(blocks)
    assert "<table>" in md
    assert "<td" in md


def test_assemble_table_already_html_passthrough():
    engine = PaddleOCRVLEngine(FakeParser())
    html = "<table><tr><td>X</td></tr></table>"
    blocks = [{"label": "table", "content": html}]
    md = engine._assemble_markdown(blocks)
    assert md == html


def test_assemble_images_skipped():
    engine = PaddleOCRVLEngine(FakeParser())
    blocks = [
        {"label": "image", "content": "some crop"},
        {"label": "chart", "content": "chart content"},
        {"label": "text",  "content": "real text"},
    ]
    md = engine._assemble_markdown(blocks)
    assert "some crop" not in md
    assert "chart content" not in md
    assert "real text" in md


# ── integration: process_page with mocked services ───────────────────────────

def _patch_layout(boxes):
    """Context manager that stubs the httpx POST to /detect."""
    mock_resp = _fake_layout_response(boxes)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)
    return patch("ocr_parser.engines.paddleocr_vl.httpx.AsyncClient", return_value=mock_client)


def test_process_page_two_stage(tmp_path):
    boxes = [
        {"bbox": [0, 0, 400, 50],  "label": "doc_title", "score": 0.99, "index": 0},
        {"bbox": [0, 60, 400, 200], "label": "text",      "score": 0.95, "index": 1},
    ]
    parser = FakeParser("Some content")
    engine = PaddleOCRVLEngine(parser)
    page_data = _make_page_data(tmp_path)

    with _patch_layout(boxes):
        result = asyncio.run(engine.process_page(page_data))

    assert result.status == "success_fallback_text"
    # both blocks produce content → md_content is non-empty
    assert result.md_content
    # artifacts written
    names = [Path(a["path"]).name for a in result.native_artifacts]
    assert "page_0001_raw.json" in names
    assert "page_0001.md" in names


def test_process_page_fallback_on_layout_failure(tmp_path):
    """If layout service is down, falls back to single-stage OCR."""
    parser = FakeParser("Fallback OCR text")
    engine = PaddleOCRVLEngine(parser)
    page_data = _make_page_data(tmp_path)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

    with patch("ocr_parser.engines.paddleocr_vl.httpx.AsyncClient", return_value=mock_client):
        result = asyncio.run(engine.process_page(page_data))

    assert result.md_content == "Fallback OCR text"


def test_process_page_empty_boxes_fallback(tmp_path):
    """Empty layout result also triggers single-stage fallback."""
    parser = FakeParser("Single stage result")
    engine = PaddleOCRVLEngine(parser)
    page_data = _make_page_data(tmp_path)

    with _patch_layout([]):
        result = asyncio.run(engine.process_page(page_data))

    assert result.md_content == "Single stage result"


def test_process_page_records_layout_failure_fallback_metrics(tmp_path):
    parser = FakeParser("Fallback OCR text")
    engine = PaddleOCRVLEngine(parser)
    page_data = _make_page_data(tmp_path)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))

    with patch("ocr_parser.engines.paddleocr_vl.httpx.AsyncClient", return_value=mock_client):
        result = asyncio.run(engine.process_page(page_data))

    snapshot = get_runtime_snapshot(parser)

    assert result.md_content == "Fallback OCR text"
    assert snapshot["paddle_layout_failures"] == 1
    assert snapshot["paddle_single_stage_fallbacks"] == 1


def test_process_page_records_two_stage_metrics(tmp_path):
    boxes = [
        {"bbox": [0, 0, 200, 50], "label": "text", "score": 0.99, "index": 0},
        {"bbox": [0, 60, 200, 120], "label": "image", "score": 0.95, "index": 1},
        {"bbox": [0, 130, 200, 200], "label": "table", "score": 0.95, "index": 2},
    ]
    parser = FakeParser("Some content")
    engine = PaddleOCRVLEngine(parser)
    page_data = _make_page_data(tmp_path)

    with _patch_layout(boxes):
        result = asyncio.run(engine.process_page(page_data))

    assert result.md_content
    assert parser.two_stage_metrics["two_stage_engine"] == "paddleocr-vl"
    assert parser.two_stage_metrics["two_stage_blocks_detected"] == 3
    assert parser.two_stage_metrics["two_stage_blocks_recognized"] == 2
    assert parser.two_stage_metrics["two_stage_blocks_skipped"] == 1
    assert parser.two_stage_metrics["two_stage_max_block_queue_depth"] == 2

    snapshot = get_runtime_snapshot(parser)
    assert snapshot["paddle_blocks_detected"] == 3
    assert snapshot["paddle_blocks_recognized"] == 2
    assert snapshot["paddle_block_queue_depth"] == 2


def test_detect_layout_calls_are_bounded_by_paddle_layout_concurrency(tmp_path):
    active = 0
    peak = 0

    parser = FakeParser("Some content")
    parser.paddle_layout_concurrency = 1
    engine = PaddleOCRVLEngine(parser)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    page_a = _make_page_data(dir_a)
    page_b = _make_page_data(dir_b)
    boxes = [{"bbox": [0, 0, 200, 50], "label": "text", "score": 0.99, "index": 0}]

    async def post(*_args, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return _fake_layout_response(boxes)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=post)

    async def run_pages():
        with patch("ocr_parser.engines.paddleocr_vl.httpx.AsyncClient", return_value=mock_client):
            await asyncio.gather(engine.process_page(page_a), engine.process_page(page_b))

    asyncio.run(run_pages())
    snapshot = get_runtime_snapshot(parser)

    assert peak == 1
    assert snapshot["paddle_layout_api_inflight_peak"] == 1
    assert snapshot["paddle_layout_queue_depth"] == 2


def test_layout_waits_when_paddle_block_backlog_reaches_high_watermark(tmp_path):
    post_times = []
    first_recognition_done_at = None

    parser = FakeParser("Some content")
    parser.paddle_layout_concurrency = 1
    parser.paddle_block_backpressure_high_watermark = 1
    parser.paddle_block_backpressure_low_watermark = 0
    engine = PaddleOCRVLEngine(parser)
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    page_a = _make_page_data(dir_a)
    page_b = _make_page_data(dir_b)
    boxes = [{"bbox": [0, 0, 200, 50], "label": "text", "score": 0.99, "index": 0}]

    async def post(*_args, **_kwargs):
        post_times.append(asyncio.get_running_loop().time())
        if len(post_times) == 1:
            await asyncio.sleep(0.02)
        return _fake_layout_response(boxes)

    async def create(**_kwargs):
        nonlocal first_recognition_done_at
        await asyncio.sleep(0.04)
        if first_recognition_done_at is None:
            first_recognition_done_at = asyncio.get_running_loop().time()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="text"))])

    parser.client.chat.completions.create = AsyncMock(side_effect=create)
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(side_effect=post)

    async def run_pages():
        with patch("ocr_parser.engines.paddleocr_vl.httpx.AsyncClient", return_value=mock_client):
            first = asyncio.create_task(engine.process_page(page_a))
            await asyncio.sleep(0.005)
            second = asyncio.create_task(engine.process_page(page_b))
            await asyncio.gather(first, second)

    asyncio.run(run_pages())
    snapshot = get_runtime_snapshot(parser)

    assert len(post_times) == 2
    assert first_recognition_done_at is not None
    assert post_times[1] >= first_recognition_done_at
    assert snapshot["paddle_block_backpressure_wait_count"] >= 1


def test_finalize_document_joins_pages(tmp_path):
    parser = FakeParser()
    engine = PaddleOCRVLEngine(parser)
    pages = [
        {"md_content": "Page one."},
        {"md_content": "Page two."},
    ]
    artifacts = asyncio.run(engine.finalize_document(pages, str(tmp_path), "report"))
    text = Path(artifacts[0]["path"]).read_text(encoding="utf-8")
    assert text == "Page one.\n\nPage two."


def test_prompt_mapping():
    engine = PaddleOCRVLEngine(FakeParser())
    assert engine._prompt_for_label("text") == "OCR:"
    assert engine._prompt_for_label("doc_title") == "OCR:"
    assert engine._prompt_for_label("table") == "Table Recognition:"
    assert engine._prompt_for_label("display_formula") == "Formula Recognition:"
    assert engine._prompt_for_label("inline_formula") == "Formula Recognition:"
    assert engine._prompt_for_label("header") is None
    assert engine._prompt_for_label("image") is None
    assert engine._prompt_for_label("chart") is None
    assert engine._prompt_for_label("seal") is None
