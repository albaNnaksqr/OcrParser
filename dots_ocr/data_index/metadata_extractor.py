import os
import hashlib
import re
from pathlib import Path
from typing import Dict

import fitz

from . import utils

# --- Language Detection Constants ---
LANGUAGE_PATTERNS: Dict[str, str] = {
    "Chinese": r'[\u4e00-\u9fa5]',
    "English": r'[A-Za-z]',
    "French": r'[àâçéèêëîïôûùüÿñæœ]',
    "German": r'[äöüßÄÖÜ]',
    "Spanish": r'[áéíóúüñÁÉÍÓÚÜÑ]',
    "Russian": r'[А-Яа-яЁё]',
    "Japanese": r'[ぁ-ゔァ-ヴ一-龥々〆〤]',
    "Arabic": r'[\u0600-\u06FF]',
    "Korean": r'[가-힣]',
    "Thai": r'[ก-๛]',
    "Turkish": r'[çğıöşüÇĞİÖŞÜ]',
    "Portuguese": r'[áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ]',
    "Italian": r'[àèéìíòóùÀÈÉÌÍÒÓÙ]',
    "Hungarian": r'[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]',
    "Czech": r'[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]',
    "Swedish": r'[åäöÅÄÖ]',
    "Serbian": r'[А-Яа-яЇїЂђЈјЉљЊњЋћЌќЏџ]',
    "Vietnamese": r'[àáâãèéêìíòóôõùúýăđĩũơưẠ-ỹ]',
    "Malay": r'[A-Za-z]',
    "Indonesian": r'[A-Za-z]',
    "Urdu": r'[\u0600-\u06FF]',
    "Persian": r'[\u0600-\u06FF]',
    "Afrikaans": r'[A-Za-z]',
}

LANGUAGE_PRIORITY = [
    "Chinese", "English", "Japanese", "Korean", "Russian", "Spanish",
    "French", "German", "Portuguese", "Italian", "Hungarian", "Czech",
    "Swedish", "Serbian", "Turkish", "Vietnamese", "Thai", "Arabic",
    "Urdu", "Persian", "Malay", "Indonesian", "Afrikaans",
]

# --- Regex Patterns ---
MD_TABLE_LINE = re.compile(r'^\s*\|.*\|\s*$')
HTML_TABLE_PATTERN = re.compile(r'<table\b.*?>.*?</table>', re.IGNORECASE | re.DOTALL)
MD_IMAGE_PATTERN = re.compile(r'!\[[^\]]*\]\([^)]+\)')
HTML_IMAGE_PATTERN = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
LATEX_ENV_NAMES = (
    "equation",
    "equation*",
    "align",
    "align*",
    "aligned",
    "gather",
    "gather*",
    "multline",
    "cases",
    "split",
    "displaymath",
)
LATEX_INLINE_PATTERN = re.compile(r'(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)', re.DOTALL)
LATEX_BLOCK_PATTERN = re.compile(r'\$\$(.+?)\$\$', re.DOTALL)
LATEX_PAREN_PATTERN = re.compile(r'\\\((.+?)\\\)', re.DOTALL)
LATEX_BRACKET_PATTERN = re.compile(r'\\\[(.+?)\\\]', re.DOTALL)
LATEX_ENV_PATTERN = re.compile(
    r'\\begin\{(' + "|".join(LATEX_ENV_NAMES) + r')\}(.+?)\\end\{\1\}',
    re.DOTALL,
)
LATEX_COMMAND_PATTERN = re.compile(r'\\[A-Za-z]+')
HTML_MATH_PATTERN = re.compile(r'<math\b.*?</math>', re.IGNORECASE | re.DOTALL)
CHEM_EQUATION_PATTERN = re.compile(
    r'(?:[A-Z][a-z]?\d*(?:\([A-Za-z]+\))?)(?:\s*(?:\+|->|\\rightarrow|⇌)\s*(?:[A-Z][a-z]?\d*(?:\([A-Za-z]+\))?))+'
)
MATH_VARIABLE_PATTERN = re.compile(r'[A-Za-z]\s*(?:[=+\-*/^])\s*[A-Za-z0-9]')
MATH_NUMBER_PATTERN = re.compile(r'\d+\s*(?:[=+\-*/^])\s*\d+')
UNICODE_MATH_PATTERN = re.compile(r'[∑∫√≈≠≤≥∞±·×÷∂∇πΩ∆Σ]')
SYMBOL_STRIP_PATTERN = re.compile(r'[\d\s\W_]+', re.UNICODE)


def get_file_extension(file_path: Path) -> str:
    return file_path.suffix.lower().strip('.')

def get_file_name(file_path: Path) -> str:
    return file_path.name

def get_file_size_bytes(file_path: Path) -> int:
    return os.path.getsize(file_path)

def get_checksum_md5(file_path: Path) -> str:
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except OSError:
        return "unknown"

def get_pdf_page_count(file_path: Path) -> int:
    try:
        with fitz.open(file_path) as doc:
            page_count = getattr(doc, "page_count", None)
            if page_count is None:
                page_count = len(doc)
            return max(int(page_count), 0)
    except Exception:
        return 0

def contains_table(text: str) -> bool:
    lines = text.splitlines()
    table_lines = 0
    for line in lines:
        if MD_TABLE_LINE.match(line):
            table_lines += 1
            if table_lines >= 3:
                return True
        else:
            table_lines = 0
    return bool(HTML_TABLE_PATTERN.search(text))

def contains_image(text: str) -> bool:
    if MD_IMAGE_PATTERN.search(text):
        return True
    return bool(HTML_IMAGE_PATTERN.search(text))

MATH_KEYWORDS = (
    "\\frac",
    "\\sum",
    "\\int",
    "\\sqrt",
    "\\alpha",
    "\\beta",
    "\\gamma",
    "\\theta",
    "\\lambda",
    "\\pi",
    "\\sin",
    "\\cos",
    "\\tan",
    "\\log",
    "\\lim",
    "\\mathbf",
    "\\mathrm",
    "\\left",
    "\\right",
)
MATH_OPERATOR_CHARS = set("=+-*/^_")


def _snippet_looks_like_math(snippet: str) -> bool:
    snippet = snippet.strip()
    if not snippet:
        return False
    lowered = snippet.lower()
    if any(keyword in lowered for keyword in MATH_KEYWORDS):
        return True
    if LATEX_COMMAND_PATTERN.search(snippet):
        return True
    if UNICODE_MATH_PATTERN.search(snippet):
        return True
    if MATH_NUMBER_PATTERN.search(snippet):
        return True
    if MATH_VARIABLE_PATTERN.search(snippet):
        return True
    operator_hits = sum(1 for ch in snippet if ch in MATH_OPERATOR_CHARS)
    alnum_hits = sum(1 for ch in snippet if ch.isalnum())
    if operator_hits >= 2 and alnum_hits >= 2:
        return True
    if ("->" in snippet or "\\rightarrow" in snippet) and alnum_hits >= 2:
        return True
    return False


def contains_equation(text: str) -> bool:
    if not text:
        return False
    for pattern in (
        LATEX_BLOCK_PATTERN,
        LATEX_PAREN_PATTERN,
        LATEX_BRACKET_PATTERN,
        LATEX_ENV_PATTERN,
    ):
        for match in pattern.finditer(text):
            if _snippet_looks_like_math(match.group(0)):
                return True
    for inline_match in LATEX_INLINE_PATTERN.finditer(text):
        if _snippet_looks_like_math(inline_match.group(1)):
            return True
    if HTML_MATH_PATTERN.search(text):
        return True
    return bool(CHEM_EQUATION_PATTERN.search(text))

def detect_language(text: str) -> str:
    analyzable = SYMBOL_STRIP_PATTERN.sub('', text)
    if not analyzable:
        return "Others"

    counts = {
        lang: len(re.findall(pattern, analyzable))
        for lang, pattern in LANGUAGE_PATTERNS.items()
    }
    max_count = max(counts.values())
    if max_count == 0:
        return "Others"

    candidates = [lang for lang, count in counts.items() if count == max_count]
    for lang in LANGUAGE_PRIORITY:
        if lang in candidates:
            return lang
    return candidates[0]

def extract_all_metadata(pdf_path: Path, md_path: Path) -> Dict[str, object]:
    """
    Extracts metadata needed by the data-index pipeline.

    Note: In S3 download+upload pipelines the original PDF may be removed after OCR
    to save disk space. In that case we still want data-index to be able to run
    based on the generated Markdown. For missing PDFs we fill best-effort fields
    (file_size/page_count/checksum) with neutral defaults.
    """

    if not md_path.exists():
        raise FileNotFoundError(f"Markdown not found: {md_path}")

    # Read full MD once for feature extraction and also keep a small head
    md_content = utils.read_text_file(md_path)
    md_head = md_content[:5000] if md_content else ""

    pdf_exists = pdf_path.exists()
    file_size = 0
    checksum = "unknown"
    page_count = 0
    if pdf_exists:
        try:
            file_size = get_file_size_bytes(pdf_path)
        except OSError:
            file_size = 0
        checksum = get_checksum_md5(pdf_path) or "unknown"
        page_count = get_pdf_page_count(pdf_path)

    metadata = {
        "file_extension": get_file_extension(pdf_path),
        "file_name": get_file_name(pdf_path),
        "file_size": int(file_size),
        "checksum_md5": checksum,
        "page_count": int(page_count),
        "language": detect_language(md_content),
        "contains_table": contains_table(md_content),
        "contains_image": contains_image(md_content),
        "contains_equation": contains_equation(md_content),
        # Pass a small preview downstream to avoid re-reading the file
        "md_head": md_head,
    }
    return metadata
