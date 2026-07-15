"""Two-stage PaddleOCR-VL engine.

Stage 1  POST http://<layout_url>/detect  (PP-DocLayoutV2)
         Input : base64 image
         Output: [{bbox:[x1,y1,x2,y2], label, score, index}]

Stage 2  OpenAI-compatible VLM on <vlm ip:port>  (PaddleOCR-VL-1.5)
         Input : cropped block image + type-specific prompt
         Output: text / OTSL table / LaTeX formula
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import Image

from ocr_parser.output.native_writer import async_write_native_json, async_write_native_text

from .api import create_chat_completion, run_in_encode_lane
from .base import EngineCapabilities, EnginePageResult
from .otsl2html import convert_otsl_to_html
from .two_stage import LayoutBlock, TwoStageMetrics, recognize_layout_blocks, record_two_stage_metrics

# ── label sets ──────────────────────────────────────────────────────────────

_DISCARD_LABELS = {
    "header", "footer", "header_image", "footer_image",
    "number",           # page number
    "seal",
    "formula_number",   # equation numbering label only
}

_TITLE_LABELS = {"doc_title", "paragraph_title"}

_TEXT_LABELS = {
    "text", "abstract", "vertical_text", "algorithm",
    "reference", "reference_content", "content",
    "figure_title", "vision_footnote", "footnote",
}

_TABLE_LABELS = {"table"}

_FORMULA_LABELS = {"display_formula", "inline_formula"}

# image / chart blocks: saved as crops, not sent to VLM
_IMAGE_LABELS = {"image", "chart"}

# ── OTSL detection ────────────────────────────────────────────────────────────

_OTSL_MARKERS = {"<fcel>", "<ecel>", "<lcel>", "<ucel>", "<xcel>", "<nl>"}


def _looks_like_otsl(text: str) -> bool:
    return any(m in text for m in _OTSL_MARKERS)


# ── Formula delimiter normalization ──────────────────────────────────────────
# PaddleOCR-VL model may return self-contained delimiters like \[...\] or $...$.
# Strip them before re-wrapping so we don't produce invalid $\[...\]$.

_FORMULA_STRIP_RE = re.compile(
    r'^\s*(?:'
    r'\\\[(?P<display_sq>[\s\S]*?)\\\]'
    r'|\\\((?P<inline_paren>[\s\S]*?)\\\)'
    r'|\$\$(?P<display_dollar>[\s\S]*?)\$\$'
    r'|\$(?P<inline_dollar>[^$]*?)\$'
    r')\s*$'
)


def _strip_formula_delimiters(content: str) -> str:
    m = _FORMULA_STRIP_RE.match(content)
    if not m:
        return content
    for group in ("display_sq", "inline_paren", "display_dollar", "inline_dollar"):
        val = m.group(group)
        if val is not None:
            return val.strip()
    return content


# ── Table-merge helpers (ported from PaddleX v3.5.1 layout_parsing/merge_table.py) ──
# Upstream note: "adapted from MinerU" per upstream acknowledgement.

def _full_to_half(text: str) -> str:
    result = []
    for char in text:
        code = ord(char)
        result.append(chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else char)
    return "".join(result)


def _table_total_columns(soup) -> int:
    rows = soup.find_all("tr")
    if not rows:
        return 0
    max_cols = 0
    occupied: Dict[int, Dict[int, bool]] = {}
    for row_idx, row in enumerate(rows):
        col_idx = 0
        occupied.setdefault(row_idx, {})
        for cell in row.find_all(["td", "th"]):
            while col_idx in occupied[row_idx]:
                col_idx += 1
            colspan = int(cell.get("colspan", 1))
            rowspan = int(cell.get("rowspan", 1))
            for r in range(row_idx, row_idx + rowspan):
                occupied.setdefault(r, {})
                for c in range(col_idx, col_idx + colspan):
                    occupied[r][c] = True
            col_idx += colspan
            max_cols = max(max_cols, col_idx)
    return max_cols


def _row_columns(row) -> int:
    return sum(int(c.get("colspan", 1)) for c in row.find_all(["td", "th"]))


def _visual_columns(row) -> int:
    return len(row.find_all(["td", "th"]))


def _detect_table_headers(soup1, soup2, max_rows: int = 5) -> Tuple[int, bool]:
    rows1 = soup1.find_all("tr")
    rows2 = soup2.find_all("tr")
    min_rows = min(len(rows1), len(rows2), max_rows)
    header_rows = 0
    headers_match = True
    for i in range(min_rows):
        cells1 = rows1[i].find_all(["td", "th"])
        cells2 = rows2[i].find_all(["td", "th"])
        if len(cells1) != len(cells2):
            headers_match = header_rows > 0
            break
        match = all(
            "".join(_full_to_half(c1.get_text()).split()) == "".join(_full_to_half(c2.get_text()).split())
            and int(c1.get("colspan", 1)) == int(c2.get("colspan", 1))
            for c1, c2 in zip(cells1, cells2)
        )
        if match:
            header_rows += 1
        else:
            headers_match = header_rows > 0
            break
    if header_rows == 0:
        headers_match = False
    return header_rows, headers_match


def _check_rows_match(soup1, soup2) -> bool:
    rows1 = soup1.find_all("tr")
    rows2 = soup2.find_all("tr")
    if not rows1 or not rows2:
        return False
    header_count, _ = _detect_table_headers(soup1, soup2)
    first_data = rows2[header_count] if len(rows2) > header_count else None
    if not first_data:
        return False
    return (
        _row_columns(rows1[-1]) == _row_columns(first_data)
        or _visual_columns(rows1[-1]) == _visual_columns(first_data)
    )


_SKIPPABLE_AFTER_TABLE = {"footer", "vision_footnote", "number", "footnote", "footer_image", "seal"}
_SKIPPABLE_BEFORE_TABLE = {"header", "header_image", "number", "seal"}
_CONTINUE_KEYWORDS = {"continue", "continued", "cont'd", "续", "续表", "续上表"}


def _is_table_skippable(block: Dict, allowed: set) -> bool:
    if block["label"] in allowed:
        return True
    return any(kw in str(block.get("content", "")).lower() for kw in _CONTINUE_KEYWORDS)


def _can_merge_tables(
    page_prev: List[Dict], prev_block: Dict,
    page_curr: List[Dict], curr_block: Dict,
) -> Tuple[bool, Any, Any]:
    from bs4 import BeautifulSoup

    x0, y0, x1, y1 = prev_block["bbox"]
    x2, y2, x3, y4 = curr_block["bbox"]
    prev_w, curr_w = x1 - x0, x3 - x2
    if prev_w == 0 or curr_w == 0:
        return False, None, None
    if abs(curr_w - prev_w) / min(curr_w, prev_w) >= 0.1:
        return False, None, None

    prev_idx = page_prev.index(prev_block)
    if not all(b["label"] in _SKIPPABLE_AFTER_TABLE for b in page_prev[prev_idx + 1:]):
        return False, None, None

    curr_idx = page_curr.index(curr_block)
    if not all(_is_table_skippable(b, _SKIPPABLE_BEFORE_TABLE) for b in page_curr[:curr_idx]):
        return False, None, None

    html_prev = prev_block.get("content", "")
    html_curr = curr_block.get("content", "")
    if not html_prev or not html_curr:
        return False, None, None

    soup_prev = BeautifulSoup(html_prev, "html.parser")
    soup_curr = BeautifulSoup(html_curr, "html.parser")
    cols_match = _table_total_columns(soup_prev) == _table_total_columns(soup_curr)
    return (cols_match or _check_rows_match(soup_prev, soup_curr)), soup_prev, soup_curr


def _perform_table_merge(soup_prev, soup_curr) -> str:
    header_count, _ = _detect_table_headers(soup_prev, soup_curr)
    rows_prev = soup_prev.find_all("tr")
    for row in soup_curr.find_all("tr")[header_count:]:
        row.extract()
        rows_prev[-1].parent.append(row)
    return str(soup_prev)


def _merge_tables_across_pages(blocks_by_page: List[List[Dict]]) -> List[List[Dict]]:
    """Merge tables spanning page boundaries.
    Ported from PaddleX v3.5.1 paddlex/inference/pipelines/layout_parsing/merge_table.py.
    """
    gid = 0
    for page in blocks_by_page:
        for b in page:
            b["_gid"] = gid
            b["_ggid"] = gid
            gid += 1

    for i in range(len(blocks_by_page) - 1, 0, -1):
        page_curr = blocks_by_page[i]
        page_prev = blocks_by_page[i - 1]
        curr_block = next((b for b in page_curr if b["label"] == "table"), None)
        prev_block = next((b for b in reversed(page_prev) if b["label"] == "table"), None)

        can_merge = False
        if curr_block and prev_block:
            can_merge, soup_prev, soup_curr = _can_merge_tables(
                page_prev, prev_block, page_curr, curr_block
            )

        if can_merge:
            prev_block["content"] = _perform_table_merge(soup_prev, soup_curr)
            curr_block["content"] = ""
            curr_block["_ggid"] = prev_block["_gid"]

    # Resolve transitive group links
    id_to_block = {b["_gid"]: b for page in blocks_by_page for b in page}
    for b in id_to_block.values():
        if b["_gid"] != b["_ggid"]:
            b["_ggid"] = id_to_block[b["_ggid"]]["_ggid"]

    return blocks_by_page


# ── Title-level helpers (ported from PaddleX v3.5.1 layout_parsing/title_level.py) ──

_SYMBOL_PATTERNS: Dict[str, re.Pattern] = {
    "ROMAN": re.compile(r"^\s*([IVX]+)(?:[\.．\)\s]|$)", re.I),
    "LETTER": re.compile(r"^\s*([A-Z])(?:[\.．\)\s])", re.I),
    "NUM_LIST": re.compile(r"^\s*(\d+(?:\.\d+)*)(?![）)])(?:[\.]?\s*|(?=[A-Z]))"),
    "NUM_LIST_WITH_BRACKET": re.compile(r"^\s*(?:[\(（])?(\d+(?:\.\d+)*)[\)）]"),
    "CHINESE_NUM": re.compile(
        r"^\s*(?:第|[（\(])?([一二三四五六七八九十]{1,2})"
        r"(?:[章节篇卷部条题讲课回）\)]|(?![a-zA-Z\u4e00-\u9fa5]))",
        re.I,
    ),
}

_SPECIAL_KEYWORDS = {
    "ABSTRACT": 1, "SUMMARY": 1, "RESUME": 1, "绪论": 1, "引言": 1,
    "CONTENTS": 1, "REFERENCES": 1, "REFERENCE": 1, "参考文献": 1,
    "APPENDIX": 1, "APPENDICES": 1, "附录": 1, "ACKNOWLEDGMENTS": 1,
    "INTRODUCTION": 1, "BACKGROUNDANDRELATEDWORK": 1, "BACKGROUND": 1,
    "RELATEDWORK": 1, "THEORETICALMODELS": 1, "DATA": 1, "METHOD": 1,
    "METHODS": 1, "METHODOLOGY": 1, "TOPICANALYSIS": 1, "RESULT": 1,
    "RESULTS": 1, "DISCUSSION": 1, "CONCLUSIONS": 1, "CONCLUSION": 1,
    "LIMITATIONS": 1, "研究背景": 1, "相关工作": 1, "研究方法": 1,
    "实验结果": 1, "讨论": 1, "结论": 1, "致谢": 1, "目录": 1,
}


def _get_symbol_and_level(content: str) -> Tuple[Optional[str], int]:
    txt = str(content).strip()
    if _SYMBOL_PATTERNS["NUM_LIST_WITH_BRACKET"].match(txt):
        return "NUM_LIST_BRACKET", 4
    if _SYMBOL_PATTERNS["ROMAN"].match(txt):
        return "ROMAN", 1
    if _SYMBOL_PATTERNS["CHINESE_NUM"].match(txt):
        return "CHINESE_NUM", 1
    if _SYMBOL_PATTERNS["LETTER"].match(txt):
        return "LETTER", 2
    m = _SYMBOL_PATTERNS["NUM_LIST"].match(txt)
    if m:
        return "NUM_LIST", m.group(1).count(".") + 1
    return None, -1


def _get_title_height(block: Dict) -> int:
    bbox = block["bbox"]
    x1, y1 = int(bbox[0]), int(bbox[1])
    x2, y2 = math.ceil(bbox[2]), math.ceil(bbox[3])
    h, w = y2 - y1, x2 - x1
    if h <= 0 or w <= 0:
        return 0
    lines = max(block.get("content", "").strip().count("\n") + 1, 1)
    return int(h / lines) if (w / h) >= 1.0 else int(w / lines)


def _cluster_heights(entries: List[Dict], k: int = 4) -> Dict[int, int]:
    """Rank-based height → level mapping (no sklearn required).
    Larger height → lower level number (more prominent heading).
    Equivalent to KMeans for the small distinct-value counts seen in practice.
    """
    heights = [e["height"] for e in entries if e["height"] > 0]
    if not heights:
        return {}
    unique_desc = sorted(set(heights), reverse=True)
    n = len(unique_desc)
    mapping: Dict[int, int] = {}
    for rank, h in enumerate(unique_desc):
        level = (int(rank * k / n) + 1) if n > 1 else 1
        mapping[h] = min(level, k)
    return mapping


def _compute_global_symbol_seq(
    entries: List[Dict], sym_level: Dict[int, Tuple]
) -> Dict[str, int]:
    seq: Dict[str, int] = {}
    counter = 1
    for idx, e in enumerate(entries):
        symbol, level = sym_level[idx]
        if level > 0 and symbol not in seq:
            seq[symbol] = counter
            counter += 1
    return seq


def _compute_levels_for_entries(entries: List[Dict]) -> List[Dict]:
    sym_level: Dict[int, Tuple] = {}
    for idx, e in enumerate(entries):
        symbol, level = _get_symbol_and_level(e["content"])
        e["symbol"] = symbol
        e["level"] = level
        sym_level[idx] = (symbol, level)

    cluster_map = _cluster_heights(entries)
    global_seq = _compute_global_symbol_seq(entries, sym_level)
    first_num_level = 0

    for idx, e in enumerate(entries):
        if e.get("level") == 0:
            continue
        symbol, level = sym_level[idx]
        cluster_level = cluster_map.get(e["height"], 1)
        keyword = str(e["content"]).upper().strip().rstrip("：: ").replace(" ", "")

        if level > 0:
            semantic_level = level
            if symbol == "NUM_LIST":
                if first_num_level != 0:
                    rel = global_seq.get(symbol, 1) + (level - first_num_level)
                else:
                    first_num_level = level
                    rel = global_seq.get(symbol, 1)
            else:
                rel = global_seq.get(symbol, 1)
            votes = [semantic_level, rel, cluster_level]
            mc = Counter(votes).most_common(1)
            final_level = mc[0][0] if mc[0][1] > 1 else rel
        elif keyword in _SPECIAL_KEYWORDS:
            final_level = _SPECIAL_KEYWORDS[keyword]
        else:
            final_level = cluster_level

        e["level"] = int(final_level)

    return entries


def _assign_levels_to_pages(blocks_by_page: List[List[Dict]]) -> List[List[Dict]]:
    """Assign title_level to paragraph_title blocks across all pages.
    Ported from PaddleX v3.5.1 paddlex/inference/pipelines/layout_parsing/title_level.py.
    doc_title is always # (level not assigned); paragraph_title gets level 1-4
    which maps to ## through ##### in _assemble_markdown.
    """
    entries = []
    for page in blocks_by_page:
        for block in page:
            if block["label"] == "paragraph_title":
                entries.append({
                    "origin_block": block,
                    "content": block.get("content", ""),
                    "height": _get_title_height(block),
                    "level": None,
                })

    if not entries:
        return blocks_by_page

    entries = _compute_levels_for_entries(entries)
    for e in entries:
        level = e.get("level")
        if level is not None and level > 0:
            e["origin_block"]["title_level"] = level

    return blocks_by_page


# ── engine ────────────────────────────────────────────────────────────────────

class PaddleOCRVLEngine:
    """Two-stage document OCR using PP-DocLayoutV2 + PaddleOCR-VL-1.5."""

    capabilities = EngineCapabilities(
        uses_shared_postprocess=False,
        emits_native_artifacts=True,
        requires_layout_service=True,
    )

    def __init__(self, parser: Any):
        self.parser = parser
        self.name = "paddleocr-vl"
        layout_url = getattr(parser, "layout_detection_url", None) or "http://localhost:30002"
        self._layout_url = layout_url.rstrip("/")
        self._layout_concurrency = getattr(parser, "paddle_layout_concurrency", 0) or 0
        self._block_backpressure_high = (
            getattr(parser, "paddle_block_backpressure_high_watermark", 0) or 0
        )
        self._block_backpressure_low = (
            getattr(parser, "paddle_block_backpressure_low_watermark", 0) or 0
        )
        self._block_concurrency = getattr(parser, "block_concurrency", 0) or 0

    def _metrics(self) -> Dict[str, Any]:
        metrics = getattr(self.parser, "paddleocr_vl_metrics", None)
        if not isinstance(metrics, dict):
            metrics = {}
            setattr(self.parser, "paddleocr_vl_metrics", metrics)
        return metrics

    def _increment_metric(self, key: str, amount: int = 1) -> None:
        metrics = self._metrics()
        metrics[key] = int(metrics.get(key, 0) or 0) + amount

    def _add_float_metric(self, key: str, amount: float) -> None:
        metrics = self._metrics()
        metrics[key] = float(metrics.get(key, 0.0) or 0.0) + float(amount)

    def _get_layout_semaphore(self) -> Optional[asyncio.Semaphore]:
        limit = int(self._layout_concurrency or 0)
        if limit <= 0:
            return None
        sem = getattr(self.parser, "_paddle_layout_semaphore", None)
        sem_limit = getattr(self.parser, "_paddle_layout_semaphore_limit", None)
        if sem is None or sem_limit != limit:
            sem = asyncio.Semaphore(limit)
            setattr(self.parser, "_paddle_layout_semaphore", sem)
            setattr(self.parser, "_paddle_layout_semaphore_limit", limit)
        return sem

    async def _wait_for_block_backpressure(self) -> None:
        high = int(self._block_backpressure_high or 0)
        if high <= 0:
            return

        low = int(self._block_backpressure_low or 0)
        if low >= high:
            low = high - 1

        waited = False
        wait_started = time.monotonic()
        while int(self._metrics().get("_paddle_blocks_pending", 0) or 0) >= high:
            waited = True
            self._increment_metric("paddle_block_backpressure_wait_count")
            await asyncio.sleep(0.005)
            if int(self._metrics().get("_paddle_blocks_pending", 0) or 0) <= low:
                break

        if waited:
            self._add_float_metric(
                "paddle_block_backpressure_wait_seconds_total",
                max(0.0, time.monotonic() - wait_started),
            )

    # ── image helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _pil_to_b64(img: Image.Image) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _pil_to_data_url(img: Image.Image) -> str:
        return f"data:image/png;base64,{PaddleOCRVLEngine._pil_to_b64(img)}"

    async def _encode_async(self, img: Image.Image) -> str:
        loop = asyncio.get_event_loop()
        return await run_in_encode_lane(
            self.parser,
            lambda: loop.run_in_executor(None, PaddleOCRVLEngine._pil_to_data_url, img),
        )

    # ── stage 1: layout detection ─────────────────────────────────────────────

    async def _detect_layout(self, image: Image.Image) -> List[Dict[str, Any]]:
        await self._wait_for_block_backpressure()
        semaphore = self._get_layout_semaphore()
        if semaphore is None:
            return await self._detect_layout_uncapped(image)

        metrics = self._metrics()
        pending_key = "_paddle_layout_pending"
        metrics[pending_key] = int(metrics.get(pending_key, 0) or 0) + 1
        metrics["paddle_layout_queue_depth"] = max(
            int(metrics.get("paddle_layout_queue_depth", 0) or 0),
            int(metrics[pending_key]),
        )
        try:
            async with semaphore:
                await self._wait_for_block_backpressure()
                return await self._detect_layout_uncapped(image)
        finally:
            metrics[pending_key] = max(0, int(metrics.get(pending_key, 0) or 0) - 1)

    async def _detect_layout_uncapped(self, image: Image.Image) -> List[Dict[str, Any]]:
        loop = asyncio.get_event_loop()
        b64 = await run_in_encode_lane(
            self.parser,
            lambda: loop.run_in_executor(None, self._pil_to_b64, image),
        )
        payload = {"image_b64": b64, "use_paddlex_filter_boxes": True}
        metrics = self._metrics()
        metrics["paddle_layout_api_call_count"] = int(metrics.get("paddle_layout_api_call_count", 0) or 0) + 1
        metrics["paddle_layout_api_inflight"] = int(metrics.get("paddle_layout_api_inflight", 0) or 0) + 1
        metrics["paddle_layout_api_inflight_peak"] = max(
            int(metrics.get("paddle_layout_api_inflight_peak", 0) or 0),
            int(metrics["paddle_layout_api_inflight"]),
        )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"{self._layout_url}/detect", json=payload)
                resp.raise_for_status()
        finally:
            metrics["paddle_layout_api_inflight"] = max(
                0,
                int(metrics.get("paddle_layout_api_inflight", 0) or 0) - 1,
            )
        boxes = resp.json()["boxes"]
        boxes.sort(key=lambda b: b.get("index", 0))
        return boxes

    # ── stage 2: per-block VLM inference ─────────────────────────────────────

    def _prompt_for_label(self, label: str) -> Optional[str]:
        if label in _DISCARD_LABELS or label in _IMAGE_LABELS:
            return None
        if label in _TABLE_LABELS:
            return "Table Recognition:"
        if label in _FORMULA_LABELS:
            return "Formula Recognition:"
        return "OCR:"

    async def _call_vlm(self, data_url: str, prompt: str) -> str:
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

    async def _recognize_block(
        self, block: Dict[str, Any], crop: Image.Image
    ) -> Dict[str, Any]:
        label = block["label"]
        prompt = self._prompt_for_label(label)
        if prompt is None:
            return {**block, "content": ""}
        data_url = await self._encode_async(crop)
        content = await self._call_vlm(data_url, prompt)
        return {**block, "content": content.strip()}

    # ── fallback: single-stage ────────────────────────────────────────────────

    async def _infer_single_stage(
        self, image: Image.Image
    ) -> Tuple[List[Dict[str, Any]], str]:
        data_url = await self._encode_async(image)
        text = await self._call_vlm(data_url, "OCR:")
        return [], text

    # ── two-stage pipeline ────────────────────────────────────────────────────

    async def _infer_two_stage(
        self, image_path: str, save_dir: str = "", page_num: int = 0
    ) -> Tuple[List[Dict[str, Any]], str]:
        with Image.open(image_path) as img:
            original = img.convert("RGB")

        metrics = TwoStageMetrics(engine_name=self.name)
        layout_start = time.monotonic()
        try:
            boxes = await self._detect_layout(original)
        except Exception:
            layout_latency = max(0.0, time.monotonic() - layout_start)
            metrics.layout_latency_seconds_total += layout_latency
            self._add_float_metric("paddle_layout_latency_seconds_total", layout_latency)
            self._increment_metric("paddle_layout_failures")
            self._increment_metric("paddle_single_stage_fallbacks")
            record_two_stage_metrics(self.parser, metrics)
            return await self._infer_single_stage(original)
        layout_latency = max(0.0, time.monotonic() - layout_start)
        metrics.layout_latency_seconds_total += layout_latency
        self._add_float_metric("paddle_layout_latency_seconds_total", layout_latency)

        if not boxes:
            self._increment_metric("paddle_single_stage_fallbacks")
            record_two_stage_metrics(self.parser, metrics)
            return await self._infer_single_stage(original)

        images_dir: Optional[str] = None
        if save_dir and any(b["label"] in _IMAGE_LABELS for b in boxes):
            images_dir = os.path.join(save_dir, "native", self.name, "images")
            os.makedirs(images_dir, exist_ok=True)

        def _save_crop(crop: Image.Image, label: str, idx: int) -> str:
            fname = f"page_{page_num:04d}_{label}_{idx:03d}.jpg"
            crop_rgb = crop.convert("RGB") if crop.mode != "RGB" else crop
            crop_rgb.save(os.path.join(images_dir, fname), "JPEG", quality=92)
            return f"images/{fname}"

        loop = asyncio.get_event_loop()

        layout_blocks: List[LayoutBlock] = []
        for idx, block in enumerate(boxes):
            crop = original.crop(block["bbox"])
            img_path = None
            if block["label"] in _IMAGE_LABELS and images_dir:
                img_path = await loop.run_in_executor(None, _save_crop, crop, block["label"], idx)
            layout_blocks.append(
                LayoutBlock(
                    block_id=f"{page_num}:{idx}",
                    label=block["label"],
                    bbox=tuple(block["bbox"]),
                    prompt=self._prompt_for_label(block["label"]),
                    payload=(block, crop, img_path),
                )
            )
        del original

        recognizable_count = sum(1 for block in layout_blocks if block.prompt is not None)
        self._increment_metric("paddle_blocks_detected", len(layout_blocks))
        paddle_metrics = self._metrics()
        paddle_metrics["_paddle_blocks_pending"] = (
            int(paddle_metrics.get("_paddle_blocks_pending", 0) or 0) + recognizable_count
        )
        paddle_metrics["paddle_block_queue_depth"] = max(
            int(paddle_metrics.get("paddle_block_queue_depth", 0) or 0),
            int(paddle_metrics.get("_paddle_blocks_pending", 0) or 0),
        )

        async def _recognize(layout_block: LayoutBlock) -> str:
            block, crop, _img_path = layout_block.payload
            result = await self._recognize_block(block, crop)
            return result.get("content", "")

        block_concurrency = self._block_concurrency or len(layout_blocks) or 1
        try:
            recognized_results, metrics = await recognize_layout_blocks(
                layout_blocks,
                _recognize,
                concurrency=block_concurrency,
                engine_name=self.name,
                metrics=metrics,
            )
        finally:
            paddle_metrics["_paddle_blocks_pending"] = max(
                0,
                int(paddle_metrics.get("_paddle_blocks_pending", 0) or 0) - recognizable_count,
            )
        self._increment_metric(
            "paddle_blocks_recognized",
            sum(1 for result in recognized_results if not result.skipped),
        )
        record_two_stage_metrics(self.parser, metrics)

        recognized = []
        for result in recognized_results:
            block, _crop, img_path = result.block.payload
            recognized.append({**block, "content": result.content.strip(), "img_path": img_path})

        blocks = list(recognized)
        return blocks, self._assemble_markdown(blocks)

    # ── markdown assembly ─────────────────────────────────────────────────────

    def _assemble_markdown(self, blocks: List[Dict[str, Any]]) -> str:
        # Official reference: ppstructure/recovery/recovery_to_markdown.py
        # figure → <div align="center"><img src="images/xxx.jpg"></div>
        # paragraph_title title_level: level N → "#" * (N+1) heading
        # (level 1 → ##, level 2 → ###, …) matching paddlex markdown_format_funcs.py
        parts: List[str] = []
        for block in blocks:
            label = block["label"]
            if label in _DISCARD_LABELS:
                continue

            img_path: Optional[str] = block.get("img_path")

            if label in _IMAGE_LABELS:
                if img_path:
                    parts.append(
                        f'<div align="center">\n\t<img src="{img_path}">\n</div>'
                    )
                continue

            content = (block.get("content") or "").strip()
            if not content:
                continue

            if label == "doc_title":
                parts.append(f"# {content}")
            elif label == "paragraph_title":
                level = block.get("title_level")
                hashes = "#" * (level + 1) if level else "##"
                parts.append(f"{hashes} {content}")
            elif label in _TABLE_LABELS:
                if _looks_like_otsl(content):
                    html = convert_otsl_to_html(content)
                    parts.append(html if html else content)
                else:
                    parts.append(content)
            elif label == "display_formula":
                parts.append(f"$$\n{_strip_formula_delimiters(content)}\n$$")
            elif label == "inline_formula":
                parts.append(f"${_strip_formula_delimiters(content)}$")
            else:
                parts.append(content)

        return "\n\n".join(parts)

    # ── retry wrapper ─────────────────────────────────────────────────────────

    async def _run_with_retries(self, coro_factory) -> Any:
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

    # ── public interface ──────────────────────────────────────────────────────

    async def process_page(self, page_data: Dict[str, Any]) -> EnginePageResult:
        page_idx = page_data["page_idx"]
        original_page_num = page_data["original_page_num"]
        save_dir = page_data["save_dir"]
        image_path = page_data.get("processed_image_path") or page_data.get("origin_image_path")

        blocks, md_content = await self._run_with_retries(
            lambda: self._infer_two_stage(image_path, save_dir=save_dir, page_num=original_page_num)
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

        return EnginePageResult(
            page_no=page_idx,
            original_page_num=original_page_num,
            status="success_fallback_text",
            raw_response=md_content,
            md_content=md_content,
            cells=blocks if blocks else None,
            native_artifacts=artifacts,
        )

    async def finalize_document(
        self, page_results: List[Dict[str, Any]], save_dir: str, filename: str
    ) -> List[Dict[str, str]]:
        # Collect per-page blocks for cross-page post-processing.
        # Falls back to string concatenation if any page lacks block data
        # (e.g. single-stage VLM fallback when layout detection failed).
        pages_with_blocks: List[Tuple[int, List[Dict[str, Any]]]] = []
        md_by_page: List[str] = []

        for page_result in page_results:
            if isinstance(page_result, EnginePageResult):
                md = page_result.md_content
                cells = page_result.cells
            else:
                md = page_result.get("md_content", "")
                cells = page_result.get("cells")

            md_by_page.append(md.strip() if md else "")
            if cells is not None:
                pages_with_blocks.append((len(md_by_page) - 1, cells))

        all_have_blocks = len(pages_with_blocks) == len(page_results)

        if all_have_blocks and len(pages_with_blocks) > 1:
            # Cross-page post-processing: merge tables then relevel titles.
            # Mirrors PaddleX v3.5.1 restructure_pages(merge_tables=True, relevel_titles=True).
            blocks_by_page = [cells for _, cells in pages_with_blocks]
            _merge_tables_across_pages(blocks_by_page)
            _assign_levels_to_pages(blocks_by_page)
            # Reassemble markdown for each page using updated blocks
            final_parts = [
                self._assemble_markdown(blks)
                for blks in blocks_by_page
            ]
        else:
            final_parts = md_by_page

        combined = "\n\n".join(p for p in final_parts if p)
        if not combined:
            return []

        artifact = await async_write_native_text(
            save_dir,
            self.name,
            f"{filename}.md",
            combined,
            kind="document_markdown",
        )
        return [artifact.__dict__]
