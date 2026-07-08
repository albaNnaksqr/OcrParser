from __future__ import annotations

import json
import re


SUCCESS_STATUSES = {
    "success",
    "skipped_blank",
    "success_fallback_text",
    "success_fallback_image",
}

NEW_PARAGRAPH_PATTERN = re.compile(
    r"^\s*("
    r"#+|\u3000\u3000|[■●◆➢]|"
    r"[([（【].*?[)）】]|"
    r"\d+[.、)\s]|"
    r"[a-zA-Z][.)\s]|"
    r"[一二三四五六七八九十百千]+[、.)\s]|"
    r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]"
    r")"
)
english_letter_pattern = re.compile(r"^[a-zA-Z]")
ends_with_english_pattern = re.compile(r"[a-zA-Z]$")
starts_with_chinese_pattern = re.compile(r"^[一-龥]")
contains_chinese_pattern = re.compile(r"[一-龥]")
ends_with_digit_pattern = re.compile(r"\d$")
SUMMARY_ANCHOR_PATTERN = re.compile(
    r"(\*\*\s*(摘要|Abstract)\s*[：:]*\s*\*\*)|(【\s*(摘要|Abstract)\s*[：:]*\s*】)|(\[\s*(摘要|Abstract)\s*[：:]*\s*\])",
    re.IGNORECASE,
)
ALLOWED_CATEGORIES_BEFORE_SUMMARY = {"Title", "Section-header"}
CODE_BLOCK_PATTERN = re.compile(r"```.*?```", re.S)
INLINE_CODE_PATTERN = re.compile(r"`[^`]+`")
IMAGE_LINK_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]*\)")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
MULTISPACE_PATTERN = re.compile(r"\s+")
WORD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
CHINESE_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff]")
PAGE_TAG_PATTERN = re.compile(r"<special_page_num_tag>.*?</special_page_num_tag>")


def _is_english_to_chinese_transition(self, text1: str, text2: str) -> bool:
    if not text1 or not text2:
        return False
    if not self.ends_with_english_pattern.search(text1.rstrip()):
        return False
    if not self.contains_chinese_pattern.search(text2):
        return False
    return True


def _locate_first_page_summary_anchor(self, cells):
    for idx, cell in enumerate(cells):
        text = cell.get("text") or ""
        if text and self.SUMMARY_ANCHOR_PATTERN.search(text):
            return idx
    return None


def _trim_first_page_blocks(self, cells):
    anchor_idx = self._locate_first_page_summary_anchor(cells)
    if anchor_idx is None:
        return cells
    trimmed_cells = []
    for idx, cell in enumerate(cells):
        if idx < anchor_idx and cell.get("category") not in self.ALLOWED_CATEGORIES_BEFORE_SUMMARY:
            continue
        trimmed_cells.append(cell)
    return trimmed_cells


@classmethod
def _clean_markdown_text(cls, text: str) -> str:
    if not text:
        return ""
    cleaned = cls.CODE_BLOCK_PATTERN.sub(" ", text)
    cleaned = cls.INLINE_CODE_PATTERN.sub(" ", cleaned)
    cleaned = cls.IMAGE_LINK_PATTERN.sub(" ", cleaned)
    cleaned = cls.MARKDOWN_LINK_PATTERN.sub(r"\1", cleaned)
    cleaned = cleaned.replace("<special_page_num_tag>", " ").replace("</special_page_num_tag>", " ")
    cleaned = cls.PAGE_TAG_PATTERN.sub(" ", cleaned)
    cleaned = cls.HTML_TAG_PATTERN.sub(" ", cleaned)
    cleaned = cleaned.replace("#", " ").replace("*", " ").replace("_", " ").replace(">", " ")
    return cls.MULTISPACE_PATTERN.sub(" ", cleaned)


@classmethod
def _count_words_from_text(cls, text: str) -> int:
    if not text:
        return 0
    cleaned = cls._clean_markdown_text(text)
    chinese_chars = cls.CHINESE_CHAR_PATTERN.findall(cleaned)
    word_tokens = cls.WORD_TOKEN_PATTERN.findall(cleaned)
    return len(chinese_chars) + len(word_tokens)


def _load_filter_keywords(self):
    if not self.keyword_filter_config:
        return [], set()
    try:
        with open(self.keyword_filter_config, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        keywords = []
        for category in config:
            if "关键词" in category:
                keywords.extend(category["关键词"])
        if keywords:
            self._console_write(f"Loaded {len(keywords)} keywords for filtering from {self.keyword_filter_config}")
        return list(set(keywords)), {"Text", "List-item", "Section-header", "Footnote"}
    except Exception as exc:
        self._console_write(f"Error loading keyword filter config: {exc}", level="error")
        return [], set()
