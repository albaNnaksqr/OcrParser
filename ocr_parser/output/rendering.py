from __future__ import annotations

import asyncio
import contextlib
import copy
import math
import os
import re
import time
import traceback
from typing import Dict, List, Optional, Set

import aiofiles
import imagehash
from PIL import Image

from dots_ocr.utils.format_transformer_v3 import (
    layoutjson2md_full_robust,
    normalize_superscript_citations,
    unescape_basic_sequences,
)


def is_author_block(text: str, debug: bool = False, log_fn=None) -> bool:
    if not text or len(text) < 20:
        return False

    text_for_analysis = re.sub(r"<[^>]+>", "", text)
    stop_words = {
        "a",
        "an",
        "the",
        "in",
        "of",
        "for",
        "and",
        "is",
        "are",
        "was",
        "were",
        "on",
        "at",
        "to",
        "it",
        "as",
        "by",
        "with",
        "from",
    }
    word_list = re.split(r"[\s;(),]+", text_for_analysis.lower())
    if word_list:
        stop_word_count = sum(1 for word in word_list if word in stop_words)
        if stop_word_count / len(word_list) > 0.1:
            return False

    score = 0
    text_len = len(text)
    if re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", text):
        score += 3

    superscripts_unicode = re.findall(r"[¹²³⁴⁵⁶⁷⁸⁹⁰*†‡§′]", text)
    superscripts_html = re.findall(r"<sup>", text)
    total_superscripts = len(superscripts_unicode) + len(superscripts_html)
    if total_superscripts >= 5:
        score += 4
    elif total_superscripts >= 2:
        score += 3

    institution_keywords = [
        "医院",
        "大学",
        "学院",
        "研究所",
        "中心",
        "附属",
        "人民医院",
        "医科大学",
        "研究院",
        "邮编",
        "通信作者",
        "重点实验室",
        "University",
        "Institute",
        "College",
        "Hospital",
        "Center",
        "Department",
        "School",
        "Laboratory",
        "Inc.",
        "Ltd.",
        "Correspondence",
        "email",
        "e-mail",
        "Oncology",
        "Medicine",
        "Received",
        "Accepted",
    ]
    keyword_count = 0
    cleaned_lower_text = text_for_analysis.lower()
    for kw in institution_keywords:
        if kw.isascii():
            keyword_count += cleaned_lower_text.count(kw.lower())
        else:
            keyword_count += text_for_analysis.count(kw)
    if keyword_count >= 3:
        score += 3
    elif keyword_count >= 1:
        score += 1

    if re.search(r"\b\d{5,}\b", text_for_analysis):
        score += 2
    if text_len and text.count(",") / text_len > 0.03:
        score += 1

    initial_text = text_for_analysis[:200]
    potential_words = re.split(r"[\s;()]+", initial_text)
    name_like_word_count = 0
    en_keywords_lower = [k.lower() for k in institution_keywords if k.isascii()]
    for word in potential_words:
        word = word.strip(",").strip()
        if not word or len(word) <= 1:
            continue
        is_name_like = False
        if 2 <= len(word) <= 4 and re.search(r"[\u4e00-\u9fa5]", word):
            is_name_like = True
        elif (word.istitle() or re.match(r"^[A-Z][a-z-]+", word)) and not word.isupper() and word.lower() not in en_keywords_lower:
            is_name_like = True
        elif word.isupper() and len(word) < 15 and re.match(r"^[A-Z][A-Z-]*[A-Z]$", word) and word.lower() not in en_keywords_lower:
            is_name_like = True
        if is_name_like:
            name_like_word_count += 1

    if name_like_word_count > 5:
        score += 3
    elif name_like_word_count > 2:
        score += 2

    if len(re.findall(r"\b(MD|PhD|PHD|MPH|MLIS|MBA)\b", text)) >= 2:
        score += 2

    if debug and score >= 1 and callable(log_fn):
        log_fn(f"[DEBUG is_author_block] Score: {score} | Text: {text[:80]}...", level="always")
    return score >= 5


def _find_duplicate_filenames(name_hash_map, group_threshold: int, similarity_pct: float):
    if len(name_hash_map) < group_threshold:
        return set()
    sample_hash = next(iter(name_hash_map.values()))
    hash_size = len(str(sample_hash)) * 4
    dist_threshold = int(hash_size * (1 - similarity_pct / 100.0))
    filenames = list(name_hash_map.keys())
    visited = set()
    files_to_delete = set()
    for i in range(len(filenames)):
        first = filenames[i]
        if first in visited:
            continue
        current_group = {first}
        visited.add(first)
        hash_first = name_hash_map[first]
        for j in range(i + 1, len(filenames)):
            second = filenames[j]
            if second in visited:
                continue
            if (hash_first - name_hash_map[second]) <= dist_threshold:
                current_group.add(second)
                visited.add(second)
        if len(current_group) >= group_threshold:
            files_to_delete.update(current_group)
    return files_to_delete


def _clean_markdown_content(md_content: str, files_to_remove):
    if not files_to_remove:
        return md_content
    pattern_str = r"!\[[^\]]*\]\s*\(\s*images/(?:" + "|".join(re.escape(fn) for fn in files_to_remove) + r")\s*\)\n?"
    cleaned_content = re.sub(pattern_str, "", md_content)
    cleaned_content = re.sub(r"(\n\s*){3,}", "\n\n", cleaned_content).strip()
    return cleaned_content


async def _generate_md_for_one_page(self, page_idx, all_pages_layout_data, images_dir, time_warn_s: float = 5.0):
    loop = asyncio.get_running_loop()
    result = all_pages_layout_data[page_idx]
    page_num = page_idx + 1
    status = result.get("status")
    t0 = time.time()

    async with self.md_gen_semaphore:
        try:
            origin_md_content = ""
            cells_to_render = []
            origin_cells_to_render = []
            if status == "success":
                origin_path = result.get("origin_image_path")
                filtered_cells_source = result.get("cells", []) or []
                original_cells_source = result.get("original_cells")
                if original_cells_source is None:
                    original_cells_source = filtered_cells_source

                def _prepare_cells_for_md(source_cells, *, apply_superscript: bool) -> List[dict]:
                    if not source_cells:
                        return []
                    working = copy.deepcopy(source_cells)
                    working = self._merge_adjacent_text_blocks_in_same_page(working)
                    pre_filter_count = len(working)
                    working = [
                        cell
                        for cell in working
                        if (self.keep_page_header or cell.get("category") != "Page-header")
                        and (self.keep_page_footer or cell.get("category") != "Page-footer")
                        and (not self.skip_footnote or cell.get("category") != "Footnote")
                    ]
                    if working and len(working) < pre_filter_count:
                        working = self._merge_adjacent_text_blocks_in_same_page(working)
                    if apply_superscript and working:
                        working = normalize_superscript_citations(working)
                    return working

                cells_to_render = _prepare_cells_for_md(filtered_cells_source, apply_superscript=self.normalize_superscript)
                if self.filter_author_blocks and page_num == 1:
                    cells_to_render = [
                        box
                        for box in cells_to_render
                        if box.get("category") not in {"Text", "Section-header"}
                        or not is_author_block(box.get("text", ""), debug=getattr(self, "author_block_debug", False), log_fn=self._console_write)
                    ]

                if self.generate_origin_md:
                    origin_cells_to_render = _prepare_cells_for_md(original_cells_source, apply_superscript=False)

                def _mk_md_sync():
                    md_processed = ""
                    md_origin_processed = ""
                    if not origin_path or not os.path.exists(origin_path):
                        return md_processed, md_origin_processed
                    with Image.open(origin_path) as image:
                        orig = image.convert("RGB")
                        if cells_to_render:
                            md_processed = layoutjson2md_full_robust(orig, cells_to_render, "text", False)
                        if self.generate_origin_md and origin_cells_to_render:
                            md_origin_processed = layoutjson2md_full_robust(orig, origin_cells_to_render, "text", False)
                    return md_processed, md_origin_processed

                md_content, origin_md_content = await loop.run_in_executor(None, _mk_md_sync)
            elif status in ("success_fallback_text", "success_fallback_image"):
                md_content = result.get("md_content", "")
                if self.generate_origin_md:
                    origin_md_content = md_content
            elif status != "skipped_blank":
                md_content = f"\n\n[Page {page_num} could not be rendered or processed]\n\n"
                if self.generate_origin_md:
                    origin_md_content = md_content
            else:
                md_content = ""

            if md_content or (self.generate_origin_md and origin_md_content):
                final_md_text = ""
                final_origin_md_text = ""
                image_b64_to_filename = {}

                if md_content:
                    md_content_unescaped = unescape_basic_sequences(md_content, convert_controls=False)
                    origin_path = result.get("origin_image_path")
                    filtered_cells_for_images = cells_to_render or (result.get("cells", []) or [])

                    def _img_proc_sync():
                        origin_image = None
                        try:
                            if origin_path and os.path.exists(origin_path):
                                with Image.open(origin_path) as image:
                                    origin_image = image.convert("RGB").copy()
                            return self._process_base64_images_with_custom_naming(
                                md_content_unescaped,
                                images_dir,
                                page_num,
                                filtered_cells_for_images,
                                origin_image,
                                image_b64_to_filename=image_b64_to_filename,
                            )
                        finally:
                            origin_image = None

                    processed = await loop.run_in_executor(None, _img_proc_sync)
                    if processed and processed.get("md_content") is not None:
                        final_md_text = unescape_basic_sequences(processed["md_content"], convert_controls=False)
                    else:
                        final_md_text = md_content_unescaped

                if self.generate_origin_md:
                    origin_md_unescaped = unescape_basic_sequences(origin_md_content or "", convert_controls=False)
                    if origin_md_unescaped:
                        origin_path = result.get("origin_image_path")
                        origin_cells_for_images = origin_cells_to_render or result.get("original_cells")
                        if origin_cells_for_images is None:
                            origin_cells_for_images = result.get("cells", []) or []

                        def _img_proc_origin_sync():
                            origin_image = None
                            try:
                                if origin_path and os.path.exists(origin_path):
                                    with Image.open(origin_path) as image:
                                        origin_image = image.convert("RGB").copy()
                                return self._process_base64_images_with_custom_naming(
                                    origin_md_unescaped,
                                    images_dir,
                                    page_num,
                                    origin_cells_for_images,
                                    origin_image,
                                    image_b64_to_filename=image_b64_to_filename,
                                )
                            finally:
                                origin_image = None

                        processed_origin = await loop.run_in_executor(None, _img_proc_origin_sync)
                        if processed_origin and processed_origin.get("md_content") is not None:
                            final_origin_md_text = unescape_basic_sequences(
                                processed_origin["md_content"], convert_controls=False
                            )
                        else:
                            final_origin_md_text = origin_md_unescaped

                page_md_obj = {"page_num": page_num, "md_content": final_md_text}
                if self.generate_origin_md:
                    page_md_obj["origin_md_content"] = final_origin_md_text
                return page_md_obj

            return None
        finally:
            elapsed = time.time() - t0
            if elapsed > time_warn_s:
                self._console_write(f"[MD] Page {page_num} MD-gen took {elapsed:.1f}s (windowed).", level="warning")


async def _filter_duplicate_images(
    self,
    md_content: str,
    images_dir: str,
    group_threshold: int = 3,
    similarity_threshold_pct: float = 75.0,
    delete_files: bool = True,
) -> str:
    def _sync_filter_logic():
        if not os.path.isdir(images_dir):
            return md_content
        try:
            ref_pattern = re.compile(r"!\[[^\]]*\]\s*\(\s*images/([^)]+)\s*\)")
            referenced_filenames = set(ref_pattern.findall(md_content))
            if len(referenced_filenames) < group_threshold:
                return md_content

            name_hash_map = {}
            for filename in referenced_filenames:
                path = os.path.join(images_dir, filename)
                if not os.path.exists(path):
                    continue
                try:
                    with Image.open(path) as image:
                        name_hash_map[filename] = imagehash.phash(image)
                except Exception as exc:
                    self._console_write(f"Warning: Could not calculate hash for image {filename}: {exc}", level="warning")

            if len(name_hash_map) < group_threshold:
                return md_content

            files_to_delete = _find_duplicate_filenames(name_hash_map, group_threshold, similarity_threshold_pct)
            if not files_to_delete:
                self._console_write("No significant near-duplicate image groups found.")
                return md_content

            cleaned_md_content = _clean_markdown_content(md_content, files_to_delete)
            if delete_files:
                for filename in files_to_delete:
                    path = os.path.join(images_dir, filename)
                    try:
                        if os.path.exists(path):
                            os.remove(path)
                    except OSError as exc:
                        self._console_write(f"Error removing duplicate image file {path}: {exc}", level="error")
            return cleaned_md_content
        except Exception as exc:
            self._console_write(f"An unexpected error occurred during duplicate image filtering: {exc}", level="error")
            self._console_write(traceback.format_exc(), level="error")
            return md_content

    return await asyncio.get_running_loop().run_in_executor(None, _sync_filter_logic)


async def _cleanup_unused_images(self, markdown_paths: List[str], images_dir: str) -> None:
    if not os.path.isdir(images_dir):
        return
    referenced: Set[str] = set()
    pattern = re.compile(r"!\[[^\]]*\]\s*\(\s*images/([^)]+)\s*\)")
    for path in markdown_paths:
        if not path or not os.path.exists(path):
            continue
        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as handle:
                content = await handle.read()
            referenced.update(pattern.findall(content))
        except Exception as exc:
            self._console_write(f"Warning: Failed to read markdown file '{path}' during image cleanup: {exc}", level="warning")
    try:
        for filename in os.listdir(images_dir):
            if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if filename not in referenced:
                try:
                    os.remove(os.path.join(images_dir, filename))
                except OSError as exc:
                    self._console_write(f"Warning: Could not remove unused image '{filename}': {exc}", level="warning")
    except Exception as exc:
        self._console_write(f"Warning: Failed to enumerate images for cleanup in '{images_dir}': {exc}", level="warning")


async def _combine_layout_images_to_pdf(self, image_paths: List[str], output_pdf_path: str) -> Optional[str]:
    if not image_paths:
        return None

    def _sync_combine():
        import fitz  # PyMuPDF

        doc = fitz.open()
        try:
            for path in image_paths:
                if not os.path.exists(path):
                    continue
                img_doc = fitz.open(path)
                pdf_bytes = img_doc.convert_to_pdf()
                img_doc.close()
                page_pdf = fitz.open("pdf", pdf_bytes)
                doc.insert_pdf(page_pdf)
                page_pdf.close()
            if not doc.page_count:
                return None
            doc.save(output_pdf_path)
            return output_pdf_path
        finally:
            doc.close()

    return await asyncio.get_running_loop().run_in_executor(None, _sync_combine)


def _get_unique_md_path(self, save_dir: str, base_filename: str) -> str:
    if base_filename.lower().endswith(".md"):
        base_filename = base_filename[:-3] or "output_md"
    md_path = os.path.join(save_dir, f"{base_filename}.md")
    if not os.path.exists(md_path):
        return md_path
    counter = 2
    while True:
        new_md_path = os.path.join(save_dir, f"{base_filename}_{counter}.md")
        if not os.path.exists(new_md_path):
            self._console_write(
                f"Warning: Output file for '{base_filename}' already exists. Saving as '{os.path.basename(new_md_path)}' instead.",
                level="warning",
            )
            return new_md_path
        counter += 1


async def _compute_md_word_stats(
    self,
    md_path: str,
    *,
    sampled_pages: int,
    total_pdf_pages: int,
    page_limit: int = 0,
) -> Dict[str, int]:
    text = ""
    try:
        async with aiofiles.open(md_path, "r", encoding="utf-8") as handle:
            text = await handle.read()
    except Exception as exc:
        self._console_write(f"WARNING: Failed to read markdown for word stats ({md_path}): {exc}", level="warning")

    actual_words = self._count_words_from_text(text)
    sampled = max(sampled_pages, 0)
    total = max(total_pdf_pages, sampled)
    estimated = actual_words
    if sampled > 0 and total > sampled and actual_words > 0:
        try:
            estimated = math.ceil(actual_words * (total / sampled))
        except ZeroDivisionError:
            estimated = actual_words

    stats = {
        "word_count_actual": actual_words,
        "word_count_estimated": estimated,
        "sampled_pages": sampled,
        "total_pdf_pages": total,
    }
    if page_limit and page_limit > 0:
        stats["page_limit_applied"] = min(page_limit, total if total > 0 else page_limit)
    return stats
