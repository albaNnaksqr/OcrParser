from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple


RecognizeBlock = Callable[["LayoutBlock"], Awaitable[str]]


@dataclass(frozen=True)
class LayoutBlock:
    block_id: str
    label: str
    bbox: Tuple[float, float, float, float]
    prompt: Optional[str]
    payload: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecognizedBlock:
    block: LayoutBlock
    content: str
    skipped: bool = False
    wait_seconds: float = 0.0
    recognition_latency_seconds: float = 0.0


@dataclass
class TwoStageMetrics:
    engine_name: Optional[str] = None
    layout_latency_seconds_total: float = 0.0
    recognition_latency_seconds_total: float = 0.0
    blocks_detected: int = 0
    blocks_recognized: int = 0
    blocks_skipped: int = 0
    max_block_queue_depth: int = 0
    oldest_block_wait_seconds: float = 0.0

    def to_runtime_dict(self) -> Dict[str, Any]:
        return {
            "two_stage_engine": self.engine_name,
            "two_stage_layout_latency_seconds_total": self.layout_latency_seconds_total,
            "two_stage_recognition_latency_seconds_total": self.recognition_latency_seconds_total,
            "two_stage_blocks_detected": self.blocks_detected,
            "two_stage_blocks_recognized": self.blocks_recognized,
            "two_stage_blocks_skipped": self.blocks_skipped,
            "two_stage_max_block_queue_depth": self.max_block_queue_depth,
            "two_stage_oldest_block_wait_seconds": self.oldest_block_wait_seconds,
        }

    def merge(self, other: "TwoStageMetrics") -> None:
        if other.engine_name:
            self.engine_name = other.engine_name
        self.layout_latency_seconds_total += other.layout_latency_seconds_total
        self.recognition_latency_seconds_total += other.recognition_latency_seconds_total
        self.blocks_detected += other.blocks_detected
        self.blocks_recognized += other.blocks_recognized
        self.blocks_skipped += other.blocks_skipped
        self.max_block_queue_depth = max(self.max_block_queue_depth, other.max_block_queue_depth)
        self.oldest_block_wait_seconds = max(
            self.oldest_block_wait_seconds,
            other.oldest_block_wait_seconds,
        )


def record_two_stage_metrics(target: Any, metrics: TwoStageMetrics) -> None:
    aggregate = getattr(target, "_two_stage_metrics", None)
    if not isinstance(aggregate, TwoStageMetrics):
        aggregate = TwoStageMetrics(engine_name=metrics.engine_name)
        setattr(target, "_two_stage_metrics", aggregate)
    aggregate.merge(metrics)
    setattr(target, "two_stage_metrics", aggregate.to_runtime_dict())


async def recognize_layout_blocks(
    blocks: Sequence[LayoutBlock],
    recognize_block: RecognizeBlock,
    *,
    concurrency: int,
    engine_name: Optional[str] = None,
    metrics: Optional[TwoStageMetrics] = None,
) -> tuple[List[RecognizedBlock], TwoStageMetrics]:
    stage_metrics = metrics or TwoStageMetrics(engine_name=engine_name)
    if engine_name and not stage_metrics.engine_name:
        stage_metrics.engine_name = engine_name

    stage_metrics.blocks_detected += len(blocks)
    recognizable = [block for block in blocks if block.prompt is not None]
    stage_metrics.blocks_skipped += len(blocks) - len(recognizable)
    stage_metrics.max_block_queue_depth = max(
        stage_metrics.max_block_queue_depth,
        len(recognizable),
    )

    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    queued_at = {id(block): time.monotonic() for block in recognizable}

    async def run_one(block: LayoutBlock) -> RecognizedBlock:
        async with semaphore:
            start = time.monotonic()
            wait_seconds = max(0.0, start - queued_at.get(id(block), start))
            content = await recognize_block(block)
            latency = max(0.0, time.monotonic() - start)
            stage_metrics.blocks_recognized += 1
            stage_metrics.recognition_latency_seconds_total += latency
            stage_metrics.oldest_block_wait_seconds = max(
                stage_metrics.oldest_block_wait_seconds,
                wait_seconds,
            )
            return RecognizedBlock(
                block=block,
                content=content,
                skipped=False,
                wait_seconds=wait_seconds,
                recognition_latency_seconds=latency,
            )

    tasks = {
        block.block_id: asyncio.create_task(run_one(block))
        for block in recognizable
    }
    results = await asyncio.gather(*tasks.values())
    recognized_by_id = dict(zip(tasks.keys(), results))

    ordered: List[RecognizedBlock] = []
    for block in blocks:
        if block.prompt is None:
            ordered.append(RecognizedBlock(block=block, content="", skipped=True))
        else:
            ordered.append(recognized_by_id[block.block_id])
    return ordered, stage_metrics
