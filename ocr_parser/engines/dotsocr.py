from __future__ import annotations

import asyncio
import copy
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List

from dots_ocr.utils.format_transformer_v3 import (
    ensure_section_headers_have_markers,
    filter_blocks_by_keywords,
    layoutjson2md_simple_extract,
)
from dots_ocr.utils.layout_utils import post_process_output

from .base import (
    EngineCapabilities,
    EngineExecutionTrace,
    EnginePageResult,
    FallbackInfo,
    StageOutcome,
)
from ..infra.failure_category import infer_failure_category


@dataclass(frozen=True)
class _PageContext:
    parser: Any
    page_data: Mapping[str, Any]
    origin_path: Any
    processed_path: Any
    origin_image: Any
    processed_image: Any
    original_page_num: int
    page_idx: int
    save_dir: Any
    prompt_mode: str
    prompt: Any
    payload: Any
    processed_size_for_layout: Any


@dataclass
class _AttemptState:
    attempt_limit: int
    attempts_used: int = 0
    last_error: BaseException | None = None
    last_raw_response: Any = None
    use_concurrent_for_data_issue: bool = False
    concurrent_issue_reason: str = "malformed model output"

    @property
    def remaining(self) -> int:
        return self.attempt_limit - self.attempts_used


@dataclass(frozen=True)
class _ProcessedAttempt:
    response: str
    cells: Any
    original_cells: Any
    origin_image: Any


def _open_image_size(path: str) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


async def _prepare_page_context(parser: Any, page_data: Dict[str, Any]) -> _PageContext:
    snapshot = MappingProxyType(dict(page_data))
    origin_path = snapshot.get("origin_image_path")
    processed_path = snapshot.get("processed_image_path")
    origin_image = snapshot.get("origin_image")
    processed_image = snapshot.get("processed_image")
    original_page_num = snapshot["original_page_num"]
    prompt_mode = snapshot["prompt_mode"]
    processed_size = snapshot.get("processed_size")
    bbox = snapshot.get("bbox")
    loop = asyncio.get_running_loop()
    if bbox is not None and prompt_mode == "prompt_grounding_ocr":
        if processed_image is not None:
            width, height = processed_image.size
        elif processed_size:
            width, height = processed_size
        elif processed_path:
            width, height = await loop.run_in_executor(
                None,
                _open_image_size,
                processed_path,
            )
        else:
            width, height = (0, 0)
        prompt = parser.get_prompt(
            prompt_mode,
            bbox=bbox,
            origin_image=None,
            image=type("Tmp", (), {"width": width, "height": height})(),
        )
    else:
        prompt = parser.get_prompt(prompt_mode)
    payload = processed_path if processed_path else (processed_image or origin_image)
    processed_size_for_layout = processed_size
    if processed_image is not None:
        processed_size_for_layout = processed_image.size
    elif processed_size_for_layout is None and processed_path:
        processed_size_for_layout = await loop.run_in_executor(
            None,
            _open_image_size,
            processed_path,
        )
    return _PageContext(
        parser=parser,
        page_data=snapshot,
        origin_path=origin_path,
        processed_path=processed_path,
        origin_image=origin_image,
        processed_image=processed_image,
        original_page_num=original_page_num,
        page_idx=snapshot["page_idx"],
        save_dir=snapshot.get("save_dir"),
        prompt_mode=prompt_mode,
        prompt=prompt,
        payload=payload,
        processed_size_for_layout=processed_size_for_layout,
    )


async def _post_process_response(
    context: _PageContext,
    response: str,
) -> _ProcessedAttempt:
    parser = context.parser
    loop = asyncio.get_running_loop()

    def do_post_process() -> tuple[Any, Any, Any]:
        from PIL import Image

        processed_image = context.processed_image
        if processed_image is None:
            with Image.open(context.processed_path) as image:
                processed_image = image.convert("RGB").copy()
        origin_image = context.origin_image
        if origin_image is None:
            with Image.open(context.origin_path) as image:
                origin_image = image.convert("RGB").copy()
        cells, _ = post_process_output(
            response,
            context.prompt_mode,
            origin_image,
            processed_image,
            min_pixels=parser.min_pixels,
            max_pixels=parser.max_pixels,
        )
        needs_original_cells = bool(
            parser.save_page_layout
            or parser.generate_origin_md
            or parser.filter_keywords
        )
        original_cells = copy.deepcopy(cells) if needs_original_cells else None
        if (
            parser.trim_first_page_summary
            and context.original_page_num == 1
            and isinstance(cells, list)
        ):
            cells = parser._trim_first_page_blocks(cells)
        if parser.filter_keywords:
            cells = filter_blocks_by_keywords(
                cells,
                parser.filter_keywords,
                parser.categories_to_filter,
            )
        if isinstance(cells, list):
            ensure_section_headers_have_markers(cells)
        return cells, origin_image, original_cells

    semaphore = getattr(parser, "postprocess_semaphore", None)
    if semaphore is None:
        cells, origin_image, original_cells = await loop.run_in_executor(
            None,
            do_post_process,
        )
    else:
        async with semaphore:
            cells, origin_image, original_cells = await loop.run_in_executor(
                None,
                do_post_process,
            )
    return _ProcessedAttempt(
        response=response,
        cells=cells,
        original_cells=original_cells,
        origin_image=origin_image,
    )


def _success_trace() -> EngineExecutionTrace:
    return EngineExecutionTrace(
        stages=(
            StageOutcome(stage="primary_inference", status="success"),
            StageOutcome(stage="postprocess", status="success"),
        ),
        fallback=FallbackInfo(),
    )


def _failed_primary_stages(state: _AttemptState) -> tuple[StageOutcome, ...]:
    category = infer_failure_category({"error": str(state.last_error or "")})
    if state.last_raw_response:
        return (
            StageOutcome(stage="primary_inference", status="success"),
            StageOutcome(
                stage="postprocess",
                status="failed",
                failure_category=category,
            ),
        )
    return (
        StageOutcome(
            stage="primary_inference",
            status="failed",
            failure_category=category,
        ),
        StageOutcome(stage="postprocess", status="skipped"),
    )


async def _refine_table_blocks(
    context: _PageContext,
    attempt: _ProcessedAttempt,
    *,
    concurrent: bool,
) -> None:
    parser = context.parser
    if not parser.enable_table_reparse or parser._table_reparse_stack != 0:
        return
    try:
        await parser._maybe_refine_table_blocks(
            page_number=context.original_page_num,
            cells=attempt.cells,
            origin_image=attempt.origin_image,
            original_cells=attempt.original_cells,
            filename=context.page_data.get("filename"),
            prompt_mode=context.prompt_mode,
            save_dir=context.save_dir,
        )
    except Exception as exc:
        suffix = " during concurrent retry" if concurrent else ""
        parser._console_write(
            f"Table reparse encountered an error on page "
            f"{context.original_page_num}{suffix}: {exc}",
            level="warning",
        )


async def _save_success_result(
    context: _PageContext,
    attempt: _ProcessedAttempt,
    *,
    concurrent: bool,
) -> EnginePageResult:
    parser = context.parser
    save_name = (
        f"{Path(context.page_data['filename']).stem}_page_"
        f"{context.original_page_num}"
    )
    json_path, layout_path = await parser._save_intermediate_outputs_async(
        context.save_dir,
        save_name,
        attempt.response,
        attempt.cells,
        attempt.original_cells,
        attempt.origin_image,
        page_number=context.original_page_num,
        processed_size=context.processed_size_for_layout,
    )
    if concurrent:
        parser._console_write(
            f"Concurrent retry succeeded for page {context.original_page_num}.",
            level="always",
        )
    return EnginePageResult(
        page_no=context.page_idx,
        original_page_num=context.original_page_num,
        status="success",
        cells=attempt.cells,
        original_cells=attempt.original_cells,
        page_json_path=json_path,
        page_layout_path=layout_path,
        execution_trace=_success_trace(),
    )


def _record_data_issue(
    context: _PageContext,
    state: _AttemptState,
    exc: Exception,
    reason: str,
) -> None:
    parser = context.parser
    state.last_error = exc
    recorder = getattr(parser, "record_api_error", None)
    if callable(recorder):
        recorder(exc, stage="model_output")
    parser.monitor.record_error(type(exc).__name__)
    if state.remaining <= 0:
        return
    if parser.concurrent_retries and parser.concurrent_retries > 1:
        state.use_concurrent_for_data_issue = True
        state.concurrent_issue_reason = reason
        lanes = min(parser.concurrent_retries, state.remaining)
        parser._console_write(
            f"{reason} on page {context.original_page_num}. Switching to "
            f"concurrent retry mode with {lanes} lane(s).",
            level="always",
        )
    else:
        parser._console_write(
            f"{reason} on page {context.original_page_num}, but concurrent "
            "retries are unavailable. Continuing serial retries.",
            level="warning",
        )


async def _run_serial_primary_attempt(
    context: _PageContext,
    state: _AttemptState,
) -> tuple[EnginePageResult | None, bool]:
    parser = context.parser
    try:
        response, attempts_used = await parser._run_inference_with_retries(
            context.payload,
            context.prompt,
            context.original_page_num,
            use_race=False,
            max_attempts=state.remaining,
        )
    except Exception as exc:
        state.last_error = exc
        return None, True
    state.attempts_used += attempts_used
    state.last_raw_response = response
    try:
        attempt = await _post_process_response(context, response)
    except Exception as exc:
        _record_data_issue(
            context,
            state,
            exc,
            f"Post-processing failed ({type(exc).__name__})",
        )
        return None, False
    try:
        parser._validate_cells_structure(attempt.cells)
    except parser.NonStandardModelOutputError as exc:
        _record_data_issue(
            context,
            state,
            exc,
            f"Malformed layout output ({exc})",
        )
        return None, False
    await _refine_table_blocks(context, attempt, concurrent=False)
    return await _save_success_result(context, attempt, concurrent=False), False


async def _run_concurrent_data_issue_retry(
    context: _PageContext,
    state: _AttemptState,
    reason: str,
) -> EnginePageResult | None:
    parser = context.parser
    attempts = min(parser.concurrent_retries or 0, state.remaining)
    if attempts <= 1:
        return None
    parser._console_write(
        f"Concurrent retry triggered ({attempts} lanes) on page "
        f"{context.original_page_num} - {reason}.",
        level="info",
    )
    parser.monitor.record_retry(attempts)
    try:
        response, _ = await parser._run_inference_with_retries(
            context.payload,
            context.prompt,
            context.original_page_num,
            use_race=True,
            max_attempts=1,
            race_attempts=attempts,
        )
        state.last_raw_response = response
        attempt = await _post_process_response(context, response)
        parser._validate_cells_structure(attempt.cells)
        await _refine_table_blocks(context, attempt, concurrent=True)
        return await _save_success_result(context, attempt, concurrent=True)
    except Exception as exc:
        state.last_error = exc
        recorder = getattr(parser, "record_api_error", None)
        if callable(recorder):
            recorder(exc, stage="concurrent_retry")
        parser.monitor.record_error(type(exc).__name__)
        detail = f": {exc}" if str(exc) else ""
        parser._console_write(
            f"Concurrent retry ({attempts}) failed for page "
            f"{context.original_page_num}: {type(exc).__name__}{detail}",
            level="warning",
        )
        return None
    finally:
        state.attempts_used += attempts


async def _run_final_concurrent_retry(
    context: _PageContext,
    state: _AttemptState,
) -> EnginePageResult | None:
    parser = context.parser
    if not (
        parser.concurrent_retries
        and parser.concurrent_retries > 1
        and state.remaining > 1
    ):
        return None
    attempts = min(parser.concurrent_retries, state.remaining)
    try:
        parser.monitor.record_retry(attempts)
        parser._console_write(
            f"Racing {attempts} concurrent requests for page "
            f"{context.original_page_num}...",
            level="always",
        )
        response, _ = await parser._run_inference_with_retries(
            context.payload,
            context.prompt,
            context.original_page_num,
            use_race=True,
            max_attempts=1,
            race_attempts=attempts,
        )
        state.last_raw_response = response
        attempt = await _post_process_response(context, response)
        parser._validate_cells_structure(attempt.cells)
        await _refine_table_blocks(context, attempt, concurrent=True)
        return await _save_success_result(context, attempt, concurrent=True)
    except Exception as exc:
        state.last_error = exc
        parser.monitor.record_error(type(exc).__name__)
        parser._console_write(
            f"Concurrent retries failed for page {context.original_page_num}: "
            f"{type(exc).__name__}",
            level="always",
        )
        return None
    finally:
        state.attempts_used += attempts


async def _run_primary_attempts(
    context: _PageContext,
    state: _AttemptState,
) -> EnginePageResult | None:
    parser = context.parser
    while state.attempts_used < state.attempt_limit:
        if state.use_concurrent_for_data_issue:
            if state.remaining <= 1:
                state.use_concurrent_for_data_issue = False
                parser._console_write(
                    f"Only {state.remaining} retry slot(s) left for page "
                    f"{context.original_page_num}; falling back to serial retries.",
                    level="warning",
                )
                continue
            result = await _run_concurrent_data_issue_retry(
                context,
                state,
                state.concurrent_issue_reason,
            )
            if result is not None:
                return result
            if state.attempts_used >= state.attempt_limit:
                break
            continue
        result, stop = await _run_serial_primary_attempt(context, state)
        if result is not None:
            return result
        if stop:
            break
    if state.last_error is not None:
        parser._console_write(
            f"Page {context.original_page_num} primary attempt failed: "
            f"{type(state.last_error).__name__}.",
            level="always",
        )
    return await _run_final_concurrent_retry(context, state)


async def _text_fallback_result(
    context: _PageContext,
    state: _AttemptState,
) -> EnginePageResult | None:
    if not state.last_raw_response:
        return None
    parser = context.parser

    def extract_text() -> Any:
        from PIL import Image

        if context.processed_image is not None:
            processed = context.processed_image
        else:
            with Image.open(context.processed_path) as image:
                processed = image.convert("RGB").copy()
        if context.origin_image is not None:
            origin = context.origin_image
        else:
            with Image.open(context.origin_path) as image:
                origin = image.convert("RGB").copy()
        cells, _ = post_process_output(
            state.last_raw_response,
            context.prompt_mode,
            origin,
            processed,
            min_pixels=parser.min_pixels,
            max_pixels=parser.max_pixels,
        )
        if isinstance(cells, list):
            ensure_section_headers_have_markers(cells)
        return cells

    try:
        cells = await asyncio.get_running_loop().run_in_executor(None, extract_text)
        if cells and isinstance(cells, list):
            cells = parser._merge_adjacent_text_blocks_in_same_page(cells)
            markdown = layoutjson2md_simple_extract(cells, "text", True)
            if markdown and markdown.strip():
                parser._console_write(
                    f"Text extraction fallback succeeded for page "
                    f"{context.original_page_num}.",
                    level="info",
                )
                return EnginePageResult(
                    page_no=context.page_idx,
                    original_page_num=context.original_page_num,
                    status="success_fallback_text",
                    md_content=markdown,
                    cells=[],
                    page_json_path=None,
                    page_layout_path=None,
                    execution_trace=EngineExecutionTrace(
                        stages=(
                            *_failed_primary_stages(state),
                            StageOutcome(stage="text_fallback", status="success"),
                        ),
                        fallback=FallbackInfo(
                            used=True,
                            reason="primary_stage_failed",
                            source_stage=(
                                "postprocess"
                                if state.last_raw_response
                                else "primary_inference"
                            ),
                        ),
                    ),
                )
    except Exception as exc:
        parser._console_write(
            f"Text extraction fallback also failed for page "
            f"{context.original_page_num}. Reason: {exc}",
            level="error",
        )
    return None


async def _image_fallback_result(
    context: _PageContext,
    state: _AttemptState,
) -> EnginePageResult:
    parser = context.parser
    parser._console_write(
        f"Page {context.original_page_num} failed text extraction. Saving error "
        "screenshot as final placeholder.",
        level="error",
    )
    try:
        images_dir = os.path.join(context.save_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        filename = f"page_{context.original_page_num}_bad.jpg"
        path = os.path.join(images_dir, filename)

        def save_bad_image() -> None:
            from PIL import Image

            if context.origin_image is not None:
                origin = context.origin_image
                if origin.mode != "RGB":
                    origin = origin.convert("RGB")
                origin.save(path, "JPEG", quality=95)
            else:
                with Image.open(context.origin_path) as image:
                    origin = image.convert("RGB")
                    origin.save(path, "JPEG", quality=95)

        await asyncio.get_running_loop().run_in_executor(None, save_bad_image)
        if parser.badcase_collection_dir:
            try:
                date_dir = os.path.join(
                    parser.badcase_collection_dir,
                    datetime.now().strftime("%Y-%m-%d"),
                )
                os.makedirs(date_dir, exist_ok=True)
                source_name = Path(
                    context.page_data.get("filename", "unknown_file")
                ).stem
                badcase_name = (
                    f"{source_name}_page_{context.original_page_num}.jpg"
                )
                shutil.copyfile(path, os.path.join(date_dir, badcase_name))
            except Exception as exc:
                parser._console_write(
                    f"WARNING: Failed to copy badcase screenshot. Reason: {exc}",
                    level="warning",
                )
        parser._console_write(
            f"Fallback succeeded. Screenshot for page "
            f"{context.original_page_num} saved.",
            level="info",
        )
        return EnginePageResult(
            page_no=context.page_idx,
            original_page_num=context.original_page_num,
            status="success_fallback_image",
            md_content=f"![{filename}](images/{filename})",
            cells=[],
            page_json_path=None,
            page_layout_path=None,
            execution_trace=EngineExecutionTrace(
                stages=(
                    *_failed_primary_stages(state),
                    StageOutcome(
                        stage="text_fallback",
                        status="failed",
                        failure_category="parser_failed",
                    ),
                    StageOutcome(stage="image_fallback", status="success"),
                ),
                fallback=FallbackInfo(
                    used=True,
                    reason="text_fallback_unavailable",
                    source_stage="text_fallback",
                ),
            ),
        )
    except Exception as exc:
        parser._console_write(
            f"CRITICAL: Image fallback failed for page "
            f"{context.original_page_num}. Reason: {exc}",
            level="error",
        )
        return EnginePageResult(
            page_no=context.page_idx,
            original_page_num=context.original_page_num,
            status="error",
            error=(
                f"{type(state.last_error).__name__}: {state.last_error} | "
                f"plus: {exc}"
            ),
            page_json_path=None,
            page_layout_path=None,
            execution_trace=EngineExecutionTrace(
                stages=(
                    *_failed_primary_stages(state),
                    StageOutcome(
                        stage="text_fallback",
                        status="failed",
                        failure_category="parser_failed",
                    ),
                    StageOutcome(
                        stage="image_fallback",
                        status="failed",
                        failure_category="output_unwritable",
                    ),
                ),
                fallback=FallbackInfo(
                    used=True,
                    reason="text_fallback_unavailable",
                    source_stage="text_fallback",
                ),
            ),
        )


async def _fallback_result(
    context: _PageContext,
    state: _AttemptState,
) -> EnginePageResult:
    error_name = type(state.last_error).__name__ if state.last_error else "UnknownError"
    context.parser._console_write(
        f"Page {context.original_page_num} failed all attempts. Last known error: "
        f"{error_name}. Attempting fallbacks.",
        level="error",
    )
    text_result = await _text_fallback_result(context, state)
    if text_result is not None:
        return text_result
    return await _image_fallback_result(context, state)


class DotsOCREngine:
    name = "dotsocr"
    capabilities = EngineCapabilities(
        uses_shared_postprocess=True,
        emits_native_artifacts=False,
    )

    def __init__(self, parser: Any):
        self.parser = parser

    async def process_page(self, page_data: Dict[str, Any]) -> EnginePageResult:
        context = await _prepare_page_context(self.parser, page_data)
        retry_limit = self.parser.max_retries if self.parser.max_retries else 1
        state = _AttemptState(attempt_limit=max(retry_limit, 1))
        result = await _run_primary_attempts(context, state)
        if result is not None:
            return result
        return await _fallback_result(context, state)

    async def finalize_document(
        self, page_results: List[Dict[str, Any]], save_dir: str, filename: str
    ) -> List[Dict[str, str]]:
        return []
