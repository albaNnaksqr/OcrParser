from __future__ import annotations

import asyncio

from ocr_parser.engines.two_stage import LayoutBlock, recognize_layout_blocks
from ocr_parser.engines.two_stage import TwoStageMetrics, record_two_stage_metrics
from ocr_parser.runtime import get_runtime_snapshot


def test_recognize_layout_blocks_bounds_block_concurrency():
    active = 0
    peak_active = 0
    blocks = [
        LayoutBlock(block_id=f"b{i}", label="text", bbox=(0, 0, 10, 10), prompt="OCR:")
        for i in range(6)
    ]

    async def recognize(block: LayoutBlock) -> str:
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return f"content-{block.block_id}"

    results, metrics = asyncio.run(
        recognize_layout_blocks(blocks, recognize, concurrency=2)
    )

    assert peak_active == 2
    assert [result.content for result in results] == [
        "content-b0",
        "content-b1",
        "content-b2",
        "content-b3",
        "content-b4",
        "content-b5",
    ]
    assert metrics.blocks_detected == 6
    assert metrics.blocks_recognized == 6
    assert metrics.blocks_skipped == 0
    assert metrics.max_block_queue_depth == 6
    assert metrics.recognition_latency_seconds_total > 0


def test_recognize_layout_blocks_skips_blocks_without_prompt():
    called = []
    blocks = [
        LayoutBlock(block_id="title", label="doc_title", bbox=(0, 0, 10, 10), prompt="OCR:"),
        LayoutBlock(block_id="image", label="image", bbox=(0, 10, 20, 20), prompt=None),
        LayoutBlock(block_id="footer", label="footer", bbox=(0, 20, 20, 30), prompt=None),
    ]

    async def recognize(block: LayoutBlock) -> str:
        called.append(block.block_id)
        return f"content-{block.block_id}"

    results, metrics = asyncio.run(
        recognize_layout_blocks(blocks, recognize, concurrency=4)
    )

    assert called == ["title"]
    assert [(result.block.block_id, result.content, result.skipped) for result in results] == [
        ("title", "content-title", False),
        ("image", "", True),
        ("footer", "", True),
    ]
    assert metrics.blocks_detected == 3
    assert metrics.blocks_recognized == 1
    assert metrics.blocks_skipped == 2


def test_stage_metrics_runtime_dict_uses_engine_prefix():
    blocks = [
        LayoutBlock(block_id="a", label="text", bbox=(0, 0, 1, 1), prompt="OCR:"),
    ]

    async def recognize(_block: LayoutBlock) -> str:
        await asyncio.sleep(0)
        return "ok"

    _results, metrics = asyncio.run(
        recognize_layout_blocks(blocks, recognize, concurrency=1, engine_name="mineru")
    )

    snapshot = metrics.to_runtime_dict()

    assert snapshot["two_stage_engine"] == "mineru"
    assert snapshot["two_stage_blocks_detected"] == 1
    assert snapshot["two_stage_blocks_recognized"] == 1
    assert snapshot["two_stage_blocks_skipped"] == 0
    assert snapshot["two_stage_max_block_queue_depth"] == 1
    assert "two_stage_recognition_latency_seconds_total" in snapshot


def test_runtime_snapshot_includes_recorded_two_stage_metrics():
    class Parser:
        pass

    parser = Parser()
    metrics = TwoStageMetrics(
        engine_name="paddleocr-vl",
        layout_latency_seconds_total=0.5,
        recognition_latency_seconds_total=1.25,
        blocks_detected=5,
        blocks_recognized=3,
        blocks_skipped=2,
        max_block_queue_depth=3,
        oldest_block_wait_seconds=0.1,
    )

    record_two_stage_metrics(parser, metrics)
    snapshot = get_runtime_snapshot(parser)

    assert snapshot["two_stage_engine"] == "paddleocr-vl"
    assert snapshot["two_stage_blocks_detected"] == 5
    assert snapshot["two_stage_blocks_recognized"] == 3
    assert snapshot["two_stage_blocks_skipped"] == 2
    assert snapshot["two_stage_max_block_queue_depth"] == 3
