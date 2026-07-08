from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .algorithms import extract_table_screenshot

PAGE_IMAGE_SAVE_QUALITY = 92


def _sanitize_table_reparse_output(self, content: str) -> str:
    if not content:
        return ""
    content = content.strip()
    fence_match = re.match(r"^```(?:html|table)?\s*([\s\S]*?)\s*```$", content, re.IGNORECASE)
    if fence_match:
        content = fence_match.group(1).strip()
    return content


def _compress_table_html(self, content: str) -> str:
    if not content:
        return ""
    content = content.replace("\r", " ").replace("\n", " ")
    content = re.sub(r">\s+<", "><", content)
    content = re.sub(r"\s{2,}", " ", content)
    return content.strip()


def _ensure_table_ocr_pipeline(self):
    if not self.enable_table_reparse:
        return None
    thread_pipeline = getattr(self._table_ocr_thread_local, "pipeline", None)
    if thread_pipeline is not None:
        return thread_pipeline
    if self._table_ocr_pipeline_error is not None:
        return None

    with self._table_ocr_pipeline_lock:
        thread_pipeline = getattr(self._table_ocr_thread_local, "pipeline", None)
        if thread_pipeline is not None:
            return thread_pipeline
        if self._table_ocr_pipeline_error is not None:
            return None
        try:
            if self._table_ocr_cls is None:
                from paddleocr import PaddleOCRVL

                self._table_ocr_cls = PaddleOCRVL
        except ImportError as exc:
            self._console_write(
                "Table reparsing requires 'paddleocr'. Dependency not found; skipping table refinement.",
                level="error",
            )
            self._table_ocr_pipeline_error = exc
            return None
        try:
            if self._table_ocr_init_kwargs is None:
                init_kwargs = {}
                if self.table_ocr_backend:
                    init_kwargs["vl_rec_backend"] = self.table_ocr_backend
                if self.table_ocr_server_url:
                    init_kwargs["vl_rec_server_url"] = self.table_ocr_server_url
                if self.table_ocr_device:
                    init_kwargs["device"] = self.table_ocr_device
                self._table_ocr_init_kwargs = init_kwargs
            pipeline = self._table_ocr_cls(**(self._table_ocr_init_kwargs or {}))
        except Exception as exc:
            self._console_write(
                f"Failed to initialize PaddleOCRVL table OCR pipeline ({self.table_ocr_backend} @ {self.table_ocr_server_url}): {exc}",
                level="error",
            )
            self._table_ocr_pipeline_error = exc
            return None
        setattr(self._table_ocr_thread_local, "pipeline", pipeline)
        if not self._table_ocr_pipeline_ready_logged:
            self._console_write(
                f"PaddleOCRVL table OCR pipeline ready ({self.table_ocr_backend} @ {self.table_ocr_server_url})."
            )
            self._table_ocr_pipeline_ready_logged = True
        return pipeline


async def _predict_table_with_external_model(self, image_path: str):
    if self._ensure_table_ocr_pipeline() is None:
        return None
    loop = asyncio.get_running_loop()
    last_exc = None
    for attempt in range(1, self.table_ocr_max_retries + 1):
        try:
            async with self._table_ocr_semaphore:
                return await loop.run_in_executor(
                    self._table_ocr_executor, functools.partial(self._table_ocr_predict_sync, image_path)
                )
        except Exception as exc:
            last_exc = exc
            self._console_write(
                f"PaddleOCRVL table OCR attempt {attempt}/{self.table_ocr_max_retries} failed: {exc}",
                level="warning",
            )
            if attempt < self.table_ocr_max_retries:
                await asyncio.sleep(self.table_ocr_retry_delay)
    self._console_write(
        f"PaddleOCRVL table OCR failed after {self.table_ocr_max_retries} attempts for {os.path.basename(image_path)}: {last_exc}",
        level="error",
    )
    return None


def _table_ocr_predict_sync(self, image_path: str):
    pipeline = self._ensure_table_ocr_pipeline()
    if pipeline is None:
        raise RuntimeError("PaddleOCRVL pipeline is unavailable for table OCR.")
    return pipeline.predict(image_path)


def _extract_string_from_prediction(self, prediction, candidates: Tuple[str, ...]):
    if prediction is None:
        return None
    if isinstance(prediction, dict):
        for key in candidates:
            value = prediction.get(key)
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8", errors="ignore")
                except Exception:
                    continue
            if isinstance(value, str) and value.strip():
                return value
    for attr in candidates:
        if not hasattr(prediction, attr):
            continue
        value = getattr(prediction, attr)
        try:
            value = value()
        except TypeError:
            pass
        except Exception:
            continue
        if callable(value):
            continue
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="ignore")
            except Exception:
                continue
        if isinstance(value, str) and value.strip():
            return value
    return None


def _fallback_save_to_markdown(self, prediction):
    if prediction is None:
        return None
    save_fn = getattr(prediction, "save_to_markdown", None)
    if not callable(save_fn):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="paddleocr_markdown_") as tmp_dir:
            try:
                result = save_fn(save_path=tmp_dir)
            except TypeError:
                result = save_fn(tmp_dir)
            candidates = []
            if isinstance(result, str) and os.path.exists(result):
                candidates.append(Path(result))
            candidates.extend(Path(tmp_dir).glob("*.md"))
            for candidate in candidates:
                try:
                    return candidate.read_text(encoding="utf-8")
                except Exception:
                    continue
    except Exception as exc:
        self._console_write(f"Failed to collect markdown from PaddleOCRVL save_to_markdown: {exc}", level="warning")
    return None


def _fallback_save_to_json(self, prediction):
    if prediction is None:
        return None
    save_fn = getattr(prediction, "save_to_json", None)
    if not callable(save_fn):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="paddleocr_json_") as tmp_dir:
            try:
                result = save_fn(save_path=tmp_dir)
            except TypeError:
                result = save_fn(tmp_dir)
            candidates = []
            if isinstance(result, str) and os.path.exists(result):
                candidates.append(Path(result))
            candidates.extend(Path(tmp_dir).glob("*.json"))
            for candidate in candidates:
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except Exception:
                    continue
    except Exception as exc:
        self._console_write(f"Failed to collect JSON from PaddleOCRVL save_to_json: {exc}", level="warning")
    return None


def _extract_json_from_prediction(self, prediction):
    if prediction is None:
        return None
    json_candidates = (
        "json",
        "json_result",
        "structured",
        "structure",
        "structured_data",
        "table_structure",
        "dict",
        "to_dict",
        "as_dict",
        "result_dict",
        "data",
    )
    if isinstance(prediction, dict):
        for key in json_candidates:
            if key not in prediction:
                continue
            value = prediction[key]
            if isinstance(value, (dict, list)):
                return value
            if isinstance(value, str):
                with contextlib.suppress(Exception):
                    return json.loads(value)
    for attr in json_candidates:
        if not hasattr(prediction, attr):
            continue
        value = getattr(prediction, attr)
        try:
            value = value()
        except TypeError:
            pass
        except Exception:
            continue
        if callable(value):
            continue
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            with contextlib.suppress(Exception):
                return json.loads(value)
    return self._fallback_save_to_json(prediction)


def _extract_table_reparse_payload(self, prediction):
    markdown = self._extract_string_from_prediction(prediction, ("markdown", "markdown_str", "markdown_text", "md"))
    html = self._extract_string_from_prediction(prediction, ("html", "html_str", "html_text"))
    if not markdown and html:
        markdown = html
    if not html and markdown and self._is_probable_html(markdown):
        html = markdown
    if not markdown:
        markdown = self._fallback_save_to_markdown(prediction)
    structured = self._extract_json_from_prediction(prediction)
    return markdown, html, structured


def _is_probable_html(self, text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"<\s*(table|tr|td|th)\b", text.strip().lower()))


async def _maybe_refine_table_blocks(
    self,
    *,
    page_number: int,
    cells: List[dict],
    origin_image,
    original_cells: Optional[List[dict]] = None,
    filename: Optional[str] = None,
    prompt_mode: Optional[str] = None,
    save_dir: Optional[str] = None,
) -> bool:
    if not self.enable_table_reparse or self._table_reparse_stack > 0:
        return False
    if origin_image is None or not cells or not prompt_mode:
        return False

    table_indices = [idx for idx, cell in enumerate(cells) if isinstance(cell, dict) and cell.get("category") == "Table"]
    if not table_indices:
        return False

    original_lookup: Dict[Tuple[int, int, int, int], dict] = {}
    if original_cells:
        for original_cell in original_cells:
            if not isinstance(original_cell, dict) or original_cell.get("category") != "Table":
                continue
            bbox_val = original_cell.get("bbox")
            if not isinstance(bbox_val, (list, tuple)) or len(bbox_val) != 4:
                continue
            bbox_key = tuple(int(round(float(coord))) for coord in bbox_val)
            original_lookup.setdefault(bbox_key, original_cell)

    refined_any = False
    file_tag = None
    if filename:
        with contextlib.suppress(Exception):
            file_tag = Path(filename).stem or Path(filename).name

    if self._ensure_table_ocr_pipeline() is None:
        self._console_write(
            "Table reparse is enabled but PaddleOCRVL pipeline could not be initialized. Skipping table refinement.",
            level="warning",
        )
        return False

    for local_idx, cell_idx in enumerate(table_indices):
        table_cell = cells[cell_idx]
        table_image_raw, _ = extract_table_screenshot(origin_image, table_cell, page_number, local_idx)
        if table_image_raw is None:
            continue

        try:
            table_origin_image = table_image_raw.convert("RGB")
        except Exception as convert_exc:
            self._console_write(
                f"Failed to convert table image to RGB for page {page_number} table #{local_idx + 1}: {convert_exc}",
                level="warning",
            )
            with contextlib.suppress(Exception):
                table_image_raw.close()
            continue
        finally:
            with contextlib.suppress(Exception):
                table_image_raw.close()

        table_filename = file_tag or f"page_{page_number}"
        table_filename = f"{table_filename}_table_{local_idx + 1}"
        temp_image_path = None
        try:
            temp_file = tempfile.NamedTemporaryFile(
                prefix=f"{table_filename}_reparse_", suffix=".jpg", dir=save_dir or None, delete=False
            )
            temp_image_path = temp_file.name
            temp_file.close()
            table_origin_image.save(temp_image_path, "JPEG", quality=PAGE_IMAGE_SAVE_QUALITY)
        except Exception as save_exc:
            self._console_write(
                f"Failed to save temporary table image for page {page_number} table #{local_idx + 1}: {save_exc}",
                level="warning",
            )
            with contextlib.suppress(Exception):
                table_origin_image.close()
            if temp_image_path:
                with contextlib.suppress(Exception):
                    os.remove(temp_image_path)
            continue
        finally:
            with contextlib.suppress(Exception):
                table_origin_image.close()

        try:
            predictions = await self._predict_table_with_external_model(temp_image_path)
        finally:
            if temp_image_path:
                with contextlib.suppress(Exception):
                    os.remove(temp_image_path)

        if not predictions:
            self._console_write(
                f"PaddleOCRVL returned empty predictions for page {page_number} table #{local_idx + 1}.",
                level="warning",
            )
            continue

        markdown_parts = []
        html_parts = []
        structured_payload = None
        prediction_list = predictions if isinstance(predictions, (list, tuple)) else [predictions]
        for prediction in prediction_list:
            md_text, html_text, structured = self._extract_table_reparse_payload(prediction)
            if md_text:
                markdown_parts.append(md_text.strip())
            if html_text:
                html_parts.append(html_text.strip())
            if structured is not None and structured_payload is None:
                structured_payload = structured

        combined_markdown = "\n\n".join(part for part in markdown_parts if part)
        combined_html = "\n".join(part for part in html_parts if part)
        sanitized_markdown = self._sanitize_table_reparse_output(combined_markdown) if combined_markdown else ""
        sanitized_html = self._sanitize_table_reparse_output(combined_html) if combined_html else ""
        if sanitized_html and self._is_probable_html(sanitized_html):
            sanitized_html = self._compress_table_html(sanitized_html)

        final_text = sanitized_markdown or sanitized_html
        if not final_text:
            continue

        final_html = sanitized_html or (sanitized_markdown if sanitized_markdown else final_text)
        table_cell["text"] = final_text
        table_cell["html"] = final_html
        if structured_payload is not None:
            table_cell["table_json"] = structured_payload

        bbox_key = tuple(int(round(float(coord))) for coord in (table_cell.get("bbox") or [])[:4])
        original_match = original_lookup.get(bbox_key)
        if original_match:
            original_match["text"] = final_text
            original_match["html"] = final_html
            if structured_payload is not None:
                original_match["table_json"] = structured_payload

        refined_any = True
        table_label = table_cell.get("caption_text") or f"table #{local_idx + 1}"
        context_label = file_tag or "current file"
        self._console_write(
            f"Table reparse updated page {page_number} {table_label} ({context_label}).",
            level="info",
        )

    return refined_any
