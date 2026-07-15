from __future__ import annotations

from types import MethodType
from typing import Any, Iterable

from . import bootstrap as bootstrap_ops
from . import lifecycle as lifecycle_ops
from . import runtime as runtime_ops
from .config import ParserConfig
from .domain import algorithms as algorithm_ops
from .domain import metadata as metadata_ops
from .domain import prompts as prompt_ops
from .domain import qr as qr_ops
from .domain import table_reparse as table_ops
from .output import assets as asset_ops
from .output import json_writer as json_ops
from .output import rendering as rendering_ops
from .pipeline import document_parser as document_ops


class _OperationComponent:
    """Bind a deliberately allow-listed set of legacy operations to one owner."""

    def __init__(self, owner: Any, operations: Iterable[Any]) -> None:
        self._owner = owner
        self._operations = {}
        for operation in operations:
            if isinstance(operation, classmethod):
                operation = operation.__func__
            self._operations[operation.__name__] = operation

    def resolve(self, name: str) -> Any:
        operation = self._operations.get(name)
        if operation is None:
            raise AttributeError(name)
        return MethodType(operation, self._owner)


class ParserRuntime:
    """Own process resources, concurrency controls, metrics, and cancellation state."""

    def __init__(self, facade: Any, config: ParserConfig) -> None:
        self._facade = facade
        bootstrap_ops.configure_runtime(self, config, facade._console_write)
        self._lifecycle = _OperationComponent(self, (lifecycle_ops.initialize, lifecycle_ops.shutdown))

    def resolve(self, name: str) -> Any:
        try:
            return self._lifecycle.resolve(name)
        except AttributeError:
            try:
                return object.__getattribute__(self, name)
            except AttributeError:
                raise AttributeError(name) from None

    def __getattr__(self, name: str) -> Any:
        if name == "_load_filter_keywords":
            return MethodType(metadata_ops._load_filter_keywords, self)
        facade = object.__getattribute__(self, "_facade")
        return getattr(facade, name)


class InferenceRuntime(_OperationComponent):
    """Inference calls, retry behavior, API lanes, and runtime telemetry."""

    def __init__(self, runtime: ParserRuntime) -> None:
        super().__init__(
            runtime,
            (
                runtime_ops._ensure_runtime_counters,
                runtime_ops._ensure_execution_control,
                runtime_ops.apply_execution_control_payload,
                runtime_ops.classify_api_error,
                runtime_ops.record_api_error,
                runtime_ops.get_runtime_snapshot,
                runtime_ops.autotune_api_concurrency,
                runtime_ops.api_lane,
                runtime_ops._validate_cells_structure,
                runtime_ops._inference_with_vllm,
                runtime_ops._race_inference_attempts,
                runtime_ops._is_transient_inference_error,
                runtime_ops._run_inference_with_retries,
            ),
        )


class OutputManager(_OperationComponent):
    """Document output, page sidecars, rendering, and asset lifecycle."""

    def __init__(self, facade: Any) -> None:
        super().__init__(
            facade,
            (
                json_ops._get_document_json_output_path,
                json_ops._register_page_json_payload,
                json_ops._flush_document_page_json,
                json_ops._save_intermediate_outputs_async,
                rendering_ops._generate_md_for_one_page,
                rendering_ops._filter_duplicate_images,
                rendering_ops._cleanup_unused_images,
                rendering_ops._combine_layout_images_to_pdf,
                rendering_ops._get_unique_md_path,
                rendering_ops._compute_md_word_stats,
                asset_ops._process_base64_images_with_custom_naming,
            ),
        )


class ResumePolicy:
    """Single place for resume/force-reprocess policy decisions."""

    def __init__(self, config: ParserConfig) -> None:
        self.enabled = config.enable_resume
        self.force_reprocess = config.force_reprocess

    def may_reuse_existing_output(self) -> bool:
        return self.enabled and not self.force_reprocess

    def resolve(self, name: str) -> Any:
        raise AttributeError(name)


class DocumentPipeline(_OperationComponent):
    """High-level document/page orchestration plus shared OCR domain operations."""

    def __init__(self, facade: Any) -> None:
        super().__init__(
            facade,
            (
                document_ops.parse_file,
                document_ops.parse_pdf,
                document_ops.process_single_page,
                metadata_ops._is_english_to_chinese_transition,
                metadata_ops._locate_first_page_summary_anchor,
                metadata_ops._trim_first_page_blocks,
                metadata_ops._clean_markdown_text,
                metadata_ops._count_words_from_text,
                metadata_ops._load_filter_keywords,
                algorithm_ops._perform_intra_page_matching,
                algorithm_ops._merge_adjacent_text_blocks_in_same_page,
                algorithm_ops._should_merge_same_page_text_blocks,
                algorithm_ops._merge_two_text_cells,
                algorithm_ops._is_path_clean_between,
                algorithm_ops._is_valid_continuation_page,
                algorithm_ops._is_contiguous,
                algorithm_ops._is_toc_entry,
                algorithm_ops._perform_cross_page_table_caption_matching,
                algorithm_ops._is_sentence_end,
                algorithm_ops._starts_new_paragraph,
                algorithm_ops._perform_cross_page_text_merging,
                algorithm_ops._is_path_clean_between_texts,
                table_ops._sanitize_table_reparse_output,
                table_ops._compress_table_html,
                table_ops._ensure_table_ocr_pipeline,
                table_ops._predict_table_with_external_model,
                table_ops._table_ocr_predict_sync,
                table_ops._extract_string_from_prediction,
                table_ops._fallback_save_to_markdown,
                table_ops._fallback_save_to_json,
                table_ops._extract_json_from_prediction,
                table_ops._extract_table_reparse_payload,
                table_ops._is_probable_html,
                table_ops._maybe_refine_table_blocks,
                prompt_ops.get_prompt,
                qr_ops._is_qr_or_barcode,
            ),
        )
