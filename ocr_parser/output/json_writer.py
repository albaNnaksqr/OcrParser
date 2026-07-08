from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
from typing import Optional

import aiofiles

from dots_ocr.utils.layout_utils import draw_layout_on_image


def _get_document_json_output_path(self, save_dir: str) -> str:
    document_name = Path(save_dir).name or "document"
    return os.path.join(save_dir, f"{document_name}_pages.json")


def _register_page_json_payload(
    self,
    save_dir: str,
    page_number: Optional[int],
    page_identifier: str,
    json_data,
    page_width: Optional[int] = None,
    page_height: Optional[int] = None,
) -> str:
    record = self._document_json_registry.setdefault(
        save_dir,
        {"document": Path(save_dir).name or "document", "output_path": self._get_document_json_output_path(save_dir), "pages": []},
    )
    page_record = {"page_number": page_number, "page_identifier": page_identifier, "data": json_data}
    if page_width and page_height:
        page_record["page_width"] = int(page_width)
        page_record["page_height"] = int(page_height)
    record["pages"].append(page_record)
    return record["output_path"]


async def _flush_document_page_json(self, save_dir: str) -> Optional[str]:
    if not self.save_page_json:
        self._document_json_registry.pop(save_dir, None)
        return None
    record = self._document_json_registry.pop(save_dir, None)
    if not record or not record.get("pages"):
        return None
    pages_sorted = sorted(
        record["pages"], key=lambda item: (item.get("page_number") if item.get("page_number") is not None else math.inf)
    )
    payload = {"document": record.get("document"), "total_pages": len(pages_sorted), "pages": pages_sorted}
    output_path = record.get("output_path") or self._get_document_json_output_path(save_dir)
    async with aiofiles.open(output_path, "w", encoding="utf-8") as handle:
        await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
    return output_path


async def _save_intermediate_outputs_async(
    self,
    save_dir,
    save_name_base,
    response,
    cells,
    original_cells,
    origin_image,
    page_number=None,
    processed_size=None,
):
    if not self.save_page_json and not self.save_page_layout:
        return None, None

    loop = asyncio.get_running_loop()
    json_path, layout_path = None, None

    json_payload_args = None

    def _save_sync():
        nonlocal layout_path, json_payload_args
        page_width = page_height = None
        if origin_image is not None:
            try:
                page_width, page_height = origin_image.size
            except Exception:
                page_width = page_height = None
        if self.save_page_json:
            if isinstance(response, str):
                try:
                    json_data_to_save = json.loads(response)
                except json.JSONDecodeError:
                    json_data_to_save = {"raw_response": response}
            else:
                json_data_to_save = response
            json_payload_args = (save_dir, page_number, save_name_base, json_data_to_save, page_width, page_height)

        if self.save_page_layout and origin_image and (cells or original_cells):
            layout_path = os.path.join(save_dir, f"{save_name_base}_layout.jpg")
            try:
                cells_for_layout = original_cells if original_cells else cells
                resized_w = resized_h = None
                if processed_size and isinstance(processed_size, (list, tuple)) and len(processed_size) == 2:
                    resized_w, resized_h = processed_size
                layout_image = draw_layout_on_image(
                    origin_image, cells_for_layout, resized_height=resized_h, resized_width=resized_w
                )
                if layout_image.mode != "RGB":
                    layout_image = layout_image.convert("RGB")
                layout_image.save(layout_path, "JPEG", quality=95)
            except Exception as exc:
                self._console_write(
                    f"Warning: Failed to draw layout on image for {save_name_base}. Saving original image instead. Error: {exc}",
                    level="warning",
                )
                try:
                    fallback_image = origin_image.convert("RGB")
                    fallback_image.save(layout_path, "JPEG", quality=95)
                except Exception as fallback_exc:
                    self._console_write(
                        f"CRITICAL: Could not save fallback original image for {save_name_base}. Error: {fallback_exc}",
                        level="error",
                    )
                    layout_path = None

    await loop.run_in_executor(None, _save_sync)

    # register on the event loop (single-threaded) to avoid concurrent dict mutation
    json_path = None
    if json_payload_args is not None:
        sd, pn, snb, jd, pw, ph = json_payload_args
        json_path = self._register_page_json_payload(sd, pn, snb, jd, page_width=pw, page_height=ph)

    return json_path, layout_path
