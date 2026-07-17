from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from ocr_parser.output.native_writer import async_write_native_json, async_write_native_text

from .api import create_chat_completion, prepare_image_payload, run_in_encode_lane
from .base import (
    EngineCapabilities,
    EngineExecutionTrace,
    EnginePageResult,
    FallbackInfo,
    StageOutcome,
)
from .otsl2html import convert_otsl_to_html
from .two_stage import LayoutBlock, TwoStageMetrics, recognize_layout_blocks, record_two_stage_metrics


_MINERU_LAYOUT_RE = re.compile(r'^(\d{3})\s+(\d{3})\s+(\d{3})\s+(\d{3})(\w+)$', re.MULTILINE)

# Block types whose crops are saved as image files (official: IMAGE, CHART).
# Equations are rendered as LaTeX; tables as HTML — no crop needed.
_MINERU_VISUAL_BLOCKS = {"image", "chart"}

_MINERU_DISCARD_BLOCKS = {"header", "footer", "page_number", "aside_text", "page_footnote"}
_MINERU_TEXT_BLOCKS = {
    "text",
    "title",
    "ref_text",
    "list",
    "phonetic",
    "aside_text",
    "header",
    "footer",
    "page_number",
    "page_footnote",
    "image_caption",
    "table_caption",
    "code_caption",
    "image_footnote",
    "table_footnote",
}


class _DynamicAsyncLimiter:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = 0
        self._limit = 0

    @contextlib.asynccontextmanager
    async def slot(self, limit: int):
        async with self._condition:
            self._limit = max(0, int(limit))
            self._condition.notify_all()
            await self._condition.wait_for(lambda: self._limit <= 0 or self._active < self._limit)
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active = max(0, self._active - 1)
                self._condition.notify_all()


class NativeOpenAIEngine:
    """Thin async wrapper around an OpenAI-compatible VLM endpoint.

    Sends the full page image with a minimal prompt and records the raw
    text response.  No schema extraction or post-processing is applied —
    the VLM output (plain text / markdown) is stored as-is.
    """

    def __init__(self, parser: Any, name: str):
        self.parser = parser
        self.name = name
        self.capabilities = EngineCapabilities(
            uses_shared_postprocess=False,
            emits_native_artifacts=True,
        )

    def _prompt(self) -> str:
        if self.name == "mineru":
            # Upstream prompt: mineru-vl-utils v0.2.6 mineru_client.py.
            return "\nLayout Detection:"
        if self.name == "paddleocr-vl":
            # Upstream prompt: paddlex v3.5.1 PaddleOCR-VL-1.5.yaml.
            return "OCR:"
        # Minimal instruction; the model is fine-tuned for document OCR so
        # it produces markdown directly without needing a structured prompt.
        return "Read this document page and output the content in Markdown."

    async def _infer(self, image_path: str, prompt: str) -> str:
        data_url, _ = await prepare_image_payload(self.parser, image_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        response = await create_chat_completion(
            self.parser,
            model=self.parser.model_name,
            messages=messages,
            temperature=self.parser.temperature,
            top_p=self.parser.top_p,
            max_tokens=self.parser.max_completion_tokens,
        )
        return response.choices[0].message.content or ""

    def _image_to_data_url(self, pil_image) -> str:
        import base64

        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _record_mineru_queue_depth(self, *, recognition_queue_depth: int) -> None:
        metrics = getattr(self.parser, "mineru_two_stage_metrics", None)
        if not isinstance(metrics, dict):
            metrics = {}
            setattr(self.parser, "mineru_two_stage_metrics", metrics)
        metrics["mineru_recognition_queue_depth"] = max(
            int(metrics.get("mineru_recognition_queue_depth", 0) or 0),
            int(recognition_queue_depth),
        )
        page_count = int(metrics.get("_mineru_page_count", 0) or 0) + 1
        block_total = int(metrics.get("_mineru_blocks_total", 0) or 0) + int(recognition_queue_depth)
        metrics["_mineru_page_count"] = page_count
        metrics["_mineru_blocks_total"] = block_total
        metrics["mineru_blocks_per_page_avg"] = block_total / page_count if page_count else 0.0

    def _record_mineru_filtered_blocks(self, count: int) -> None:
        if count <= 0:
            return
        metrics = getattr(self.parser, "mineru_two_stage_metrics", None)
        if not isinstance(metrics, dict):
            metrics = {}
            setattr(self.parser, "mineru_two_stage_metrics", metrics)
        metrics["mineru_blocks_filtered"] = int(metrics.get("mineru_blocks_filtered", 0) or 0) + count

    def _mineru_min_block_area_ratio(self) -> float:
        try:
            return max(0.0, float(getattr(self.parser, "mineru_min_block_area_ratio", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _mineru_max_blocks_per_page(self) -> int:
        try:
            return max(0, int(getattr(self.parser, "mineru_max_blocks_per_page", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _should_filter_mineru_block(self, block: Dict[str, Any]) -> bool:
        if block["type"] in _MINERU_DISCARD_BLOCKS:
            return True
        min_area_ratio = self._mineru_min_block_area_ratio()
        if min_area_ratio <= 0:
            return False
        width = max(0, int(block["x2"]) - int(block["x1"]))
        height = max(0, int(block["y2"]) - int(block["y1"]))
        area_ratio = (width * height) / 1_000_000
        return area_ratio < min_area_ratio

    def _mineru_recognition_api_limit(self) -> int:
        explicit = int(getattr(self.parser, "mineru_recognition_api_concurrency", 0) or 0)
        if explicit > 0:
            return explicit

        api_limiter = getattr(self.parser, "api_limiter", None)
        api_limit = getattr(api_limiter, "limit", None)
        if api_limit is None:
            api_limit = getattr(self.parser, "api_concurrency_start", 0) or getattr(
                self.parser,
                "api_concurrency",
                0,
            )
        try:
            api_limit = int(api_limit or 0)
        except (TypeError, ValueError):
            api_limit = 0
        if api_limit <= 0:
            return 0

        reserved = int(getattr(self.parser, "mineru_layout_reserved_api_slots", 1) or 0)
        return max(1, api_limit - max(0, reserved))

    async def _with_mineru_recognition_slot(self, coro):
        limit = self._mineru_recognition_api_limit()
        if limit <= 0:
            return await coro

        limiter = getattr(self.parser, "_mineru_recognition_api_limiter", None)
        if limiter is None:
            limiter = _DynamicAsyncLimiter()
            setattr(self.parser, "_mineru_recognition_api_limiter", limiter)

        async with limiter.slot(limit):
            return await coro

    async def _infer_raw(
        self,
        data_url: str,
        prompt: str,
        with_system_prompt: bool = False,
        mineru_stage: Optional[str] = None,
    ) -> str:
        if mineru_stage and self.name == "mineru":
            return await self._infer_raw_with_mineru_stage_metrics(
                data_url,
                prompt,
                with_system_prompt=with_system_prompt,
                mineru_stage=mineru_stage,
            )

        messages = []
        if with_system_prompt:
            messages.append({"role": "system", "content": "You are a helpful assistant."})
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        )
        response = await create_chat_completion(
            self.parser,
            model=self.parser.model_name,
            messages=messages,
            temperature=self.parser.temperature,
            top_p=self.parser.top_p,
            max_tokens=self.parser.max_completion_tokens,
        )
        return response.choices[0].message.content or ""

    async def _infer_raw_with_mineru_stage_metrics(
        self,
        data_url: str,
        prompt: str,
        *,
        with_system_prompt: bool,
        mineru_stage: str,
    ) -> str:
        if mineru_stage == "recognition":
            return await self._with_mineru_recognition_slot(
                self._infer_raw_with_mineru_stage_metrics(
                    data_url,
                    prompt,
                    with_system_prompt=with_system_prompt,
                    mineru_stage="_recognition_inner",
                )
            )
        if mineru_stage == "_recognition_inner":
            mineru_stage = "recognition"

        metrics = getattr(self.parser, "mineru_two_stage_metrics", None)
        if not isinstance(metrics, dict):
            metrics = {}
            setattr(self.parser, "mineru_two_stage_metrics", metrics)

        queue_pending_key = f"_mineru_{mineru_stage}_stage_pending"
        queue_depth_key = f"mineru_{mineru_stage}_queue_depth"
        if mineru_stage == "layout":
            metrics[queue_pending_key] = int(metrics.get(queue_pending_key, 0) or 0) + 1
            metrics[queue_depth_key] = max(
                int(metrics.get(queue_depth_key, 0) or 0),
                int(metrics[queue_pending_key]),
            )

        prefix = f"mineru_{mineru_stage}_api"
        inflight_key = f"{prefix}_inflight"
        peak_key = f"{prefix}_inflight_peak"
        call_key = f"{prefix}_call_count"
        metrics[call_key] = int(metrics.get(call_key, 0) or 0) + 1
        metrics[inflight_key] = int(metrics.get(inflight_key, 0) or 0) + 1
        metrics[peak_key] = max(
            int(metrics.get(peak_key, 0) or 0),
            int(metrics[inflight_key]),
        )
        try:
            return await self._infer_raw(
                data_url,
                prompt,
                with_system_prompt=with_system_prompt,
                mineru_stage=None,
            )
        finally:
            metrics[inflight_key] = max(0, int(metrics.get(inflight_key, 0) or 0) - 1)
            if mineru_stage == "layout":
                metrics[queue_pending_key] = max(
                    0,
                    int(metrics.get(queue_pending_key, 0) or 0) - 1,
                )

    async def _infer_mineru(
        self,
        image_path: str,
        prompt: str,
        save_dir: str = "",
        page_num: int = 0,
    ) -> Tuple[str, EngineExecutionTrace]:
        loop = asyncio.get_event_loop()

        with Image.open(image_path) as original_image:
            original = original_image.convert("RGB")

        layout_image = original.resize((1036, 1036), Image.Resampling.BICUBIC)
        layout_data_url = await run_in_encode_lane(
            self.parser,
            lambda: loop.run_in_executor(None, self._image_to_data_url, layout_image),
        )
        del layout_image
        metrics = TwoStageMetrics(engine_name=self.name)
        layout_start = time.monotonic()
        layout_response = await self._infer_raw(
            layout_data_url,
            "\nLayout Detection:",
            with_system_prompt=True,
            mineru_stage="layout",
        )
        metrics.layout_latency_seconds_total += max(0.0, time.monotonic() - layout_start)
        blocks = self._parse_mineru_layout(layout_response)

        if not blocks:
            record_two_stage_metrics(self.parser, metrics)
            return layout_response, EngineExecutionTrace(
                stages=(
                    StageOutcome(
                        stage="layout",
                        status="failed",
                        failure_category="model_output_invalid",
                        duration_seconds=metrics.layout_latency_seconds_total,
                    ),
                    StageOutcome(stage="recognition", status="skipped"),
                ),
                fallback=FallbackInfo(
                    used=True,
                    reason="layout_output_unusable",
                    source_stage="layout",
                ),
            )

        filtered = [b for b in blocks if not self._should_filter_mineru_block(b)]
        self._record_mineru_filtered_blocks(len(blocks) - len(filtered))
        max_blocks_per_page = self._mineru_max_blocks_per_page()
        if max_blocks_per_page and len(filtered) > max_blocks_per_page:
            self._record_mineru_filtered_blocks(len(filtered) - max_blocks_per_page)
            filtered = filtered[:max_blocks_per_page]

        # Prepare images/ dir lazily (only if there are visual blocks to save).
        images_dir: Optional[str] = None
        if save_dir and any(b["type"] in _MINERU_VISUAL_BLOCKS for b in filtered):
            images_dir = os.path.join(save_dir, "native", self.name, "images")
            os.makedirs(images_dir, exist_ok=True)

        def _crop_and_encode(block: Dict[str, Any], idx: int) -> Tuple[str, Optional[str]]:
            """Returns (data_url, relative_img_path_or_None)."""
            crop = original.crop(
                (
                    block["x1"] * original.width / 1000,
                    block["y1"] * original.height / 1000,
                    block["x2"] * original.width / 1000,
                    block["y2"] * original.height / 1000,
                )
            )
            data_url = self._image_to_data_url(crop)
            img_path: Optional[str] = None
            if images_dir and block["type"] in _MINERU_VISUAL_BLOCKS:
                fname = f"page_{page_num:04d}_{block['type']}_{idx:03d}.jpg"
                crop_rgb = crop.convert("RGB") if crop.mode != "RGB" else crop
                crop_rgb.save(os.path.join(images_dir, fname), "JPEG", quality=92)
                img_path = f"images/{fname}"
            return data_url, img_path

        pairs: List[Tuple[str, Optional[str]]] = await asyncio.gather(
            *[loop.run_in_executor(None, _crop_and_encode, b, i) for i, b in enumerate(filtered)]
        )
        del original

        def _prompt_for_block(block: Dict[str, Any]) -> str:
            block_type = block["type"]
            if block_type in _MINERU_TEXT_BLOCKS:
                return "\nText Recognition:"
            elif block_type == "table":
                return "\nTable Recognition:"
            elif block_type == "equation":
                return "\nFormula Recognition:"
            elif block_type in _MINERU_VISUAL_BLOCKS:
                return "\nImage Analysis:"
            return "\nText Recognition:"

        layout_blocks = [
            LayoutBlock(
                block_id=f"{page_num}:{idx}",
                label=block["type"],
                bbox=(block["x1"], block["y1"], block["x2"], block["y2"]),
                prompt=(
                    None
                    if block["type"] in _MINERU_VISUAL_BLOCKS
                    and bool(getattr(self.parser, "mineru_skip_visual_block_recognition", False))
                    else _prompt_for_block(block)
                ),
                payload=(block, data_url, img_path),
            )
            for idx, (block, (data_url, img_path)) in enumerate(zip(filtered, pairs))
        ]
        self._record_mineru_queue_depth(
            recognition_queue_depth=sum(1 for block in layout_blocks if block.prompt is not None)
        )

        async def recognize_block(layout_block: LayoutBlock) -> str:
            _block, data_url, _img_path = layout_block.payload
            return (
                await self._infer_raw(
                    data_url,
                    layout_block.prompt or "\nText Recognition:",
                    with_system_prompt=True,
                    mineru_stage="recognition",
                )
            ).strip()

        block_concurrency = getattr(self.parser, "block_concurrency", 0) or len(layout_blocks) or 1
        recognized_results, metrics = await recognize_layout_blocks(
            layout_blocks,
            recognize_block,
            concurrency=block_concurrency,
            engine_name=self.name,
            metrics=metrics,
        )
        record_two_stage_metrics(self.parser, metrics)
        recognized_blocks = []
        for result in recognized_results:
            block, _data_url, img_path = result.block.payload
            recognized_blocks.append({**block, "content": result.content.strip(), "img_path": img_path})
        return self._assemble_mineru_markdown(list(recognized_blocks)), EngineExecutionTrace(
            stages=(
                StageOutcome(
                    stage="layout",
                    status="success",
                    duration_seconds=metrics.layout_latency_seconds_total,
                ),
                StageOutcome(
                    stage="recognition",
                    status="success",
                    duration_seconds=metrics.recognition_latency_seconds_total,
                ),
            ),
            fallback=FallbackInfo(),
        )

    def _parse_mineru_layout(self, layout_response: str) -> List[Dict[str, Any]]:
        blocks = []
        for match in _MINERU_LAYOUT_RE.finditer(layout_response or ""):
            x1, y1, x2, y2, block_type = match.groups()
            blocks.append(
                {
                    "x1": int(x1),
                    "y1": int(y1),
                    "x2": int(x2),
                    "y2": int(y2),
                    "type": block_type,
                }
            )
        return blocks

    def _assemble_mineru_markdown(self, blocks: List[Dict[str, Any]]) -> str:
        # Official reference: vlm_middle_json_mkcontent.py _build_visual_body_segments
        # image/chart → ![](path) + <details><summary>{type} content</summary>{vlm_text}</details>
        parts = []
        for block in blocks:
            block_type = block["type"]
            content = (block.get("content") or block.get("text") or "").strip()
            img_path: Optional[str] = block.get("img_path")

            if block_type in _MINERU_VISUAL_BLOCKS:
                if img_path:
                    parts.append(f"![]({img_path})")
                    if content:
                        summary = f"{block_type} content"
                        parts.append(
                            f"<details>\n<summary>{summary}</summary>\n\n{content}\n</details>"
                        )
                elif content:
                    parts.append(content)
            elif block_type == "title":
                if content:
                    parts.append(f"# {content}")
            elif block_type == "table":
                if content:
                    html_table = convert_otsl_to_html(content)
                    parts.append(html_table if html_table else content)
            elif block_type == "equation":
                if content:
                    parts.append(f"$$\n{content}\n$$")
            else:
                if content:
                    parts.append(content)
        return "\n\n".join(parts)

    async def _infer_paddleocr_vl(self, image_path: str, prompt: str) -> str:
        # Upstream prompt: paddlex v3.5.1 PaddleOCR-VL-1.5.yaml.
        return await self._infer(image_path, prompt)

    async def _run_with_retries(self, coro_factory) -> str:
        attempt = 0
        delay_base = getattr(self.parser, "retry_delay", None) or 1.0
        attempt_limit = max(getattr(self.parser, "max_retries", None) or 1, 1)
        while True:
            attempt += 1
            try:
                t0 = time.time()
                result = await coro_factory()
                self.parser.monitor.record_inference_time(time.time() - t0)
                if attempt > 1:
                    self.parser.monitor.record_retry(attempt - 1)
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.parser.monitor.record_error(type(exc).__name__)
                if attempt >= attempt_limit or not self.parser._is_transient_inference_error(exc):
                    raise
                backoff = min(delay_base * (2 ** min(attempt - 1, 5)), 30.0)
                await asyncio.sleep(backoff)

    async def _infer_with_retries(
        self,
        image_path: str,
        prompt: str,
        save_dir: str = "",
        page_num: int = 0,
    ) -> Tuple[str, EngineExecutionTrace]:
        if self.name == "mineru":
            return await self._run_with_retries(
                lambda: self._infer_mineru(image_path, prompt, save_dir=save_dir, page_num=page_num)
            )
        if self.name == "paddleocr-vl":
            text = await self._run_with_retries(lambda: self._infer_paddleocr_vl(image_path, prompt))
        else:
            text = await self._run_with_retries(lambda: self._infer(image_path, prompt))
        return text, EngineExecutionTrace(
            stages=(StageOutcome(stage="primary_inference", status="success"),),
            fallback=FallbackInfo(),
        )

    async def process_page(self, page_data: Dict[str, Any]) -> EnginePageResult:
        page_idx = page_data["page_idx"]
        original_page_num = page_data["original_page_num"]
        save_dir = page_data["save_dir"]
        image_path = page_data.get("processed_image_path") or page_data.get("origin_image_path")

        md_content, execution_trace = await self._infer_with_retries(
            image_path, self._prompt(), save_dir=save_dir, page_num=original_page_num
        )

        artifacts = [
            (await async_write_native_json(
                save_dir,
                self.name,
                f"page_{original_page_num:04d}_raw.json",
                md_content,
                kind="raw",
            )).__dict__
        ]

        if md_content:
            artifacts.append(
                (await async_write_native_text(
                    save_dir,
                    self.name,
                    f"page_{original_page_num:04d}.md",
                    md_content,
                    kind="markdown",
                )).__dict__
            )

        execution_trace = EngineExecutionTrace(
            stages=(*execution_trace.stages, StageOutcome(stage="output", status="success")),
            fallback=execution_trace.fallback,
        )
        return EnginePageResult(
            page_no=page_idx,
            original_page_num=original_page_num,
            status="success_fallback_text",
            raw_response=md_content,
            md_content=md_content,
            native_artifacts=artifacts,
            execution_trace=execution_trace,
        )

    async def finalize_document(
        self, page_results: List[Dict[str, Any]], save_dir: str, filename: str
    ) -> List[Dict[str, str]]:
        md_contents = []
        for page_result in page_results:
            if isinstance(page_result, EnginePageResult):
                md_content = page_result.md_content
            else:
                md_content = page_result.get("md_content", "")
            if md_content:
                md_contents.append(md_content.strip())
        if not md_contents:
            return []
        artifact = await async_write_native_text(
            save_dir,
            self.name,
            f"{filename}.md",
            "\n\n".join(md_contents),
            kind="document_markdown",
        )
        return [artifact.__dict__]
