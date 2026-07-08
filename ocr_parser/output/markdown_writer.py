from __future__ import annotations

import asyncio
import contextlib
import os
import re
from typing import Any, Dict, List

import aiofiles

from dots_ocr.utils.format_transformer_v3 import unescape_basic_sequences

from ..models import MarkdownArtifacts


async def write_document_outputs(
    parser: Any,
    *,
    filename: str,
    save_dir: str,
    all_pages_layout_data: List[Dict[str, Any]],
    total_pages_expected: int,
) -> MarkdownArtifacts:
    images_dir = os.path.join(save_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    combined_md_path = parser._get_unique_md_path(save_dir, filename)
    origin_md_path = None
    layout_pdf_path = None

    window = max(8, min(parser.md_gen_concurrency // 2 if parser.md_gen_concurrency > 0 else 16, 64))
    next_to_schedule = 0
    next_to_write = 0
    pending = {}

    async def _stream_pages(md_handle, origin_handle=None):
        nonlocal next_to_schedule, next_to_write, pending

        while next_to_schedule < total_pages_expected and len(pending) < window:
            pending[next_to_schedule] = asyncio.create_task(
                parser._generate_md_for_one_page(
                    next_to_schedule, all_pages_layout_data, images_dir
                )
            )
            next_to_schedule += 1

        while next_to_write < total_pages_expected:
            if next_to_write not in pending:
                while next_to_schedule < total_pages_expected and len(pending) < window:
                    pending[next_to_schedule] = asyncio.create_task(
                        parser._generate_md_for_one_page(
                            next_to_schedule, all_pages_layout_data, images_dir
                        )
                    )
                    next_to_schedule += 1

            res = await pending.pop(next_to_write)
            page_text = (res or {}).get("md_content", "")
            origin_text = (res or {}).get("origin_md_content", "") if origin_handle is not None else ""

            if page_text:
                page_text = unescape_basic_sequences(page_text, convert_controls=False)
                if parser.add_page_tag:
                    await md_handle.write(f"{page_text}\n<special_page_num_tag>{next_to_write + 1}</special_page_num_tag>\n")
                else:
                    await md_handle.write(page_text + "\n\n")

            if origin_handle is not None and origin_text:
                origin_text = unescape_basic_sequences(origin_text, convert_controls=False)
                if parser.add_page_tag:
                    await origin_handle.write(
                        f"{origin_text}\n<special_page_num_tag>{next_to_write + 1}</special_page_num_tag>\n"
                    )
                else:
                    await origin_handle.write(origin_text + "\n\n")

            if next_to_write < len(all_pages_layout_data):
                entry = all_pages_layout_data[next_to_write]
                if "cells" in entry:
                    entry["cells"] = None
                if "original_cells" in entry:
                    entry["original_cells"] = None

            next_to_write += 1

    if parser.generate_origin_md:
        origin_md_path = parser._get_unique_md_path(save_dir, f"{filename}_origin")
        async with aiofiles.open(combined_md_path, "w", encoding="utf-8") as md_file, aiofiles.open(
            origin_md_path, "w", encoding="utf-8"
        ) as origin_md_file:
            await _stream_pages(md_file, origin_md_file)
    else:
        async with aiofiles.open(combined_md_path, "w", encoding="utf-8") as md_file:
            await _stream_pages(md_file)

    if parser.filter_duplicates:
        try:
            async with aiofiles.open(combined_md_path, "r", encoding="utf-8") as handle:
                md_text = await handle.read()
            md_text = re.sub(r"(\n\s*){3,}", "\n\n", md_text).strip()
            # Keep the original text so we can identify the same duplicate images
            # for deletion after the new file is safely on disk — _filter_duplicate_images
            # finds images to delete by scanning the text for references, so it must
            # receive the pre-cleaned text that still contains the duplicate references.
            md_text_for_deletion = md_text
            md_text = await parser._filter_duplicate_images(
                md_text, images_dir, delete_files=False
            )
            md_text = unescape_basic_sequences(md_text, convert_controls=False)
            # Atomic write: truncate happens only after rename succeeds, so a
            # write failure can never leave an empty file alongside deleted images.
            tmp_combined = combined_md_path + ".tmp"
            async with aiofiles.open(tmp_combined, "w", encoding="utf-8") as handle:
                await handle.write(md_text)
            os.replace(tmp_combined, combined_md_path)
            # Images are only deleted after the file is safely written.
            # Use the original (pre-clean) text so _filter_duplicate_images can
            # still find the duplicate references and compute files_to_delete.
            if not parser.generate_origin_md:
                await parser._filter_duplicate_images(md_text_for_deletion, images_dir, delete_files=True)

            if parser.generate_origin_md and origin_md_path and os.path.exists(origin_md_path):
                async with aiofiles.open(origin_md_path, "r", encoding="utf-8") as handle:
                    origin_md_text = await handle.read()
                origin_md_text = re.sub(r"(\n\s*){3,}", "\n\n", origin_md_text).strip()
                origin_md_text = await parser._filter_duplicate_images(
                    origin_md_text, images_dir, delete_files=False
                )
                origin_md_text = unescape_basic_sequences(origin_md_text, convert_controls=False)
                tmp_origin = origin_md_path + ".tmp"
                async with aiofiles.open(tmp_origin, "w", encoding="utf-8") as handle:
                    await handle.write(origin_md_text)
                os.replace(tmp_origin, origin_md_path)

            if parser.generate_origin_md and origin_md_path:
                await parser._cleanup_unused_images([combined_md_path, origin_md_path], images_dir)
        except Exception as exc:
            parser._console_write(
                f"WARNING: duplicate image filtering failed for '{filename}': {exc}",
                level="warning",
            )

    if parser.save_page_layout:
        layout_candidates = []
        for entry in all_pages_layout_data:
            layout_path = entry.get("page_layout_path")
            if layout_path and os.path.exists(layout_path):
                layout_candidates.append((entry.get("original_page_num", 0), layout_path))
        if layout_candidates:
            layout_candidates.sort(key=lambda item: item[0])
            layout_pdf_path = os.path.join(save_dir, f"{filename}_layout.pdf")
            combined = await parser._combine_layout_images_to_pdf(
                [path for _, path in layout_candidates], layout_pdf_path
            )
            if combined:
                parser._console_write(f"[{filename}] Layout PDF saved to {layout_pdf_path}")
                for _, path in layout_candidates:
                    with contextlib.suppress(Exception):
                        os.remove(path)
            else:
                layout_pdf_path = None

    document_json_path = await parser._flush_document_page_json(save_dir)
    parser._console_write(f"[{filename}] Successfully saved final output to {combined_md_path}")
    if parser.generate_origin_md and origin_md_path:
        parser._console_write(f"[{filename}] Origin Markdown saved to {origin_md_path}")
    if document_json_path:
        parser._console_write(f"[{filename}] Aggregated JSON saved to {document_json_path}")

    return MarkdownArtifacts(
        combined_md_path=combined_md_path,
        origin_md_path=origin_md_path,
        layout_pdf_path=layout_pdf_path,
        document_json_path=document_json_path,
    )
