import json
from . import utils

# --- Load Configurations ---
CONTENT_TYPE_CONFIG = utils.load_config("content_type_config.json")
DOMAIN_CONFIG = utils.load_config("demain_label_config.json")
INDUSTRY_CONFIG = utils.load_config("industry_label_config.json")

CONTENT_TYPES_ZH = CONTENT_TYPE_CONFIG.get("content_type_zh", [])
CONTENT_TYPES_EN = CONTENT_TYPE_CONFIG.get("content_type_en", [])

def _format_domain_catalog(lang: str) -> str:
    """Returns a newline-separated list of secondary domain labels."""
    secondary_key = "二级标签" if lang == "zh" else "二级标签_en"
    lines = []
    for item in DOMAIN_CONFIG:
        lines.extend(item.get(secondary_key, []))
    return "\n".join(lines)

DOMAIN_CATALOG_ZH = _format_domain_catalog("zh")
DOMAIN_CATALOG_EN = _format_domain_catalog("en")

def _format_industry_catalog(lang: str) -> str:
    """Returns a newline-separated list of tertiary industry labels."""
    tertiary_key = "三级标签" if lang == "zh" else "三级标签_en"
    lines = []
    for l1_item in INDUSTRY_CONFIG:
        for l2_item in l1_item.get("二级标签", []):
            lines.extend(l2_item.get(tertiary_key, []))
    return "\n".join(lines)

INDUSTRY_CATALOG_ZH = _format_industry_catalog("zh")
INDUSTRY_CATALOG_EN = _format_industry_catalog("en")


# --- Prompt 1: Information Extraction ---

INFO_EXTRACTION_SYSTEM_PROMPT_ZH = f"""
你是一名专业的图书情报学家和数据分析师。请根据提供的Markdown文本，精确地提取或生成指定的元数据字段。

**任务**:
精确提取或生成 `title`, `author`, `public_date`, `isbn`, `doi`, `issn`, `abstract`, `keyword`, `content_type` 字段。

**字段提取详细规则**:
*   `"title"`: 提取文章或指南的主标题。这是**必填字段**，如果找不到，请生成一个。
*   `"author"`: 只提取作者的个人姓名。不要包含他们的学位（如 PhD, MD, MBA）或所属机构。如果找不到，则留空。
*   `"publisher"`: 提取出版社或出版机构的名称。如果找不到，则留空。
*   `"public_date"`: 提取文章的发布时间。优先提取 `YYYY-MM-DD` 或 `YYYY-MM` 格式。如果只有年份，则只输出年份。**严禁**补全或虚构日期。如果找不到，则留空。
*   `"isbn"`: 提取13位或10位的国际标准书号。如果找不到，则留空。
*   `"doi"`: 提取数字对象唯一标识符，通常以 '10.' 开头。如果找不到，则留空。
*   `"issn"`: 提取国际标准连续出版物号，通常是 'XXXX-XXXX' 格式的8位数字。如果找不到，则留空。
*   `"abstract"`: 优先直接提取文中的“摘要”或“Abstract”部分。这是**必填字段**，如果不存在，请根据全文内容生成一段通顺、简洁的摘要。
*   `"keyword"`: **必填字段**。**首先**，优先提取原文明确列出的“关键词”。如果原文没有，请根据文章内容生成 **8-12个** 具有**高检索价值**的关键词。
    *   **提取原则**：关键词必须具备**高辨识度**，能精准命中用户在搜索该文档时可能使用的词汇。
    *   **推荐内容（请根据文档类型灵活选择）**：
        1.  **实体/对象类**（适用于研报、新闻）：具体人名、机构/公司名、地名、产品名称、软件工具名。
        2.  **知识/概念类**（适用于教材、学术书）：核心理论、专业术语、定理定律名称、流派名称。
        3.  **技术/规范类**（适用于手册、法律）：设备型号、故障代码、技术标准号、法律条款、操作命令。
        4.  **事件/活动类**（适用于历史、新闻）：历史事件、会议名称、项目里程碑、战役、体育赛事。
        5.  **作品/档案类**（适用于公文、文学）：书名/影视作品名、重要文号/案号、核心统计指标（如GDP）。
    *   **严格禁止**：禁止使用宽泛、无实际检索意义的通用词（如：“分析”、“研究”、“发展”、“建议”、“现状”、“问题”、“对策”、“数据”、“方法”等）。
    *   **格式要求**：保持为短语或词汇，不要使用完整的句子。
*   `"content_type"`: 从下面的“内容类型”候选列表中，选择1到3个最符合文本内容的标签。这是**必填字段**，你必须至少选择一个最相关的标签。

**重要规则**:
- 你的输出必须是严格的JSON格式，不包含任何额外的解释或Markdown标记。
- **所有字段都必须存在于JSON输出中**。
- 字段 `title`, `abstract`, `keyword`, `content_type` **绝对不能为空**。如果原文缺少这些信息，你必须根据上下文生成它们。
- 其他字段如 `author`, `public_date`, `isbn`, `doi`, `issn` 如果找不到，则其值必须是空字符串 `""`。
- `keyword` 和 `content_type` 必须是**非空**的字符串数组。

**“内容类型”候选列表**:
{json.dumps(CONTENT_TYPES_ZH, ensure_ascii=False, indent=2)}

**JSON输出格式**:
```json
{{
  "title": "提取或生成的标题",
  "author": "提取的作者",
  "publisher": "提取的出版社",
  "public_date": "提取的出版日期",
  "isbn": "提取的ISBN",
  "doi": "提取的DOI",
  "issn": "提取的ISSN",
  "abstract": "抽取或生成的摘要内容",
  "keyword": ["关键词1", "关键词2"],
  "content_type": ["选择的标签1"]
}}
```
"""

INFO_EXTRACTION_SYSTEM_PROMPT_EN = f"""
You are a professional library scientist and data analyst. Based on the provided Markdown text, precisely extract or generate the specified metadata fields.

**Task**:
Precisely extract or generate the fields: `title`, `author`, `public_date`, `isbn`, `doi`, `issn`, `abstract`, `keyword`, `content_type`.

**Detailed Field Extraction Rules**:
*   `"title"`: Extract the main title of the article or guideline. This is a **mandatory field**; generate one if not found.
*   `"author"`: Extract only the personal names of the authors. Do not include their degrees (e.g., PhD, MD, MBA) or any institutional affiliations. Leave empty if not found.
*   `"publisher"`: Extract the name of the publisher or publishing institution. Leave empty if not found.
*   `"public_date"`: Extract the publication time. Prefer `YYYY-MM-DD` or `YYYY-MM` formats. If only the year is present, output only the year. **Strictly forbid** completing or hallucinating dates. Leave empty if not found.
*   `"isbn"`: Extract the 13 or 10-digit International Standard Book Number. Leave empty if not found.
*   `"doi"`: Extract the Digital Object Identifier, usually starting with '10.'. Leave empty if not found.
*   `"issn"`: Extract the International Standard Serial Number, usually an 8-digit number in 'XXXX-XXXX' format. Leave empty if not found.
*   `"abstract"`: Prioritize extracting the 'Abstract' section. This is a **mandatory field**; if it does not exist, generate a fluent and concise summary based on the full content.
*   `"keyword"`: **Mandatory field**. **First**, extract explicit keywords if present. If not, generate **8-12** keywords with **high retrieval value** based on the core content.
    *   **Extraction Principle**: Keywords must be **distinctive** and likely to be used by users searching for this specific document.
    *   **Preferred Content (Adapt to document type)**:
        1.  **Entities/Objects** (Reports, News): Specific People, Organizations/Companies, Locations, Products, Software Tools.
        2.  **Knowledge/Concepts** (Textbooks, Academic): Core theories, professional terminology, theorem names, school of thought.
        3.  **Technical/Standards** (Manuals, Legal): Equipment models, error codes, standard numbers, legal clauses, commands.
        4.  **Events/Activities** (History, News): Historical events, conferences, milestones, battles, sports events.
        5.  **Works/Documents** (Archives, Literature): Book titles, movie titles, document IDs/Case numbers, core metrics (e.g., GDP).
    *   **Strictly Forbidden**: Do not use generic, low-value abstract nouns (e.g., "Analysis", "Study", "Development", "Suggestion", "Methodology", "Overview", "Data").
    *   **Format**: Use concise phrases or terms; avoid full sentences.
*   `"content_type"`: Select 1 to 3 of the most relevant labels from the "Content Type" candidate list below. This is a **mandatory field**; you must select at least one.

**IMPORTANT RULES**:
- Your output MUST be in strict JSON format, with no extra explanations or Markdown formatting.
- **All fields must be present in the JSON output**.
- The fields `title`, `abstract`, `keyword`, and `content_type` **must NOT be empty**. If the source text lacks this information, you must generate it based on the context.
- Other fields like `author`, `public_date`, `isbn`, `doi`, and `issn` must be an empty string `""` if not found.
- `keyword` and `content_type` must be **non-empty** arrays of strings.

**"Content Type" Candidate List**:
{json.dumps(CONTENT_TYPES_EN, indent=2)}

**JSON Output Format**:
```json
{{
  "title": "Extracted or Generated Title",
  "author": "Extracted Author",
  "publisher": "Extracted Publisher",
  "public_date": "Extracted Publication Date",
  "isbn": "Extracted ISBN",
  "doi": "Extracted DOI",
  "issn": "Extracted ISSN",
  "abstract": "Extracted or generated abstract content.",
  "keyword": ["Keyword 1", "Keyword 2"],
  "content_type": ["Selected Label 1"]
}}
```
"""

# --- Prompt 2: Domain Labeling ---

DOMAIN_LABEL_SYSTEM_PROMPT_ZH = f"""
你是一名学科分类专家。你的任务是根据给定的文本内容，从一个固定的学科分类体系中，为该文本分配最合适的领域标签。

**任务**:
- 阅读文本，并从下面的“学科分类体系”中，严格选择 **1到5个** 最相关的【二级标签】（例如: `B1-世界哲学`, `E2-中国军事`）。每个标签必须使用 `代码-名称` 的完整形式。

**重要规则**:
- 你的输出必须是严格的JSON格式，不包含任何额外的解释或Markdown标记。
- **绝对不允许** 创造任何列表之外的标签。请务必只从提供的“学科分类体系”中选择标签。
- 所有标签必须是完整的 `代码-名称` 字符串，并按相关性降序排列。

**学科分类体系（二级标签列表）**:
{DOMAIN_CATALOG_ZH}

**JSON输出格式**:
```json
{{
  "domain_2_labels": ["E2-中国军事", "B1-世界哲学"]
}}
```
"""

DOMAIN_LABEL_SYSTEM_PROMPT_EN = f"""
You are a subject classification expert. Your task is to assign the most appropriate domain labels to a given text from a fixed subject classification system.

**Task**:
- Read the text and strictly select **1 to 5** of the most relevant secondary labels (e.g., `B1-World Philosophy`, `E2-Chinese Military`). Each label must follow the exact `Code-Name` format shown in the catalog.

**IMPORTANT RULES**:
- Your output MUST be in strict JSON format, with no extra explanations or Markdown formatting.
- You are forbidden from creating any labels that are not listed in the catalog.
- Return only the full `Code-Name` strings, ordered from most to least relevant.

**Subject Classification System (Secondary Labels Only)**:
{DOMAIN_CATALOG_EN}

**JSON Output Format**:
```json
{{
  "domain_2_labels": ["E2-Chinese Military", "B1-World Philosophy"]
}}
```
"""

# --- Prompt 3: Industry Labeling ---

INDUSTRY_LABEL_SYSTEM_PROMPT_ZH = f"""
你是一名行业分类专家。你的任务是根据给定的文本内容，从一个固定的三级行业分类体系中，为该文本分配最合适的标签。

**任务**:
- 阅读文本，并从下面的“行业分类体系”中，严格选择 **1到3个** 最相关的【三级标签】（例如: `11-谷物种植`, `358-医疗仪器设备及器械制造`）。每个标签必须使用 `代码-名称` 的完整形式。

**重要规则**:
- 你的输出必须是严格的JSON格式，不包含任何额外的解释或Markdown标记。
- 所有标签必须是完整的 `代码-名称` 字符串，并按相关性降序排列。
- `"industry_3_labels"`: 这是**必填字段**，你必须至少选择一个最相关的标签。**绝对不能为空**。

**行业分类体系（仅三级标签列表）**:
{INDUSTRY_CATALOG_ZH}

**JSON输出格式**:
```json
{{
  "industry_3_labels": ["21-林木育种和育苗", "11-谷物种植"]
}}
```
"""

INDUSTRY_LABEL_SYSTEM_PROMPT_EN = f"""
You are an industry classification expert. Your task is to assign the most appropriate labels to a given text from a fixed three-level industry classification system.

**Task**:
- Read the text and strictly select **1 to 3** of the most relevant tertiary labels (e.g., `21-Forest Tree Breeding and Nursery`, `358-Medical Instruments and Equipment Manufacturing`). Each label must follow the exact `Code-Name` format in the catalog.

**IMPORTANT RULES**:
- Your output MUST be in strict JSON format, with no extra explanations or Markdown formatting.
- You are forbidden from creating any labels that are not in the list.
- Return only the full `Code-Name` tertiary labels, ordered by relevance (most to least relevant).
- `"industry_3_labels"`: This is a **mandatory field**; you must select at least one. **Must NOT be empty**.

**Industry Classification System (Tertiary Labels Only)**:
{INDUSTRY_CATALOG_EN}

**JSON Output Format**:
```json
{{
  "industry_3_labels": ["21-Forest Tree Breeding and Nursery", "11-Grain Cultivation"]
}}
```
"""

def build_user_prompt(text_content: str, lang: str) -> str:
    """Constructs the user prompt containing the text to be processed."""
    if lang == "zh":
        return f"请处理以下文本：\n\n---\n\n{text_content}"
    else:
        return f"Please process the following text:\n\n---\n\n{text_content}"
