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


def test_s3_client_dependencies_are_isolated_from_parser_imports() -> None:
    parser_root = Path(__file__).parents[1] / "ocr_parser"
    forbidden = {"aiobotocore", "botocore"}
    violations: list[str] = []

    for path in parser_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".", 1)[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".", 1)[0]]
            else:
                continue
            if forbidden.intersection(names):
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


def test_control_application_is_only_a_composition_root() -> None:
    control_root = Path(__file__).parents[1] / "ocr_platform" / "control"
    app_path = control_root / "app.py"
    app_tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))

    route_handlers = [
        node
        for node in ast.walk(app_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("api_")
    ]

    assert {node.name for node in route_handlers} <= {"api_root", "api_token_auth"}
    assert len(app_path.read_text(encoding="utf-8").splitlines()) < 200
    assert not (control_root / "service.py").exists()


def test_control_domains_do_not_import_legacy_service_facade() -> None:
    domains_root = Path(__file__).parents[1] / "ocr_platform" / "control" / "domains"
    violations: list[str] = []

    for path in domains_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "ocr_platform.control.service" or (
                node.level == 3 and module == "service"
            ):
                violations.append(f"{path.relative_to(domains_root.parent.parent.parent)}:{node.lineno}")

    assert violations == []
