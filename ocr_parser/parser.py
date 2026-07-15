from __future__ import annotations

from typing import Any, Optional

from .components import (
    DocumentPipeline,
    InferenceRuntime,
    OutputManager,
    ParserRuntime,
    ResumePolicy,
)
from .config import ParserConfig
from .domain import metadata as metadata_ops
from .engine_context import ParserEngineContext
from .engines.registry import create_engine
from .infra.console import console_write, set_verbose_mode


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

    def __init__(self, config: Optional[ParserConfig] = None, **kwargs: Any):
        if config is not None and kwargs:
            raise ValueError("Pass either a ParserConfig or keyword arguments, not both.")

        if config is None:
            config = ParserConfig.from_kwargs(**kwargs)

        self.config = config
        set_verbose_mode(bool(getattr(config, "verbose", False)))
        self.document_pipeline = DocumentPipeline(self)
        self.output_manager = OutputManager(self)
        self.resume_policy = ResumePolicy(config)
        self.runtime = ParserRuntime(self, config)
        self.inference_runtime = InferenceRuntime(self.runtime)
        self.engine_context = ParserEngineContext(self)
        self.ocr_engine = create_engine(self.engine_context, self.engine)

    def __getattr__(self, name: str) -> Any:
        components = (
            self.__dict__.get("runtime"),
            self.__dict__.get("inference_runtime"),
            self.__dict__.get("document_pipeline"),
            self.__dict__.get("output_manager"),
            self.__dict__.get("resume_policy"),
        )
        for component in components:
            if component is None:
                continue
            try:
                return component.resolve(name)
            except AttributeError:
                continue
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    def _console_write(self, message: str, level: str = "info") -> None:
        console_write(message, level=level)

    async def _process_single_page_optimized_streaming(self, page_data: dict):
        return await self.document_pipeline.resolve("process_single_page")(page_data)

    async def parse_pdf(self, input_path, filename, prompt_mode, save_dir, page_progress_callback=None, bbox=None, skip_blank_pages=False):
        return await self.document_pipeline.resolve("parse_pdf")(
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
        return await self.document_pipeline.resolve("parse_file")(
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
