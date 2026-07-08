# Official Consistency Matrix

This document defines the acceptance bar for MinerU and PaddleOCR-VL support in
this repository. Official output compatibility has priority over local
post-processing convenience. Efficiency rewrites are allowed only when they do
not change official input/output semantics.

## Decision Rules

- `official`: behavior is directly provided by upstream MinerU/PaddleOCR.
- `equivalent rewrite`: implementation is local, but matches upstream behavior
  for the same inputs and is covered by fixture-based tests.
- `custom`: behavior is not proven upstream-compatible. It must be disabled by
  default or gated behind an explicit experimental option.

## MinerU

| Area | Upstream Basis | Allowed Local Behavior | Current Branch Status |
| --- | --- | --- | --- |
| OpenAI-compatible inference | `vlm-http-client` and `hybrid-http-client` accept `server_url` for remote OpenAI-compatible servers. | Send requests to the official-compatible endpoint without changing prompts or page semantics. | Default path sends one full-page request and does not perform local block inference. |
| Markdown output | `_process_output()` writes `{pdf_file_name}.md` from `make_func(pdf_info, f_make_md_mode, image_dir)`. | Preserve returned official markdown or reproduce `make_func` behavior exactly. | Extracts returned markdown fields; full `make_func` parity still requires upstream fixtures. |
| Native artifacts | Official outputs include `{name}.md`, `{name}_middle.json`, `{name}_model.json`, `{name}_content_list.json`, and `{name}_content_list_v2.json` when enabled. | Save official-equivalent files with matching names and raw JSON payloads. | Page-level official-equivalent artifacts are written when those fields are present. |
| Cross-page handling | Driven by MinerU `pdf_info` to markdown/content-list conversion. | Reuse or exactly port that conversion. | Native default uses plain page join; custom regex merging is not used. |

## PaddleOCR-VL

| Area | Upstream Basis | Allowed Local Behavior | Current Branch Status |
| --- | --- | --- | --- |
| OpenAI-compatible inference | Official Docker pipeline uses `VLRecognition.genai_config.backend: vllm-server` and `server_url: .../v1`. | Call the official-compatible service without replacing its layout/VLM pipeline. | Default path sends one full-page request and does not perform local layout-guided VLM calls. |
| Layout handling | Official config controls `use_layout_detection`, `merge_layout_blocks`, and `markdown_ignore_labels`. | Mirror official config values or consume official service output. | Local skip labels and block prompts are not used in the default path. |
| Markdown extraction | Official response exposes `layoutParsingResults[*].markdown.text`. | Extract that field and join pages as official tools do. | Extracts `layoutParsingResults[*].markdown.text` and joins pages with blank lines. |
| Structured output | Official response includes `layoutParsingResults[*].prunedResult` and markdown image mappings. | Preserve raw response and page-level structured result files. | Raw response is preserved; `layoutParsingResults` is also written as a page artifact. |
| Page concatenation | Official helpers either join markdown parts or delegate to `pipeline.concatenate_markdown_pages`. | Use the official join/concatenation behavior only. | Native default uses plain page join; custom regex merging is not used. |

## Implementation Boundary

The parser may implement high-throughput scheduling, retries, async HTTP calls,
connection reuse, bounded concurrency, and artifact writing. These are
efficiency changes and do not require upstream parity tests when the request and
response payloads remain unchanged.

The parser must not default-enable local block prompts, markdown repair, table
repair, title releveling, label filtering, or cross-page merging unless that
behavior is either copied from upstream or proven equivalent with upstream
fixtures.

## Required Fixes Before Full Official Parity

1. Add fixture tests from real MinerU and PaddleOCR-VL responses.
2. Confirm MinerU response field names from the selected official service mode.
3. Confirm whether PaddleOCR-VL document concatenation should stay as plain
   join or call an official-equivalent `concatenate_markdown_pages` port.
4. Add artifact coverage for markdown images if the selected PaddleOCR-VL
   service returns them inline.
