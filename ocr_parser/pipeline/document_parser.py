from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz

from ..contracts.execution import aggregate_execution_metadata, execution_metadata
from ..infra.metrics import DOCUMENTS, PAGES, PAGES_IN_FLIGHT, record_engine_execution_trace
from ..models import PageTask
from ..output import write_document_outputs
from ..domain.pdf_worker import process_pdf_page_worker
from ..infra.failure_category import infer_failure_category
from ..infra.resume import STATUS_SIDECAR_NAME, cleanup_incomplete_output_dir, is_file_already_processed
from ..infra.status_sidecar import write_status_sidecar
from .postprocess import run_all_post_processing_worker


def _emit_event(parser: Any, event_type: str, **payload: Any) -> None:
    event_writer = getattr(parser, "event_writer", None)
    if event_writer is not None:
        try:
            event_writer.emit(event_type, **payload)
        except Exception as exc:
            console_write = getattr(parser, "_console_write", None)
            if callable(console_write):
                console_write(f"Failed to emit OCR event {event_type}: {exc}", level="warning")


def _runtime_snapshot(parser: Any) -> dict[str, Any] | None:
    snapshot_getter = getattr(parser, "get_runtime_snapshot", None)
    if not callable(snapshot_getter):
        return None
    try:
        snapshot = snapshot_getter()
    except Exception:
        return None
    return snapshot if isinstance(snapshot, dict) else None


def _file_result_status(parser: Any, result: List[Dict[str, Any]]) -> tuple[str, Optional[str]]:
    contentful_success_statuses = {"success", "success_fallback_text", "success_fallback_image"}
    if not result:
        return "failed", "No content could be generated"

    has_contentful_success = False
    for row in result or []:
        error = row.get("error")
        if error:
            return "failed", error
        status = row.get("status")
        if not status:
            return "failed", "Missing page status"
        if status in contentful_success_statuses:
            has_contentful_success = True
        elif status != "skipped_blank":
            return "failed", error or f"Unexpected page status: {status}"
    if not has_contentful_success:
        return "failed", "No content could be generated"
    return "success", None


async def process_single_page(parser: Any, page_data: Dict[str, Any]) -> Dict[str, Any]:
    engine_result = await parser.ocr_engine.process_page(page_data)
    return engine_result.to_layout_result()


async def _page_producer(
    parser: Any,
    *,
    input_path: str,
    page_queue: "asyncio.Queue[Optional[int]]",
    num_consumers: int,
) -> tuple[int, int]:
    total_pages_expected = 0
    total_pdf_pages = 0

    try:
        with fitz.open(input_path) as doc:
            total_pdf_pages = doc.page_count or 0

        if total_pdf_pages == 0:
            parser._console_write(
                f"Warning: File {Path(input_path).name} has 0 pages or could not be opened.",
                level="warning",
            )
            return 0, 0

        effective_limit = 0
        if parser.run_data_index and parser.index_page_limit:
            try:
                limit_val = int(parser.index_page_limit)
                if limit_val > 0:
                    effective_limit = limit_val
            except (TypeError, ValueError):
                effective_limit = 0

        total_pages_expected = total_pdf_pages
        if effective_limit:
            total_pages_expected = min(total_pdf_pages, effective_limit)

        shutdown_event = getattr(parser, "_shutdown_event", None)
        pages_queued = 0
        for page_idx in range(total_pages_expected):
            if shutdown_event and shutdown_event.is_set():
                parser._console_write(
                    f"Graceful shutdown: stopping page production at "
                    f"{pages_queued}/{total_pages_expected} pages queued.",
                    level="warning",
                )
                total_pages_expected = pages_queued
                break
            await page_queue.put(page_idx)
            pages_queued += 1
    finally:
        for _ in range(num_consumers):
            await page_queue.put(None)

    return total_pages_expected, total_pdf_pages


async def _page_consumer(
    parser: Any,
    *,
    consumer_id: int,
    loop: asyncio.AbstractEventLoop,
    input_path: str,
    filename: str,
    prompt_mode: str,
    save_dir: str,
    page_queue: "asyncio.Queue[Optional[int]]",
    results_storage: Dict[int, Dict[str, Any]],
    tmp_dir: str,
    page_progress_callback: Any = None,
    bbox: Any = None,
    skip_blank_pages: bool = False,
) -> None:
    while True:
        page_idx = await page_queue.get()
        if page_idx is None:
            break
        page_num = page_idx + 1
        try:
            task_args = (
                input_path,
                page_idx,
                parser.dpi,
                skip_blank_pages,
                parser.blank_white_threshold,
                parser.blank_noise_threshold,
                parser.min_pixels,
                parser.max_pixels,
                tmp_dir,
            )
            render_semaphore = getattr(parser, "render_semaphore", None)
            if render_semaphore is None:
                pre = await loop.run_in_executor(parser.process_pool, process_pdf_page_worker, task_args)
            else:
                async with render_semaphore:
                    pre = await loop.run_in_executor(parser.process_pool, process_pdf_page_worker, task_args)

            if pre.get("status") != "success":
                status = pre.get("status", "processing_error")
                error = pre.get("error", "Unknown pre-processing error")
                event_error = error if status != "skipped_blank" else None
                results_storage[page_num] = {
                    "page_no": page_idx,
                    "original_page_num": page_num,
                    "status": status,
                    "error": event_error,
                }
                PAGES.labels(status=status).inc()
                _emit_event(
                    parser,
                    "page_done",
                    file_path=input_path,
                    filename=filename,
                    page_no=page_num,
                    status=status,
                    error=event_error,
                    **execution_metadata(None),
                )
                if page_progress_callback:
                    page_progress_callback()
                continue

            page_task = PageTask(
                page_idx=page_idx,
                original_page_num=page_num,
                prompt_mode=prompt_mode,
                bbox=bbox,
                filename=filename,
                save_dir=save_dir,
                origin_image_path=pre["origin_path"],
                processed_image_path=pre["processed_path"],
                origin_size=pre.get("origin_size"),
                processed_size=pre.get("processed_size"),
            )

            async with parser.page_semaphore:
                PAGES_IN_FLIGHT.inc()
                try:
                    result = await parser._process_single_page_optimized_streaming(page_task.to_payload())
                finally:
                    PAGES_IN_FLIGHT.dec()

            if "origin_image" in result:
                del result["origin_image"]
            result["origin_image_path"] = pre["origin_path"]
            result["processed_image_path"] = pre["processed_path"]
            result["origin_size"] = pre.get("origin_size")
            result["processed_size"] = pre.get("processed_size")
            results_storage[page_num] = result
            status = result.get("status", "unknown")
            error = result.get("error") if status not in parser.SUCCESS_STATUSES else None
            PAGES.labels(status=status).inc()
            trace = execution_metadata(result)
            record_engine_execution_trace(
                str(getattr(parser.ocr_engine, "name", getattr(parser, "engine", "other"))),
                trace,
            )
            _emit_event(
                parser,
                "page_done",
                file_path=input_path,
                filename=filename,
                page_no=page_num,
                status=status,
                error=error,
                **trace,
            )

            if page_progress_callback:
                page_progress_callback()
        except Exception as exc:
            import traceback

            parser._console_write(
                f"Critical error in consumer {consumer_id} for page {page_num} of {filename}: {exc}\n"
                f"{traceback.format_exc()}",
                level="error",
            )
            results_storage[page_num] = {
                "page_no": page_idx,
                "original_page_num": page_num,
                "status": "error",
                "error": str(exc),
            }
            PAGES.labels(status="error").inc()
            _emit_event(
                parser,
                "page_done",
                file_path=input_path,
                filename=filename,
                page_no=page_num,
                status="error",
                error=str(exc),
                **execution_metadata(None),
            )
            if page_progress_callback:
                page_progress_callback()


async def parse_pdf(
    parser: Any,
    input_path: str,
    filename: str,
    prompt_mode: str,
    save_dir: str,
    page_progress_callback: Any = None,
    bbox: Any = None,
    skip_blank_pages: bool = False,
) -> List[Dict[str, Any]]:
    loop = asyncio.get_running_loop()

    parser._console_write(f"[{filename}] PASS 1/3: Concurrently processing pages to extract layout data...")

    qsize = min(parser.queue_size, max(2 * parser.page_concurrency, 64))
    page_queue: "asyncio.Queue[Optional[int]]" = asyncio.Queue(maxsize=qsize)
    results_storage: Dict[int, Dict[str, Any]] = {}
    total_pages_expected = 0
    total_pdf_pages = 0
    tmp_dir = tempfile.mkdtemp(prefix=f"{filename}_tmp_", dir=save_dir)

    num_consumers = parser.page_concurrency
    producer_task = asyncio.create_task(
        _page_producer(parser, input_path=input_path, page_queue=page_queue, num_consumers=num_consumers)
    )
    consumer_tasks = [
        asyncio.create_task(
            _page_consumer(
                parser,
                consumer_id=index,
                loop=loop,
                input_path=input_path,
                filename=filename,
                prompt_mode=prompt_mode,
                save_dir=save_dir,
                page_queue=page_queue,
                results_storage=results_storage,
                tmp_dir=tmp_dir,
                page_progress_callback=page_progress_callback,
                bbox=bbox,
                skip_blank_pages=skip_blank_pages,
            )
        )
        for index in range(num_consumers)
    ]

    try:
        total_pages_expected, total_pdf_pages = await producer_task
        await asyncio.gather(*consumer_tasks)

        all_pages_layout_data = [
            results_storage.get(
                index + 1,
                {
                    "page_no": index,
                    "original_page_num": index + 1,
                    "status": "missing_result",
                    "error": "Page data was lost.",
                },
            )
            for index in range(total_pages_expected)
        ]
        all_results_for_jsonl: List[Dict[str, Any]] = []
        skipped_count = sum(1 for result in all_pages_layout_data if result.get("status") == "skipped_blank")
        hard_failed_pages_count = sum(
            1 for result in all_pages_layout_data if result.get("status") not in parser.SUCCESS_STATUSES
        )

        capabilities = getattr(parser.ocr_engine, "capabilities", None)
        uses_shared_postprocess = bool(
            getattr(capabilities, "uses_shared_postprocess", False)
        )

        native_document_artifacts = await parser.ocr_engine.finalize_document(
            all_pages_layout_data,
            save_dir,
            filename,
        )

        if uses_shared_postprocess:
            parser._console_write(f"[{filename}] PASS 2/3: Performing cross-page analysis...")
            postprocess_semaphore = getattr(parser, "postprocess_semaphore", None)
            if postprocess_semaphore is None:
                all_pages_layout_data = await loop.run_in_executor(
                    parser.process_pool, run_all_post_processing_worker, parser.init_kwargs, all_pages_layout_data
                )
            else:
                async with postprocess_semaphore:
                    all_pages_layout_data = await loop.run_in_executor(
                        parser.process_pool, run_all_post_processing_worker, parser.init_kwargs, all_pages_layout_data
                    )
        else:
            parser._console_write(f"[{filename}] PASS 2/3: Skipping cross-page analysis (native engine).")

        if hard_failed_pages_count > 0:
            parser._console_write(
                f"WARNING: File '{filename}' was processed with {hard_failed_pages_count} hard-failed page(s). "
                "Placeholders will be generated.",
                level="warning",
            )

        if not any(result.get("status") in parser.SUCCESS_STATUSES for result in all_pages_layout_data):
            if skipped_count > 0 and skipped_count == total_pages_expected:
                parser._console_write(
                    f"INFO: File '{filename}' consists entirely of {skipped_count} blank page(s). "
                    "No .md file will be generated.",
                    level="always",
                )
            else:
                parser._console_write(
                    f"ERROR: No content was extracted from '{filename}'. Aborting file creation.",
                    level="error",
                )
            await parser._flush_document_page_json(save_dir)
            return [{"error": "No content could be generated", "file_path": input_path}]

        artifacts = None
        data_index_summary = None
        if uses_shared_postprocess:
            parser._console_write(f"[{filename}] PASS 3/3: Generating final Markdown (streaming)...")
            artifacts = await write_document_outputs(
                parser,
                filename=filename,
                save_dir=save_dir,
                all_pages_layout_data=all_pages_layout_data,
                total_pages_expected=total_pages_expected,
            )

            if parser.run_data_index:
                effective_total = total_pdf_pages or total_pages_expected
                if effective_total == 0:
                    effective_total = total_pages_expected
                data_index_summary = await parser._compute_md_word_stats(
                    artifacts.combined_md_path,
                    sampled_pages=total_pages_expected,
                    total_pdf_pages=effective_total,
                    page_limit=parser.index_page_limit if parser.index_page_limit else 0,
                )
        else:
            parser._console_write(f"[{filename}] PASS 3/3: Native artifacts written; skipping shared Markdown generation.")

        for index in range(total_pages_expected):
            result = results_storage.get(index + 1, {})
            status = result.get("status", "unknown_error")
            item = {
                    "page_no": result.get("original_page_num", index + 1),
                    "file_path": input_path,
                    "status": status,
                    "filename": filename,
                    "error": result.get("error") if status not in parser.SUCCESS_STATUSES else None,
                    **execution_metadata(result),
                }
            if result.get("native_artifacts"):
                item["native_artifacts"] = [dict(artifact) for artifact in result["native_artifacts"]]
            all_results_for_jsonl.append(item)

        document_execution = aggregate_execution_metadata(all_results_for_jsonl)

        for item in all_results_for_jsonl:
            if item["status"] in parser.SUCCESS_STATUSES:
                if artifacts is not None:
                    item["output_md_path"] = artifacts.combined_md_path
                    if artifacts.origin_md_path:
                        item["origin_md_path"] = artifacts.origin_md_path
                    if artifacts.layout_pdf_path:
                        item["layout_pdf_path"] = artifacts.layout_pdf_path
                    if artifacts.document_json_path:
                        item["document_json_path"] = artifacts.document_json_path
                if native_document_artifacts:
                    item.setdefault("native_artifacts", []).extend(
                        {**artifact, **document_execution}
                        for artifact in native_document_artifacts
                    )
                if data_index_summary:
                    item.update(data_index_summary)
                break

        return all_results_for_jsonl
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def parse_file(
    parser: Any,
    input_path: str,
    output_dir: str = "",
    prompt_mode: str = "prompt_layout_all_en",
    page_progress_callback: Any = None,
    bbox: Any = None,
    skip_blank_pages: bool = False,
    rename_to: Optional[str] = None,
    manifest_input_size_bytes: Optional[int] = None,
    manifest_input_mtime_ns: Optional[int] = None,
    manifest_relative_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    output_dir = output_dir or parser.output_dir
    output_dir = os.path.abspath(output_dir)

    if rename_to:
        filename = os.path.splitext(os.path.basename(rename_to))[0]
    else:
        filename, _ = os.path.splitext(os.path.basename(input_path))

    save_dir = os.path.join(output_dir, filename)
    started_monotonic = time.monotonic()

    _emit_event(
        parser,
        "file_started",
        file_path=input_path,
        filename=filename,
        output_dir=output_dir,
    )

    resume_policy = getattr(parser, "resume_policy", None)
    may_reuse_output = (
        resume_policy.may_reuse_existing_output()
        if resume_policy is not None
        else parser.enable_resume and not parser.force_reprocess
    )
    if may_reuse_output:
        is_processed, md_path = is_file_already_processed(input_path, output_dir, filename, parser._console_write)
        if is_processed:
            parser._console_write(f"Skipping already processed file: {input_path} (MD file exists: {md_path})")
            payload = {
                "file_path": input_path,
                "filename": filename,
                "status": "skipped",
                "error": None,
                **execution_metadata(None),
            }
            runtime = _runtime_snapshot(parser)
            if runtime is not None:
                payload["runtime"] = runtime
            _emit_event(
                parser,
                "file_done",
                **payload,
            )
            return [
                {
                    "page_no": 0,
                    "original_page_num": 1,
                    "file_path": input_path,
                    "output_md_path": md_path,
                    "status": "success",
                    "error": None,
                    "filename": filename,
                    "skipped": True,
                }
            ]
        cleanup_incomplete_output_dir(output_dir, filename, parser._console_write)
    elif parser.force_reprocess:
        cleanup_incomplete_output_dir(output_dir, filename, parser._console_write)

    os.makedirs(save_dir, exist_ok=True)

    try:
        file_ext = Path(input_path).suffix.lower()
        if file_ext != ".pdf":
            raise ValueError(f"File extension {file_ext} not supported by the modular parser yet. Only .pdf is supported.")
        result = await parse_pdf(
            parser,
            input_path,
            filename,
            prompt_mode,
            save_dir,
            page_progress_callback=page_progress_callback,
            bbox=bbox,
            skip_blank_pages=skip_blank_pages,
        )
        file_status, file_error = _file_result_status(parser, result)
        write_status_sidecar(
            parser=parser,
            save_dir=save_dir,
            input_path=input_path,
            filename=filename,
            status=file_status,
            error=file_error,
            result=result,
            duration_seconds=time.monotonic() - started_monotonic,
            manifest_input_size_bytes=manifest_input_size_bytes,
            manifest_input_mtime_ns=manifest_input_mtime_ns,
            manifest_relative_path=manifest_relative_path,
        )
        payload = {
            "file_path": input_path,
            "filename": filename,
            "status": file_status,
            "error": file_error,
            **aggregate_execution_metadata(result),
        }
        if file_status != "success":
            payload["failure_category"] = infer_failure_category({"error": file_error})
        runtime = _runtime_snapshot(parser)
        if runtime is not None:
            payload["runtime"] = runtime
        _emit_event(
            parser,
            "file_done" if file_status == "success" else "file_failed",
            **payload,
        )
        DOCUMENTS.labels(status="success" if file_status == "success" else "error").inc()
        return result
    except Exception as exc:
        failure_category = infer_failure_category({"error": str(exc)})
        parser._console_write(f"Critical error parsing file {input_path}: {exc}", level="error")
        await parser._flush_document_page_json(save_dir)
        write_status_sidecar(
            parser=parser,
            save_dir=save_dir,
            input_path=input_path,
            filename=filename,
            status="failed",
            error=str(exc),
            result=[],
            duration_seconds=time.monotonic() - started_monotonic,
            error_type=type(exc).__name__,
            manifest_input_size_bytes=manifest_input_size_bytes,
            manifest_input_mtime_ns=manifest_input_mtime_ns,
            manifest_relative_path=manifest_relative_path,
        )
        payload = {
            "file_path": input_path,
            "filename": filename,
            "status": "failed",
            "error": str(exc),
            "failure_category": failure_category,
            **execution_metadata(None),
        }
        runtime = _runtime_snapshot(parser)
        if runtime is not None:
            payload["runtime"] = runtime
        _emit_event(
            parser,
            "file_failed",
            **payload,
        )
        DOCUMENTS.labels(status="error").inc()
        return [{"error": str(exc), "file_path": input_path}]
