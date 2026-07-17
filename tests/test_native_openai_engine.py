import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_parser.engines.native_openai import NativeOpenAIEngine


def _make_fake_client(text: str):
    choice = SimpleNamespace(message=SimpleNamespace(content=text))
    completion = SimpleNamespace(choices=[choice])
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=completion)
    return client


class FakeMonitor:
    def record_inference_time(self, _): pass
    def record_error(self, _): pass
    def record_retry(self, _): pass


class FakeParser:
    model_name = "test-model"
    temperature = 0.1
    top_p = 0.9
    max_completion_tokens = 128
    max_retries = 1
    retry_delay = 0.0

    def __init__(self, response_text: str):
        self.client = _make_fake_client(response_text)
        self.monitor = FakeMonitor()

    def _is_transient_inference_error(self, exc) -> bool:
        return False


def _make_page_data(tmp_path, page_no=1) -> dict:
    from PIL import Image
    img_path = tmp_path / "page.jpg"
    Image.new("RGB", (200, 300)).save(str(img_path))
    return {
        "page_idx": 0,
        "original_page_num": page_no,
        "processed_image_path": str(img_path),
        "save_dir": str(tmp_path),
    }


def test_vlm_text_response_stored_as_is(tmp_path):
    """Raw VLM text output is written verbatim — no schema extraction applied."""
    md = "# Section\n\nSome paragraph text.\n\n| A | B |\n|---|---|\n| 1 | 2 |"
    parser = FakeParser(md)
    engine = NativeOpenAIEngine(parser, "paddleocr-vl")
    page_data = _make_page_data(tmp_path)

    result = asyncio.run(engine.process_page(page_data))
    payload = result.to_layout_result()

    assert payload["md_content"] == md
    assert payload["status"] == "success_fallback_text"
    md_path = Path(payload["native_artifacts"][1]["path"])
    assert md_path.read_text(encoding="utf-8") == md


def test_artifacts_named_correctly(tmp_path):
    parser = FakeParser("content")
    engine = NativeOpenAIEngine(parser, "mineru")
    page_data = _make_page_data(tmp_path, page_no=3)

    result = asyncio.run(engine.process_page(page_data))
    names = [Path(a["path"]).name for a in result.native_artifacts]

    assert "page_0003_raw.json" in names
    assert "page_0003.md" in names


def test_finalize_document_joins_pages(tmp_path):
    parser = FakeParser("")
    engine = NativeOpenAIEngine(parser, "paddleocr-vl")
    pages = [
        {"md_content": "Page one content."},
        {"md_content": "Page two content."},
    ]

    artifacts = asyncio.run(engine.finalize_document(pages, str(tmp_path), "doc"))

    md_path = Path(artifacts[0]["path"])
    assert md_path.read_text(encoding="utf-8") == "Page one content.\n\nPage two content."


def test_retry_on_transient_error(tmp_path):
    """Transient errors trigger retries with exponential backoff."""
    call_count = 0

    class TransientParser(FakeParser):
        max_retries = 3
        retry_delay = 0.0

        def _is_transient_inference_error(self, exc) -> bool:
            return True

    parser = TransientParser("ok")
    original_create = parser.client.chat.completions.create

    async def flaky(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise RuntimeError("transient")
        return await original_create(*args, **kwargs)

    parser.client.chat.completions.create = flaky
    engine = NativeOpenAIEngine(parser, "mineru")
    page_data = _make_page_data(tmp_path)

    result = asyncio.run(engine.process_page(page_data))
    assert result.md_content == "ok"
    assert call_count == 2


def test_mineru_records_two_stage_metrics(tmp_path):
    parser = FakeParser("")
    engine = NativeOpenAIEngine(parser, "mineru")
    responses = [
        "000 000 500 500text\n500 000 999 500table",
        "Recognized text",
        "<fcel>A<lcel><nl>",
    ]

    async def fake_infer_raw(_data_url, prompt, with_system_prompt=False, **_kwargs):
        assert with_system_prompt is True
        return responses.pop(0)

    engine._infer_raw = fake_infer_raw
    page_data = _make_page_data(tmp_path)

    result = asyncio.run(engine.process_page(page_data))

    assert "Recognized text" in result.md_content
    assert result.execution_trace.fallback.used is False
    assert [stage.stage for stage in result.execution_trace.stages] == [
        "layout",
        "recognition",
        "output",
    ]
    assert parser.two_stage_metrics["two_stage_engine"] == "mineru"
    assert parser.two_stage_metrics["two_stage_blocks_detected"] == 2
    assert parser.two_stage_metrics["two_stage_blocks_recognized"] == 2
    assert parser.two_stage_metrics["two_stage_blocks_skipped"] == 0
    assert parser.two_stage_metrics["two_stage_max_block_queue_depth"] == 2


def test_mineru_layout_only_output_is_recorded_as_real_fallback(tmp_path):
    parser = FakeParser("")
    engine = NativeOpenAIEngine(parser, "mineru")

    async def fake_infer_raw(*_args, **_kwargs):
        return "layout response without parseable boxes"

    engine._infer_raw = fake_infer_raw
    result = asyncio.run(engine.process_page(_make_page_data(tmp_path)))

    assert result.execution_trace.fallback.to_dict() == {
        "used": True,
        "reason": "layout_output_unusable",
        "source_stage": "layout",
    }


def test_mineru_dynamic_recognition_limiter_does_not_replace_active_slots():
    async def run_scenario():
        parser = FakeParser("")
        parser.api_limiter = SimpleNamespace(limit=2)
        parser.mineru_layout_reserved_api_slots = 0
        engine = NativeOpenAIEngine(parser, "mineru")

        active = 0
        max_active = 0
        both_active = asyncio.Event()
        release = asyncio.Event()

        async def payload():
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_active.set()
            await release.wait()
            active -= 1
            return "ok"

        first = asyncio.create_task(engine._with_mineru_recognition_slot(payload()))
        second = asyncio.create_task(engine._with_mineru_recognition_slot(payload()))
        await asyncio.wait_for(both_active.wait(), timeout=1)

        parser.api_limiter.limit = 1
        third = asyncio.create_task(engine._with_mineru_recognition_slot(payload()))
        await asyncio.sleep(0.05)

        assert max_active == 2

        release.set()
        assert await asyncio.gather(first, second, third) == ["ok", "ok", "ok"]

    asyncio.run(run_scenario())
