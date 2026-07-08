import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from ocr_parser.engines.native_openai import NativeOpenAIEngine
from ocr_parser.runtime import get_runtime_snapshot


class FakeMonitor:
    def record_inference_time(self, _):
        pass

    def record_error(self, _):
        pass

    def record_retry(self, _):
        pass


class SequencedParser:
    model_name = "MinerU2.5"
    temperature = 0.1
    top_p = 0.9
    max_completion_tokens = 128
    max_retries = 1
    retry_delay = 0.0
    block_concurrency = 2
    api_concurrency_start = 2
    api_concurrency_max = 2
    mineru_layout_reserved_api_slots = 1

    def __init__(self, responses):
        self.monitor = FakeMonitor()
        self.client = MagicMock()
        self.client.chat = MagicMock()
        self.client.chat.completions = MagicMock()
        response_iter = iter(responses)

        async def create(**_kwargs):
            await asyncio.sleep(0.01)
            text = next(response_iter)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

        self.client.chat.completions.create = AsyncMock(side_effect=create)

    def _is_transient_inference_error(self, _exc) -> bool:
        return False


def _make_page_data(tmp_path: Path) -> dict:
    img_path = tmp_path / "page.jpg"
    Image.new("RGB", (200, 300)).save(str(img_path))
    return {
        "page_idx": 0,
        "original_page_num": 1,
        "processed_image_path": str(img_path),
        "save_dir": str(tmp_path),
    }


def test_mineru_reports_layout_and_recognition_api_stage_metrics(tmp_path):
    parser = SequencedParser(
        [
            "000 000 500 500text\n500 000 999 500table",
            "Recognized text",
            "<fcel>A<lcel><nl>",
        ]
    )
    parser.mineru_layout_reserved_api_slots = 0
    engine = NativeOpenAIEngine(parser, "mineru")

    result = asyncio.run(engine.process_page(_make_page_data(tmp_path)))
    snapshot = get_runtime_snapshot(parser)

    assert "Recognized text" in result.md_content
    assert snapshot["mineru_layout_api_call_count"] == 1
    assert snapshot["mineru_recognition_api_call_count"] == 2
    assert snapshot["mineru_layout_api_inflight_peak"] == 1
    assert snapshot["mineru_recognition_api_inflight_peak"] == 2
    assert snapshot["mineru_recognition_queue_depth"] == 2
    assert snapshot["mineru_blocks_per_page_avg"] == 2.0


def test_mineru_recognition_keeps_layout_api_slot_available():
    parser = SequencedParser(["ok", "ok", "ok"])
    engine = NativeOpenAIEngine(parser, "mineru")
    started_prompts = []

    async def create(**kwargs):
        prompt = kwargs["messages"][-1]["content"][1]["text"].strip()
        started_prompts.append(prompt)
        await asyncio.sleep(0.03)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    parser.client.chat.completions.create = AsyncMock(side_effect=create)

    async def run_requests():
        parser.api_semaphore = asyncio.Semaphore(2)
        first_recognition = asyncio.create_task(
            engine._infer_raw("data:image/png;base64,a", "\nText Recognition:", mineru_stage="recognition")
        )
        second_recognition = asyncio.create_task(
            engine._infer_raw("data:image/png;base64,b", "\nTable Recognition:", mineru_stage="recognition")
        )
        await asyncio.sleep(0)
        layout = asyncio.create_task(
            engine._infer_raw("data:image/png;base64,c", "\nLayout Detection:", mineru_stage="layout")
        )
        await asyncio.gather(first_recognition, second_recognition, layout)

    asyncio.run(run_requests())

    assert started_prompts[:2] == ["Text Recognition:", "Layout Detection:"]


def test_mineru_reports_layout_queue_depth_when_layout_requests_overlap():
    parser = SequencedParser(["layout-a", "layout-b"])
    engine = NativeOpenAIEngine(parser, "mineru")

    async def create(**_kwargs):
        await asyncio.sleep(0.03)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    parser.client.chat.completions.create = AsyncMock(side_effect=create)

    async def run_requests():
        parser.api_semaphore = asyncio.Semaphore(1)
        await asyncio.gather(
            engine._infer_raw("data:image/png;base64,a", "\nLayout Detection:", mineru_stage="layout"),
            engine._infer_raw("data:image/png;base64,b", "\nLayout Detection:", mineru_stage="layout"),
        )

    asyncio.run(run_requests())
    snapshot = get_runtime_snapshot(parser)

    assert snapshot["mineru_layout_queue_depth"] == 2
    assert snapshot["api_inflight_peak"] == 1


def test_mineru_filters_discarded_and_tiny_blocks_before_recognition(tmp_path):
    parser = SequencedParser(
        [
            "000 000 999 050header\n000 060 010 070text\n000 100 500 500text",
            "Main paragraph",
        ]
    )
    parser.mineru_min_block_area_ratio = 0.001
    engine = NativeOpenAIEngine(parser, "mineru")

    result = asyncio.run(engine.process_page(_make_page_data(tmp_path)))
    snapshot = get_runtime_snapshot(parser)

    assert "Main paragraph" in result.md_content
    assert "header" not in result.md_content.lower()
    assert snapshot["mineru_recognition_api_call_count"] == 1
    assert snapshot["mineru_blocks_filtered"] == 2
    assert snapshot["mineru_recognition_queue_depth"] == 1


def test_mineru_can_save_visual_blocks_without_vlm_recognition(tmp_path):
    parser = SequencedParser(
        [
            "000 000 500 500image\n500 000 999 500text",
            "Text beside image",
        ]
    )
    parser.mineru_skip_visual_block_recognition = True
    engine = NativeOpenAIEngine(parser, "mineru")

    result = asyncio.run(engine.process_page(_make_page_data(tmp_path)))
    snapshot = get_runtime_snapshot(parser)

    assert "![](images/page_0001_image_000.jpg)" in result.md_content
    assert "Text beside image" in result.md_content
    assert snapshot["mineru_recognition_api_call_count"] == 1
    assert snapshot["two_stage_blocks_skipped"] == 1
    assert (tmp_path / "native" / "mineru" / "images" / "page_0001_image_000.jpg").exists()


def test_mineru_caps_blocks_per_page_before_recognition(tmp_path):
    parser = SequencedParser(
        [
            "000 000 200 200text\n200 000 400 200text\n400 000 600 200text",
            "First",
            "Second",
        ]
    )
    parser.mineru_max_blocks_per_page = 2
    engine = NativeOpenAIEngine(parser, "mineru")

    result = asyncio.run(engine.process_page(_make_page_data(tmp_path)))
    snapshot = get_runtime_snapshot(parser)

    assert "First" in result.md_content
    assert "Second" in result.md_content
    assert snapshot["mineru_recognition_api_call_count"] == 2
    assert snapshot["mineru_blocks_filtered"] == 1
    assert snapshot["mineru_recognition_queue_depth"] == 2
