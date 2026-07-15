from __future__ import annotations

import ast
from pathlib import Path

from ocr_parser.contracts import EngineCapabilities, ManifestItem
from ocr_parser.components import (
    DocumentPipeline,
    InferenceRuntime,
    OutputManager,
    ParserRuntime,
    ResumePolicy,
)
from ocr_parser.engines.dotsocr import DotsOCREngine
from ocr_parser.engines.native_openai import NativeOpenAIEngine
from ocr_parser.engines.paddleocr_vl import PaddleOCRVLEngine
from ocr_parser.parser import DotsOCRParser
from ocr_platform.manifest.models import ManifestItem as LegacyManifestItem


def test_parser_does_not_import_platform() -> None:
    parser_root = Path(__file__).parents[1] / "ocr_parser"
    violations: list[str] = []

    for path in parser_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "ocr_platform" or name.startswith("ocr_platform.") for name in names):
                violations.append(f"{path.relative_to(parser_root.parent)}:{node.lineno}")

    assert violations == []


def test_legacy_manifest_import_is_a_contract_reexport() -> None:
    assert LegacyManifestItem is ManifestItem


def test_engine_capabilities_replace_engine_name_checks() -> None:
    assert DotsOCREngine.capabilities == EngineCapabilities(
        uses_shared_postprocess=True,
        emits_native_artifacts=False,
    )
    assert NativeOpenAIEngine(object(), "mineru").capabilities == EngineCapabilities()
    assert PaddleOCRVLEngine.capabilities == EngineCapabilities(
        emits_native_artifacts=True,
        requires_layout_service=True,
    )


def test_parser_passes_a_narrow_context_to_engines() -> None:
    parser = DotsOCRParser(init_process_pool=False, init_md_semaphore=False)

    assert parser.ocr_engine.parser is parser.engine_context
    assert parser.engine_context.config is parser.config
    assert parser.engine_context.runtime is parser.runtime
    assert not hasattr(parser.engine_context, "parse_file")
    assert not hasattr(parser.engine_context, "ocr_engine")


def test_parser_facade_is_composed_without_class_level_method_grafts() -> None:
    parser = DotsOCRParser(init_process_pool=False, init_md_semaphore=False)

    assert isinstance(parser.runtime, ParserRuntime)
    assert isinstance(parser.document_pipeline, DocumentPipeline)
    assert isinstance(parser.inference_runtime, InferenceRuntime)
    assert isinstance(parser.output_manager, OutputManager)
    assert isinstance(parser.resume_policy, ResumePolicy)
    assert "initialize" not in DotsOCRParser.__dict__
    assert "_run_inference_with_retries" not in DotsOCRParser.__dict__
