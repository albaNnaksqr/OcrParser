from __future__ import annotations

from typing import Any, Optional

from . import bootstrap as bootstrap_ops
from .config import ParserConfig
from .domain import algorithms as algo
from .domain import metadata as metadata_ops
from .domain import prompts as prompt_ops
from .domain import qr as qr_ops
from .domain import table_reparse as table_algo
from . import lifecycle as lifecycle_ops
from .output import assets as asset_ops
from . import runtime as runtime_ops
from .pipeline.document_parser import parse_file, parse_pdf
from .engines.registry import create_engine
from .pipeline.document_parser import process_single_page
from .infra.console import console_write, set_verbose_mode
from .output import json_writer as output_json
from .output import rendering as rendering_ops


class NonStandardModelOutputError(Exception):
    """Raised when the model returns layout data that is missing required fields."""


class DotsOCRParser:
    NonStandardModelOutputError = NonStandardModelOutputError

    # --- from metadata_ops ---
    SUCCESS_STATUSES = metadata_ops.SUCCESS_STATUSES
    NEW_PARAGRAPH_PATTERN = metadata_ops.NEW_PARAGRAPH_PATTERN
    english_letter_pattern = metadata_ops.english_letter_pattern
    ends_with_english_pattern = metadata_ops.ends_with_english_pattern
    starts_with_chinese_pattern = metadata_ops.starts_with_chinese_pattern
    contains_chinese_pattern = metadata_ops.contains_chinese_pattern
    ends_with_digit_pattern = metadata_ops.ends_with_digit_pattern
    SUMMARY_ANCHOR_PATTERN = metadata_ops.SUMMARY_ANCHOR_PATTERN
    ALLOWED_CATEGORIES_BEFORE_SUMMARY = metadata_ops.ALLOWED_CATEGORIES_BEFORE_SUMMARY
    CODE_BLOCK_PATTERN = metadata_ops.CODE_BLOCK_PATTERN
    INLINE_CODE_PATTERN = metadata_ops.INLINE_CODE_PATTERN
    IMAGE_LINK_PATTERN = metadata_ops.IMAGE_LINK_PATTERN
    MARKDOWN_LINK_PATTERN = metadata_ops.MARKDOWN_LINK_PATTERN
    HTML_TAG_PATTERN = metadata_ops.HTML_TAG_PATTERN
    MULTISPACE_PATTERN = metadata_ops.MULTISPACE_PATTERN
    WORD_TOKEN_PATTERN = metadata_ops.WORD_TOKEN_PATTERN
    CHINESE_CHAR_PATTERN = metadata_ops.CHINESE_CHAR_PATTERN
    PAGE_TAG_PATTERN = metadata_ops.PAGE_TAG_PATTERN
    _is_english_to_chinese_transition = metadata_ops._is_english_to_chinese_transition
    _locate_first_page_summary_anchor = metadata_ops._locate_first_page_summary_anchor
    _trim_first_page_blocks = metadata_ops._trim_first_page_blocks
    _clean_markdown_text = metadata_ops._clean_markdown_text
    _count_words_from_text = metadata_ops._count_words_from_text
    _load_filter_keywords = metadata_ops._load_filter_keywords

    # --- from algo ---
    _perform_intra_page_matching = algo._perform_intra_page_matching
    _merge_adjacent_text_blocks_in_same_page = algo._merge_adjacent_text_blocks_in_same_page
    _should_merge_same_page_text_blocks = algo._should_merge_same_page_text_blocks
    _merge_two_text_cells = algo._merge_two_text_cells
    _is_path_clean_between = algo._is_path_clean_between
    _is_valid_continuation_page = algo._is_valid_continuation_page
    _is_contiguous = algo._is_contiguous
    _is_toc_entry = algo._is_toc_entry
    _perform_cross_page_table_caption_matching = algo._perform_cross_page_table_caption_matching
    _is_sentence_end = algo._is_sentence_end
    _starts_new_paragraph = algo._starts_new_paragraph
    _perform_cross_page_text_merging = algo._perform_cross_page_text_merging
    _is_path_clean_between_texts = algo._is_path_clean_between_texts

    # --- from lifecycle_ops ---
    initialize = lifecycle_ops.initialize
    shutdown = lifecycle_ops.shutdown

    # --- from table_algo ---
    _sanitize_table_reparse_output = table_algo._sanitize_table_reparse_output
    _compress_table_html = table_algo._compress_table_html
    _ensure_table_ocr_pipeline = table_algo._ensure_table_ocr_pipeline
    _predict_table_with_external_model = table_algo._predict_table_with_external_model
    _table_ocr_predict_sync = table_algo._table_ocr_predict_sync
    _extract_string_from_prediction = table_algo._extract_string_from_prediction
    _fallback_save_to_markdown = table_algo._fallback_save_to_markdown
    _fallback_save_to_json = table_algo._fallback_save_to_json
    _extract_json_from_prediction = table_algo._extract_json_from_prediction
    _extract_table_reparse_payload = table_algo._extract_table_reparse_payload
    _is_probable_html = table_algo._is_probable_html
    _maybe_refine_table_blocks = table_algo._maybe_refine_table_blocks

    # --- from runtime_ops ---
    _validate_cells_structure = runtime_ops._validate_cells_structure
    _inference_with_vllm = runtime_ops._inference_with_vllm
    _race_inference_attempts = runtime_ops._race_inference_attempts
    _is_transient_inference_error = runtime_ops._is_transient_inference_error
    _run_inference_with_retries = runtime_ops._run_inference_with_retries
    record_api_error = runtime_ops.record_api_error
    get_runtime_snapshot = runtime_ops.get_runtime_snapshot
    autotune_api_concurrency = runtime_ops.autotune_api_concurrency

    # --- from output_json ---
    _get_document_json_output_path = output_json._get_document_json_output_path
    _register_page_json_payload = output_json._register_page_json_payload
    _flush_document_page_json = output_json._flush_document_page_json
    _save_intermediate_outputs_async = output_json._save_intermediate_outputs_async

    # --- from rendering_ops ---
    _generate_md_for_one_page = rendering_ops._generate_md_for_one_page
    _filter_duplicate_images = rendering_ops._filter_duplicate_images
    _cleanup_unused_images = rendering_ops._cleanup_unused_images
    _combine_layout_images_to_pdf = rendering_ops._combine_layout_images_to_pdf
    _get_unique_md_path = rendering_ops._get_unique_md_path
    _compute_md_word_stats = rendering_ops._compute_md_word_stats

    # --- from prompt_ops ---
    get_prompt = prompt_ops.get_prompt

    # --- from asset_ops ---
    _process_base64_images_with_custom_naming = asset_ops._process_base64_images_with_custom_naming

    # --- from qr_ops ---
    _is_qr_or_barcode = qr_ops._is_qr_or_barcode

    def __init__(self, config: Optional[ParserConfig] = None, **kwargs: Any):
        if config is not None and kwargs:
            raise ValueError("Pass either a ParserConfig or keyword arguments, not both.")

        if config is None:
            config = ParserConfig.from_kwargs(**kwargs)

        self.config = config
        set_verbose_mode(bool(getattr(config, "verbose", False)))
        bootstrap_ops.setup_parser(self, config.to_legacy_kwargs(), self._console_write)
        self.ocr_engine = create_engine(self, self.engine)

    def _console_write(self, message: str, level: str = "info") -> None:
        console_write(message, level=level)

    async def _process_single_page_optimized_streaming(self, page_data: dict):
        return await process_single_page(self, page_data)

    async def parse_pdf(self, input_path, filename, prompt_mode, save_dir, page_progress_callback=None, bbox=None, skip_blank_pages=False):
        return await parse_pdf(
            self,
            input_path,
            filename,
            prompt_mode,
            save_dir,
            page_progress_callback=page_progress_callback,
            bbox=bbox,
            skip_blank_pages=skip_blank_pages,
        )

    async def parse_file(
        self,
        input_path: str,
        output_dir: str = "",
        prompt_mode: str = "prompt_layout_all_en",
        page_progress_callback=None,
        bbox=None,
        skip_blank_pages=False,
        rename_to: Optional[str] = None,
        manifest_input_size_bytes: Optional[int] = None,
        manifest_input_mtime_ns: Optional[int] = None,
        manifest_relative_path: Optional[str] = None,
    ):
        return await parse_file(
            self,
            input_path=input_path,
            output_dir=output_dir,
            prompt_mode=prompt_mode,
            page_progress_callback=page_progress_callback,
            bbox=bbox,
            skip_blank_pages=skip_blank_pages,
            rename_to=rename_to,
            manifest_input_size_bytes=manifest_input_size_bytes,
            manifest_input_mtime_ns=manifest_input_mtime_ns,
            manifest_relative_path=manifest_relative_path,
        )


DotsOCRParserOptimized = DotsOCRParser
