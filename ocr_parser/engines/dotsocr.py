from __future__ import annotations

import asyncio
import copy
import os
import shutil
from datetime import datetime
from pathlib import Path
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


class DotsOCREngine:
    name = "dotsocr"
    capabilities = EngineCapabilities(
        uses_shared_postprocess=True,
        emits_native_artifacts=False,
    )

    def __init__(self, parser: Any):
        self.parser = parser

    async def process_page(self, page_data: Dict[str, Any]) -> EnginePageResult:
        parser = self.parser
        origin_path = page_data.get("origin_image_path")
        processed_path = page_data.get("processed_image_path")
        origin_image_obj = page_data.get("origin_image")
        processed_image_obj = page_data.get("processed_image")

        original_page_num = page_data["original_page_num"]
        page_idx = page_data["page_idx"]
        save_dir = page_data.get("save_dir")
        prompt_mode = page_data["prompt_mode"]
        bbox = page_data.get("bbox")
        processed_size_meta = page_data.get("processed_size")

        last_error = None
        last_raw_response_for_fallback = None

        def _open_for_size(path: str):
            from PIL import Image

            with Image.open(path) as image:
                return image.size

        if bbox is not None and prompt_mode == "prompt_grounding_ocr":
            if processed_image_obj is not None:
                width, height = processed_image_obj.size
            elif processed_size_meta:
                width, height = processed_size_meta
            elif processed_path:
                width, height = await asyncio.get_running_loop().run_in_executor(None, _open_for_size, processed_path)
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

        payload = processed_path if processed_path else (processed_image_obj or origin_image_obj)
        processed_size_for_layout = processed_size_meta
        if processed_image_obj is not None:
            processed_size_for_layout = processed_image_obj.size
        elif processed_size_for_layout is None and processed_path:
            processed_size_for_layout = await asyncio.get_running_loop().run_in_executor(None, _open_for_size, processed_path)

        async def _post_process_response(response_text: str):
            loop = asyncio.get_running_loop()

            def _do_post_process():
                from PIL import Image

                proc_img = processed_image_obj
                if proc_img is None:
                    with Image.open(processed_path) as image:
                        proc_img = image.convert("RGB").copy()

                orig_img = origin_image_obj
                if orig_img is None:
                    with Image.open(origin_path) as image:
                        orig_img = image.convert("RGB").copy()

                cells_local, _ = post_process_output(
                    response_text,
                    prompt_mode,
                    orig_img,
                    proc_img,
                    min_pixels=parser.min_pixels,
                    max_pixels=parser.max_pixels,
                )
                needs_original_cells = bool(parser.save_page_layout or parser.generate_origin_md or parser.filter_keywords)
                cells_original = copy.deepcopy(cells_local) if needs_original_cells else None
                if parser.trim_first_page_summary and original_page_num == 1 and isinstance(cells_local, list):
                    cells_local = parser._trim_first_page_blocks(cells_local)
                if parser.filter_keywords:
                    cells_local = filter_blocks_by_keywords(cells_local, parser.filter_keywords, parser.categories_to_filter)

                if isinstance(cells_local, list):
                    ensure_section_headers_have_markers(cells_local)
                return cells_local, orig_img, cells_original

            postprocess_semaphore = getattr(parser, "postprocess_semaphore", None)
            if postprocess_semaphore is None:
                return await loop.run_in_executor(None, _do_post_process)

            async with postprocess_semaphore:
                return await loop.run_in_executor(None, _do_post_process)

        primary_attempt_limit = parser.max_retries if parser.max_retries else 1
        primary_attempt_limit = max(primary_attempt_limit, 1)
        primary_attempts_used = 0
        primary_success = False
        use_concurrent_for_data_issue = False
        concurrent_issue_reason = "malformed model output"

        def _success_trace() -> EngineExecutionTrace:
            return EngineExecutionTrace(
                stages=(
                    StageOutcome(stage="primary_inference", status="success"),
                    StageOutcome(stage="postprocess", status="success"),
                ),
                fallback=FallbackInfo(),
            )

        def _failed_primary_stages() -> tuple[StageOutcome, ...]:
            category = infer_failure_category({"error": str(last_error or "")})
            if last_raw_response_for_fallback:
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

        async def _run_concurrent_retries_for_data_issue(reason_label: str):
            nonlocal primary_attempts_used, last_error, last_raw_response_for_fallback, primary_success

            remaining_budget_local = primary_attempt_limit - primary_attempts_used
            attempts_for_race_local = min(parser.concurrent_retries or 0, remaining_budget_local)
            if attempts_for_race_local <= 1:
                return None

            parser._console_write(
                f"Concurrent retry triggered ({attempts_for_race_local} lanes) on page {original_page_num} - {reason_label}.",
                level="info",
            )
            parser.monitor.record_retry(attempts_for_race_local)

            race_origin_image = None
            try:
                response_race, _ = await parser._run_inference_with_retries(
                    payload,
                    prompt,
                    original_page_num,
                    use_race=True,
                    max_attempts=1,
                    race_attempts=attempts_for_race_local,
                )
                last_raw_response_for_fallback = response_race
                race_cells, race_origin_image, race_original_cells = await _post_process_response(response_race)
                parser._validate_cells_structure(race_cells)

                if parser.enable_table_reparse and parser._table_reparse_stack == 0:
                    try:
                        await parser._maybe_refine_table_blocks(
                            page_number=original_page_num,
                            cells=race_cells,
                            origin_image=race_origin_image,
                            original_cells=race_original_cells,
                            filename=page_data.get("filename"),
                            prompt_mode=prompt_mode,
                            save_dir=save_dir,
                        )
                    except Exception as refine_exc:
                        parser._console_write(
                            f"Table reparse encountered an error on page {original_page_num} during concurrent retry: {refine_exc}",
                            level="warning",
                        )

                save_name_base = f"{Path(page_data['filename']).stem}_page_{original_page_num}"
                json_path, layout_path = await parser._save_intermediate_outputs_async(
                    save_dir,
                    save_name_base,
                    response_race,
                    race_cells,
                    race_original_cells,
                    race_origin_image,
                    page_number=original_page_num,
                    processed_size=processed_size_for_layout,
                )

                primary_success = True
                parser._console_write(f"Concurrent retry succeeded for page {original_page_num}.", level="always")
                return EnginePageResult(
                    page_no=page_idx,
                    original_page_num=original_page_num,
                    status="success",
                    cells=race_cells,
                    original_cells=race_original_cells,
                    page_json_path=json_path,
                    page_layout_path=layout_path,
                    execution_trace=_success_trace(),
                )
            except Exception as race_exc:
                last_error = race_exc
                recorder = getattr(parser, "record_api_error", None)
                if callable(recorder):
                    recorder(race_exc, stage="concurrent_retry")
                parser.monitor.record_error(type(race_exc).__name__)
                detail = f": {race_exc}" if str(race_exc) else ""
                parser._console_write(
                    f"Concurrent retry ({attempts_for_race_local}) failed for page {original_page_num}: "
                    f"{type(race_exc).__name__}{detail}",
                    level="warning",
                )
                return None
            finally:
                primary_attempts_used += attempts_for_race_local
                if race_origin_image is not None:
                    del race_origin_image

        def _record_data_issue(exc: Exception, reason: str):
            nonlocal last_error, use_concurrent_for_data_issue, concurrent_issue_reason

            last_error = exc
            recorder = getattr(parser, "record_api_error", None)
            if callable(recorder):
                recorder(exc, stage="model_output")
            parser.monitor.record_error(type(exc).__name__)
            remaining_after = primary_attempt_limit - primary_attempts_used
            if remaining_after <= 0:
                return
            if parser.concurrent_retries and parser.concurrent_retries > 1:
                use_concurrent_for_data_issue = True
                concurrent_issue_reason = reason
                lanes = min(parser.concurrent_retries, remaining_after)
                parser._console_write(
                    f"{reason} on page {original_page_num}. Switching to concurrent retry mode with {lanes} lane(s).",
                    level="always",
                )
            else:
                parser._console_write(
                    f"{reason} on page {original_page_num}, but concurrent retries are unavailable. Continuing serial retries.",
                    level="warning",
                )

        while primary_attempts_used < primary_attempt_limit:
            if use_concurrent_for_data_issue:
                remaining_for_race = primary_attempt_limit - primary_attempts_used
                if remaining_for_race <= 1:
                    use_concurrent_for_data_issue = False
                    parser._console_write(
                        f"Only {remaining_for_race} retry slot(s) left for page {original_page_num}; falling back to serial retries.",
                        level="warning",
                    )
                    continue

                concurrent_result = await _run_concurrent_retries_for_data_issue(concurrent_issue_reason)
                if concurrent_result:
                    return concurrent_result
                if primary_attempts_used >= primary_attempt_limit:
                    break
                continue

            remaining_budget = primary_attempt_limit - primary_attempts_used
            try:
                response, attempts_used = await parser._run_inference_with_retries(
                    payload, prompt, original_page_num, use_race=False, max_attempts=remaining_budget
                )
            except Exception as exc:
                last_error = exc
                break

            primary_attempts_used += attempts_used
            last_raw_response_for_fallback = response

            origin_image_for_saving = None
            try:
                cells, origin_image_for_saving, original_cells = await _post_process_response(response)
            except Exception as post_exc:
                if origin_image_for_saving is not None:
                    del origin_image_for_saving
                _record_data_issue(post_exc, f"Post-processing failed ({type(post_exc).__name__})")
                continue

            try:
                parser._validate_cells_structure(cells)
            except parser.NonStandardModelOutputError as structure_exc:
                if origin_image_for_saving is not None:
                    del origin_image_for_saving
                _record_data_issue(structure_exc, f"Malformed layout output ({structure_exc})")
                continue

            if parser.enable_table_reparse and parser._table_reparse_stack == 0:
                try:
                    await parser._maybe_refine_table_blocks(
                        page_number=original_page_num,
                        cells=cells,
                        origin_image=origin_image_for_saving,
                        original_cells=original_cells,
                        filename=page_data.get("filename"),
                        prompt_mode=prompt_mode,
                        save_dir=save_dir,
                    )
                except Exception as refine_exc:
                    parser._console_write(
                        f"Table reparse encountered an error on page {original_page_num}: {refine_exc}",
                        level="warning",
                    )

            save_name_base = f"{Path(page_data['filename']).stem}_page_{original_page_num}"
            json_path, layout_path = await parser._save_intermediate_outputs_async(
                save_dir,
                save_name_base,
                response,
                cells,
                original_cells,
                origin_image_for_saving,
                page_number=original_page_num,
                processed_size=processed_size_for_layout,
            )

            if origin_image_for_saving is not None:
                del origin_image_for_saving

            primary_success = True
            return EnginePageResult(
                page_no=page_idx,
                original_page_num=original_page_num,
                status="success",
                cells=cells,
                original_cells=original_cells,
                page_json_path=json_path,
                page_layout_path=layout_path,
                execution_trace=_success_trace(),
            )

        if not primary_success and last_error is not None:
            parser._console_write(
                f"Page {original_page_num} primary attempt failed: {type(last_error).__name__}.",
                level="always",
            )

        remaining_budget_after_serial = primary_attempt_limit - primary_attempts_used
        if parser.concurrent_retries and parser.concurrent_retries > 1 and remaining_budget_after_serial > 1:
            attempts_for_race = min(parser.concurrent_retries, remaining_budget_after_serial)
            race_origin_image = None
            try:
                parser.monitor.record_retry(attempts_for_race)
                parser._console_write(
                    f"Racing {attempts_for_race} concurrent requests for page {original_page_num}...",
                    level="always",
                )
                response, _ = await parser._run_inference_with_retries(
                    payload,
                    prompt,
                    original_page_num,
                    use_race=True,
                    max_attempts=1,
                    race_attempts=attempts_for_race,
                )
                last_raw_response_for_fallback = response

                cells, race_origin_image, race_original_cells = await _post_process_response(response)
                parser._validate_cells_structure(cells)

                if parser.enable_table_reparse and parser._table_reparse_stack == 0:
                    try:
                        await parser._maybe_refine_table_blocks(
                            page_number=original_page_num,
                            cells=cells,
                            origin_image=race_origin_image,
                            original_cells=race_original_cells,
                            filename=page_data.get("filename"),
                            prompt_mode=prompt_mode,
                            save_dir=save_dir,
                        )
                    except Exception as refine_exc:
                        parser._console_write(
                            f"Table reparse encountered an error on page {original_page_num} during concurrent retry: {refine_exc}",
                            level="warning",
                        )

                save_name_base = f"{Path(page_data['filename']).stem}_page_{original_page_num}"
                json_path, layout_path = await parser._save_intermediate_outputs_async(
                    save_dir,
                    save_name_base,
                    response,
                    cells,
                    race_original_cells,
                    race_origin_image,
                    page_number=original_page_num,
                    processed_size=processed_size_for_layout,
                )

                primary_success = True
                parser._console_write(f"Concurrent retry succeeded for page {original_page_num}.", level="always")
                return EnginePageResult(
                    page_no=page_idx,
                    original_page_num=original_page_num,
                    status="success",
                    cells=cells,
                    original_cells=race_original_cells,
                    page_json_path=json_path,
                    page_layout_path=layout_path,
                    execution_trace=_success_trace(),
                )
            except Exception as exc:
                last_error = exc
                parser.monitor.record_error(type(exc).__name__)
                parser._console_write(
                    f"Concurrent retries failed for page {original_page_num}: {type(exc).__name__}",
                    level="always",
                )
            finally:
                primary_attempts_used += attempts_for_race
                if race_origin_image is not None:
                    del race_origin_image

        error_name = type(last_error).__name__ if last_error else "UnknownError"
        parser._console_write(
            f"Page {original_page_num} failed all attempts. Last known error: {error_name}. Attempting fallbacks.",
            level="error",
        )
        if last_raw_response_for_fallback:
            try:

                def _text_fallback_sync():
                    from PIL import Image

                    if processed_image_obj is not None:
                        proc = processed_image_obj
                    else:
                        with Image.open(processed_path) as image:
                            proc = image.convert("RGB").copy()
                    if origin_image_obj is not None:
                        orig = origin_image_obj
                    else:
                        with Image.open(origin_path) as image:
                            orig = image.convert("RGB").copy()
                    cells_for_text, _ = post_process_output(
                        last_raw_response_for_fallback,
                        prompt_mode,
                        orig,
                        proc,
                        min_pixels=parser.min_pixels,
                        max_pixels=parser.max_pixels,
                    )
                    if isinstance(cells_for_text, list):
                        ensure_section_headers_have_markers(cells_for_text)
                    return cells_for_text

                cells_for_text = await asyncio.get_running_loop().run_in_executor(None, _text_fallback_sync)
                if cells_for_text and isinstance(cells_for_text, list):
                    cells_for_text = parser._merge_adjacent_text_blocks_in_same_page(cells_for_text)
                    md_content_simple = layoutjson2md_simple_extract(cells_for_text, "text", True)
                    if md_content_simple and md_content_simple.strip():
                        parser._console_write(
                            f"Text extraction fallback succeeded for page {original_page_num}.",
                            level="info",
                        )
                        return EnginePageResult(
                            page_no=page_idx,
                            original_page_num=original_page_num,
                            status="success_fallback_text",
                            md_content=md_content_simple,
                            cells=[],
                            page_json_path=None,
                            page_layout_path=None,
                            execution_trace=EngineExecutionTrace(
                                stages=(*_failed_primary_stages(), StageOutcome(stage="text_fallback", status="success")),
                                fallback=FallbackInfo(
                                    used=True,
                                    reason="primary_stage_failed",
                                    source_stage=(
                                        "postprocess"
                                        if last_raw_response_for_fallback
                                        else "primary_inference"
                                    ),
                                ),
                            ),
                        )
            except Exception as text_fallback_error:
                parser._console_write(
                    f"Text extraction fallback also failed for page {original_page_num}. Reason: {text_fallback_error}",
                    level="error",
                )

        parser._console_write(
            f"Page {original_page_num} failed text extraction. Saving error screenshot as final placeholder.",
            level="error",
        )
        try:
            images_dir = os.path.join(save_dir, "images")
            os.makedirs(images_dir, exist_ok=True)
            bad_image_filename = f"page_{original_page_num}_bad.jpg"
            bad_image_path = os.path.join(images_dir, bad_image_filename)

            def _save_bad_sync():
                from PIL import Image

                if origin_image_obj is not None:
                    orig = origin_image_obj
                    if orig.mode != "RGB":
                        orig = orig.convert("RGB")
                    orig.save(bad_image_path, "JPEG", quality=95)
                else:
                    with Image.open(origin_path) as image:
                        orig = image.convert("RGB")
                        orig.save(bad_image_path, "JPEG", quality=95)

            await asyncio.get_running_loop().run_in_executor(None, _save_bad_sync)

            if parser.badcase_collection_dir:
                try:
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    date_dir = os.path.join(parser.badcase_collection_dir, today_str)
                    os.makedirs(date_dir, exist_ok=True)
                    badcase_filename_base = Path(page_data.get("filename", "unknown_file")).stem
                    badcase_file_name = f"{badcase_filename_base}_page_{original_page_num}.jpg"
                    shutil.copyfile(bad_image_path, os.path.join(date_dir, badcase_file_name))
                except Exception as copy_error:
                    parser._console_write(
                        f"WARNING: Failed to copy badcase screenshot. Reason: {copy_error}",
                        level="warning",
                    )

            final_md = f"![{bad_image_filename}](images/{bad_image_filename})"
            parser._console_write(f"Fallback succeeded. Screenshot for page {original_page_num} saved.", level="info")
            return EnginePageResult(
                page_no=page_idx,
                original_page_num=original_page_num,
                status="success_fallback_image",
                md_content=final_md,
                cells=[],
                page_json_path=None,
                page_layout_path=None,
                execution_trace=EngineExecutionTrace(
                    stages=(
                        *_failed_primary_stages(),
                        StageOutcome(stage="text_fallback", status="failed", failure_category="parser_failed"),
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
                f"CRITICAL: Image fallback failed for page {original_page_num}. Reason: {exc}",
                level="error",
            )
            return EnginePageResult(
                page_no=page_idx,
                original_page_num=original_page_num,
                status="error",
                error=f"{type(last_error).__name__}: {last_error} | plus: {exc}",
                page_json_path=None,
                page_layout_path=None,
                execution_trace=EngineExecutionTrace(
                    stages=(
                        *_failed_primary_stages(),
                        StageOutcome(stage="text_fallback", status="failed", failure_category="parser_failed"),
                        StageOutcome(stage="image_fallback", status="failed", failure_category="output_unwritable"),
                    ),
                    fallback=FallbackInfo(
                        used=True,
                        reason="text_fallback_unavailable",
                        source_stage="text_fallback",
                    ),
                ),
            )

    async def finalize_document(
        self, page_results: List[Dict[str, Any]], save_dir: str, filename: str
    ) -> List[Dict[str, str]]:
        return []
