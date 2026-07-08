import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_parser.engines.base import EnginePageResult
from ocr_parser.pipeline.document_parser import process_single_page


def test_engine_page_result_keeps_dots_cells():
    result = EnginePageResult(
        page_no=0,
        original_page_num=1,
        status="success",
        cells=[{"category": "Text", "bbox": [0, 0, 10, 10], "text": "hello"}],
        page_json_path="/tmp/page.json",
    )
    payload = result.to_layout_result()
    assert payload["status"] == "success"
    assert payload["cells"][0]["text"] == "hello"
    assert payload["page_json_path"] == "/tmp/page.json"


def test_engine_page_result_preserves_none_original_cells_for_success():
    result = EnginePageResult(
        page_no=0,
        original_page_num=1,
        status="success",
        cells=[{"category": "Text", "bbox": [0, 0, 10, 10], "text": "hello"}],
        original_cells=None,
        page_json_path="/tmp/page.json",
        page_layout_path="/tmp/page.jpg",
    )

    payload = result.to_layout_result()

    assert "original_cells" in payload
    assert payload["original_cells"] is None


def test_engine_page_result_preserves_none_artifact_paths_for_fallback():
    result = EnginePageResult(
        page_no=0,
        original_page_num=1,
        status="success_fallback_text",
        md_content="hello",
        cells=[],
        page_json_path=None,
        page_layout_path=None,
    )

    payload = result.to_layout_result()

    assert "page_json_path" in payload
    assert payload["page_json_path"] is None
    assert "page_layout_path" in payload
    assert payload["page_layout_path"] is None


@pytest.mark.asyncio
async def test_process_single_page_converts_engine_result_to_layout_dict():
    class FakeEngine:
        async def process_page(self, page_data):
            return EnginePageResult(
                page_no=page_data["page_idx"],
                original_page_num=page_data["original_page_num"],
                status="success",
                cells=[{"category": "Text", "bbox": [0, 0, 10, 10], "text": "hello"}],
            )

    class ParserStub:
        ocr_engine = FakeEngine()

    payload = await process_single_page(ParserStub(), {"page_idx": 0, "original_page_num": 1})

    assert isinstance(payload, dict)
    assert payload["status"] == "success"
    assert payload["cells"][0]["text"] == "hello"
