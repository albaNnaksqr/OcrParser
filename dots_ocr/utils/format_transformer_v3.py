import os
import sys
import json
import re

from PIL import Image
Image.MAX_IMAGE_PIXELS = 200000000
from dots_ocr.utils.image_utils import PILimage_to_base64

_ESCAPE_PATTERN = re.compile(r'\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|n|r|t)')
_UNICODE_ESCAPE_PATTERN = re.compile(r'\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})')
_DUP_LITERAL_BRACES_PATTERN = re.compile(r'(\\[{}])\1+')

def ensure_section_headers_have_markers(cells: list, text_key: str = 'text') -> None:
    """
    Ensure Section-header cells begin with Markdown heading markers.
    Mutates the provided cell list in-place; does nothing when the input is malformed.
    """
    if not isinstance(cells, list):
        return

    last_marker_count = None
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if cell.get('category') != 'Section-header':
            continue

        raw_text = cell.get(text_key, "")
        if not isinstance(raw_text, str):
            continue

        stripped_text = raw_text.lstrip()
        if stripped_text.startswith('#'):
            match = re.match(r'^(#+)', stripped_text)
            if match:
                last_marker_count = min(len(match.group(1)), 3)
            continue

        prev_count = last_marker_count if last_marker_count is not None else 1
        marker_count = max(2, min(prev_count + 1, 3))
        prefix = '#' * marker_count

        remaining = stripped_text.lstrip()
        if remaining:
            fixed_text = f"{prefix} {remaining}"
        else:
            fixed_text = f"{prefix} "

        cell[text_key] = fixed_text
        last_marker_count = marker_count


def unescape_basic_sequences(text: str, *, convert_controls: bool = True) -> str:
    """
    Convert escaped sequences into their literal characters.

    Args:
        text: The text to unescape.
        convert_controls: When True, also convert standard control sequences like \\n, \\r, \\t.
                          When False, only unicode escapes are decoded so LaTeX control words remain intact.
    """
    if not text or '\\' not in text:
        return text

    pattern = _ESCAPE_PATTERN if convert_controls else _UNICODE_ESCAPE_PATTERN

    def _replace(match: re.Match) -> str:
        seq = match.group(1)
        if seq == 'n':
            if not convert_controls:
                return match.group(0)
            return '\n'
        if seq == 'r':
            if not convert_controls:
                return match.group(0)
            return '\r'
        if seq == 't':
            if not convert_controls:
                return match.group(0)
            return '\t'
        if seq.startswith('u') and len(seq) == 5:
            try:
                return chr(int(seq[1:], 16))
            except ValueError:
                return match.group(0)
        if seq.startswith('U') and len(seq) == 9:
            try:
                return chr(int(seq[1:], 16))
            except ValueError:
                return match.group(0)
        return match.group(0)

    previous = None
    current = text
    while previous != current:
        previous = current
        current = pattern.sub(_replace, current)
        if convert_controls:
            current = current.replace('\\\r\n', '\n').replace('\\\n', '\n').replace('\\\r', '\r')
    return current


TEXTUAL_CATEGORIES = {
    "Text",
    "Section-header",
    "Title",
    "Caption",
    "List-item",
    "Footnote",
    "Page-header",
    "Page-footer",
    "Other",
    "Unknown",
}


def _convert_controls_outside_math(text: str) -> str:
    """Convert \\r, \\n, \\t sequences to real control characters outside of math segments."""
    if not text or '\\' not in text:
        return text

    result = []
    i = 0
    length = len(text)
    math_stack = []

    def _is_escaped_dollar(idx: int) -> bool:
        backslashes = 0
        j = idx - 1
        while j >= 0 and text[j] == '\\':
            backslashes += 1
            j -= 1
        return backslashes % 2 == 1

    while i < length:
        ch = text[i]

        # Handle $ / $$ math regions
        if ch == '$' and not _is_escaped_dollar(i):
            j = i
            while j < length and text[j] == '$':
                j += 1
            delim = text[i:j]
            result.append(delim)
            i = j
            if math_stack and math_stack[-1] == delim:
                math_stack.pop()
            else:
                math_stack.append(delim)
            continue

        if ch == '\\':
            # Handle \( \) and \[ \] math regions
            if text.startswith('\\(', i):
                result.append('\\(')
                math_stack.append('\\)')
                i += 2
                continue
            if text.startswith('\\[', i):
                result.append('\\[')
                math_stack.append('\\]')
                i += 2
                continue
            if math_stack and text.startswith(math_stack[-1], i):
                closing = math_stack.pop()
                result.append(closing)
                i += len(closing)
                continue

            if math_stack:
                result.append(ch)
                i += 1
                continue

            # Outside math regions: convert control sequences
            if text.startswith('\\r\\n', i):
                result.append('\n')
                i += 4
                continue
            if text.startswith('\\n', i):
                result.append('\n')
                i += 2
                continue
            if text.startswith('\\r', i):
                result.append('\n')
                i += 2
                continue
        result.append(ch)
        i += 1

    return ''.join(result)


def _dedupe_escaped_braces(text: str) -> str:
    """Collapse repeated escaped braces like \\}\\} -> \\} to avoid malformed math."""
    if not text or '\\' not in text:
        return text
    return _DUP_LITERAL_BRACES_PATTERN.sub(r'\1', text)


def _smart_join(chunks):
    """Join text chunks while inserting spaces between ASCII words only when needed."""
    result = ""
    for chunk in chunks:
        if not chunk:
            continue
        if not result:
            result = chunk
            continue

        left = result[-1]
        right = chunk[0]
        if left.isascii() and left.isalnum() and right.isascii() and right.isalnum():
            result += " " + chunk
        else:
            result += chunk
    return result


def remove_textual_newlines(category: str, text: str) -> str:
    """Collapse newlines for textual categories to match legacy behavior, except headings."""
    if not text:
        return text
    if category == 'Section-header':
        return text
    if category == 'Text':
        lines = text.splitlines()
        if any(line.lstrip().startswith('#') for line in lines):
            return text
    if category in TEXTUAL_CATEGORIES:
        lines = [line.strip() for line in re.split(r'[\r\n]+', text) if line.strip()]
        if not lines:
            return ""

        if category == 'Title':
            first_line = lines[0]
            match = re.match(r'^(#+)(\s*)(.*)$', first_line)
            if match:
                marker = match.group(1)
                content = match.group(3).strip()
                segments = []
                if content:
                    segments.append(content)
                for extra in lines[1:]:
                    stripped = extra.strip()
                    if not stripped:
                        continue
                    if stripped.startswith('#'):
                        stripped = re.sub(r'^#+\s*', '', stripped).strip()
                    segments.append(stripped)
                merged = _smart_join(segments)
                text = f"{marker} {merged}".strip() if merged else marker
            else:
                text = _smart_join(lines)

        else:
            text = _smart_join(lines)
    return text


def has_latex_markdown(text: str) -> bool:
    """
    Checks if a string contains LaTeX markdown patterns.
    
    Args:
        text (str): The string to check.
        
    Returns:
        bool: True if LaTeX markdown is found, otherwise False.
    """
    if not isinstance(text, str):
        return False
    
    # Define regular expression patterns for LaTeX markdown
    latex_patterns = [
        r'\$\$.*?\$\$',           # Block-level math formula $$...$$
        r'\$[^$\n]+?\$',          # Inline math formula $...$
        r'\\begin\{.*?\}.*?\\end\{.*?\}',  # LaTeX environment \begin{...}...\end{...}
        r'\\[a-zA-Z]+\{.*?\}',    # LaTeX command \command{...}
        r'\\[a-zA-Z]+',           # Simple LaTeX command \command
        r'\\\[.*?\\\]',           # Display math formula \[...\]
        r'\\\(.*?\\\)',           # Inline math formula \(...\)
    ]
    
    # Check if any of the patterns match
    for pattern in latex_patterns:
        if re.search(pattern, text, re.DOTALL):
            return True
    
    return False


def clean_latex_preamble(latex_text: str) -> str:
    """
    Removes LaTeX preamble commands like document class and package imports.
    
    Args:
        latex_text (str): The original LaTeX text.

    Returns:
        str: The cleaned LaTeX text without preamble commands.
    """
    # Define patterns to be removed
    patterns = [
        r'\\documentclass\{[^}]+\}',  # \documentclass{...}
        r'\\usepackage\{[^}]+\}',    # \usepackage{...}
        r'\\usepackage\[[^\]]*\]\{[^}]+\}',  # \usepackage[options]{...}
        r'\\begin\{document\}',       # \begin{document}
        r'\\end\{document\}',         # \end{document}
    ]
    
    # Apply each pattern to clean the text
    cleaned_text = latex_text
    for pattern in patterns:
        cleaned_text = re.sub(pattern, '', cleaned_text, flags=re.IGNORECASE)
    
    return cleaned_text
    

def get_formula_in_markdown(text: str) -> str:
    """
    Formats a string containing a formula into a standard Markdown block.
    
    Args:
        text (str): The input string, potentially containing a formula.

    Returns:
        str: The formatted string, ready for Markdown rendering.
    """
    # Remove leading/trailing whitespace
    text = text.strip()
    text = _dedupe_escaped_braces(text)
    # Remove stray block delimiters so we can rebuild balanced markers below.
    has_leading_block = text.startswith('$$')
    has_trailing_block = text.endswith('$$')
    if has_leading_block ^ has_trailing_block:
        if '\n' not in text:
            if has_leading_block:
                text = text[2:].lstrip()
            else:
                text = text[:-2].rstrip()
    
    # Check if it's already enclosed in $$
    if text.startswith('$$') and text.endswith('$$'):
        text_new = text[2:-2].strip()
        if not '$' in text_new:
            return f"$$\n{text_new}\n$$"
        else:
            return text

    # Handle \[...\] format, convert to $$...$$
    if text.startswith('\\[') and text.endswith('\\]'):
        inner_content = text[2:-2].strip()
        return f"$$\n{inner_content}\n$$"
        
    # Check if it's enclosed in \[ \]
    if len(re.findall(r'.*\\\[.*\\\].*', text)) > 0:
        return text

    # Handle inline formulas ($...$)
    pattern = r'\$([^$]+)\$'
    matches = re.findall(pattern, text)
    if len(matches) > 0:
        # It's an inline formula, return it as is
        return text  

    # If no LaTeX markdown syntax is present, return directly
    if not has_latex_markdown(text):  
        return text

    # Handle unnecessary LaTeX formatting like \usepackage
    if 'usepackage' in text:
        text = clean_latex_preamble(text)

    if text[0] == '`' and text[-1] == '`':
        text = text[1:-1]

    # Enclose the final text in a $$ block with newlines
    text = f"$$\n{text}\n$$"
    return text 


def clean_text(text: str) -> str:
    """
    Cleans text by removing extra whitespace.
    
    Args:
        text: The original text.
        
    Returns:
        str: The cleaned text.
    """
    if not text:
        return ""
    
    # Remove leading and trailing whitespace
    text = text.strip()
    
    # Replace multiple consecutive whitespace characters with a single space
    if text[:2] == '`$' and text[-2:] == '$`':
        text = text[1:-1]
    
    return text


def layoutjson2md(image: Image.Image, cells: list, text_key: str = 'text', no_page_hf: bool = False) -> str:
    """
    Converts a layout JSON format to Markdown.
    
    In the layout JSON, formulas are LaTeX, tables are HTML, and text is Markdown.
    
    Args:
        image: A PIL Image object.
        cells: A list of dictionaries, each representing a layout cell.
        text_key: The key for the text field in the cell dictionary.
        no_page_header_footer: If True, skips page headers and footers.
        
    Returns:
        str: The text in Markdown format.
    """
    if isinstance(cells, list):
        ensure_section_headers_have_markers(cells, text_key=text_key)

    text_items = []

    for i, cell in enumerate(cells):
        x1, y1, x2, y2 = [int(coord) for coord in cell['bbox']]
        category = cell.get('category', '')
        raw_text = cell.get(text_key, "")

        if category == 'Formula':
            text = unescape_basic_sequences(raw_text, convert_controls=False)
            text = _dedupe_escaped_braces(text)
        else:
            text = unescape_basic_sequences(raw_text, convert_controls=False)
            text = _convert_controls_outside_math(text)
            text = _dedupe_escaped_braces(text)
            text = remove_textual_newlines(category, text)
        
        if no_page_hf and category in ['Page-header', 'Page-footer']:
            continue
        
        if category == 'Picture':
            image_crop = image.crop((x1, y1, x2, y2))
            image_base64 = PILimage_to_base64(image_crop)
            text_items.append(f"![]({image_base64})")
        elif category == 'Formula':
            text_items.append(get_formula_in_markdown(text))
        else:            
            text = clean_text(text)
            text_items.append(f"{text}")

    markdown_text = '\n\n'.join(text_items)
    markdown_text = unescape_basic_sequences(markdown_text, convert_controls=False)
    markdown_text = _convert_controls_outside_math(markdown_text)
    markdown_text = _dedupe_escaped_braces(markdown_text)
    markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
    return markdown_text


def fix_streamlit_formulas(md: str) -> str:
    """
    Fixes the format of formulas in Markdown to ensure they display correctly in Streamlit.
    It adds a newline after the opening $$ and before the closing $$ if they don't already exist.
    
    Args:
        md_text (str): The Markdown text to fix.
        
    Returns:
        str: The fixed Markdown text.
    """
    
    # This inner function will be used by re.sub to perform the replacement
    def replace_formula(match):
        content = match.group(1)
        # If the content already has surrounding newlines, don't add more.
        if content.startswith('\n'):
            content = content[1:]
        if content.endswith('\n'):
            content = content[:-1]
        return f'$$\n{content}\n$$'
    
    # Use regex to find all $$....$$ patterns and replace them using the helper function.
    return re.sub(r'\$\$(.*?)\$\$', replace_formula, md, flags=re.DOTALL)

# 在 dots_ocr/utils/format_transformer.py 中
def layoutjson2md_full_robust(image: Image.Image, cells: list, text_key: str = 'text', no_page_hf: bool = False) -> str:
    """
    转换 layout JSON 到 Markdown。
    - 安全处理无效bbox (修复 right < left 问题)。
    - 将坐标裁剪到图像边界内。
    - 检查并跳过零面积的裁剪区域。
    - 如果类别是 'Text'，移除换行符。
    - 如果类别是 'Section-header'，正确处理空格，保留Markdown语法。
    """
    if not isinstance(cells, list):
        print(f"警告：cells 数据不是一个列表，而是 {type(cells)} 类型。无法处理。")
        return ""

    ensure_section_headers_have_markers(cells, text_key=text_key)

    img_width, img_height = image.size
    text_items = []
    for i, cell in enumerate(cells):
        if not isinstance(cell, dict):
            continue

        category = cell.get('category', 'Text')
        raw_text = cell.get(text_key, "")
        if category == 'Formula':
            text = unescape_basic_sequences(raw_text, convert_controls=False)
            text = _dedupe_escaped_braces(text)
        else:
            text = unescape_basic_sequences(raw_text, convert_controls=False)
            text = _convert_controls_outside_math(text)
            text = _dedupe_escaped_braces(text)
            text = remove_textual_newlines(category, text)
        
        if no_page_hf and category in ['Page-header', 'Page-footer']:
            continue
        
        if category == 'Picture':
            try:
                # --- 坐标清洗与验证层 ---
                coords = [int(coord) for coord in cell['bbox']]
                x1, y1, x2, y2 = coords

                # 1. 修正坐标顺序
                if x1 > x2: x1, x2 = x2, x1
                if y1 > y2: y1, y2 = y2, y1

                # 2. 裁剪坐标到图像边界内
                x1 = max(0, x1)
                y1 = max(0, y1)
                x2 = min(img_width, x2)
                y2 = min(img_height, y2)

                # 3. 检查裁剪区域是否有效
                if x1 >= x2 or y1 >= y2:
                    print(f"警告：跳过一个图片单元格，因为其有效裁剪区域面积为零。原始Bbox: {cell.get('bbox')}")
                    continue
                # --- 清洗结束 ---

                image_crop = image.crop((x1, y1, x2, y2))
                image_base64 = PILimage_to_base64(image_crop)
                text_items.append(f"![]({image_base64})")
                
            except (KeyError, TypeError, ValueError) as e:
                print(f"警告：因 bbox 严重无效，跳过一个图片单元格。错误: {e}. Cell: {cell}")
                continue
        elif category == 'Formula':
            text_items.append(get_formula_in_markdown(text))
        else:
            # clean_text 会处理首尾空格和一些特殊情况
            text = clean_text(text)
            text_items.append(f"{text}")

    markdown_text = '\n\n'.join(text_items)
    markdown_text = unescape_basic_sequences(markdown_text, convert_controls=False)
    markdown_text = _convert_controls_outside_math(markdown_text)
    markdown_text = _dedupe_escaped_braces(markdown_text)
    markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
    return markdown_text


def layoutjson2md_simple_extract(cells: list, text_key: str = 'text', no_page_hf: bool = False) -> str:
    # 它只按顺序提取文本，别的什么都不做。
    if not isinstance(cells, list): return ""
    ensure_section_headers_have_markers(cells, text_key=text_key)
    md_lines = []
    for cell in cells:
        if not isinstance(cell, dict): continue
        if no_page_hf and cell.get('category', '') in ['Page-header', 'Page-footer']: continue
        category = cell.get('category', '')
        raw_text = cell.get(text_key, '')
        if category == 'Formula':
            text = unescape_basic_sequences(raw_text, convert_controls=False)
            text = _dedupe_escaped_braces(text)
        else:
            text = unescape_basic_sequences(raw_text, convert_controls=False)
            text = _convert_controls_outside_math(text)
            text = _dedupe_escaped_braces(text)
            text = remove_textual_newlines(category, text)
        text = text.strip()
        if text: md_lines.append(text)
    markdown_text = "\n\n".join(md_lines)
    markdown_text = unescape_basic_sequences(markdown_text, convert_controls=False)
    markdown_text = _convert_controls_outside_math(markdown_text)
    markdown_text = _dedupe_escaped_braces(markdown_text)
    markdown_text = re.sub(r'\n{3,}', '\n\n', markdown_text)
    return markdown_text

def filter_blocks_by_keywords(cells: list, keywords: list, categories_to_filter: set) -> list:
    """
    Filters out blocks from a list of cells based on a list of keywords and categories.

    Args:
        cells: A list of cell dictionaries.
        keywords: A list of keywords to filter by.
        categories_to_filter: A set of categories to apply the filter to.

    Returns:
        A new list of cells with the filtered blocks removed.
    """
    if not keywords or not categories_to_filter:
        return cells

    filtered_cells = []
    keyword_pattern = re.compile('|'.join(re.escape(kw) for kw in keywords), re.IGNORECASE)
    
    for cell in cells:
        if cell.get('category') in categories_to_filter:
            text = cell.get('text', '')
            if text and keyword_pattern.search(text):
                continue  # Skip this block
        
        filtered_cells.append(cell)
        
    return filtered_cells

SUPERSCRIPT_MAP = {
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
    '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
    '⁻': '-',  # Superscript minus
}
SUPERSCRIPT_CHARS = "".join(SUPERSCRIPT_MAP.keys())
CITATION_PATTERN = re.compile(f'([.,。，])([{SUPERSCRIPT_CHARS},-]+)')

def _replace_citation(match):
    punctuation = match.group(1)
    superscript_part = match.group(2)
    
    # Create a translation table and apply it
    translation_table = str.maketrans(SUPERSCRIPT_MAP)
    normal_digits = superscript_part.translate(translation_table)
    
    # It's possible for a comma to be at the end of the match, e.g. "text.³, "
    # We should strip trailing commas from the part that goes inside <sup>
    processed_digits = normal_digits.rstrip(',')
    trailing_commas = normal_digits[len(processed_digits):]

    return f"{punctuation}<sup>{processed_digits}</sup>{trailing_commas}"

def normalize_superscript_citations(cells: list) -> list:
    """
    Normalizes superscript citations in 'Text' blocks to use <sup> tags.
    """
    if not cells:
        return cells
    
    for cell in cells:
        if cell.get('category') == 'Text':
            original_text = cell.get('text')
            if original_text and any(sup in original_text for sup in SUPERSCRIPT_CHARS):
                cell['text'] = CITATION_PATTERN.sub(_replace_citation, original_text)
    return cells
