"""Stable contracts shared by the parser and the optional control platform."""

from .document import ArtifactMetadata, DocumentResult, MarkdownArtifacts, PageTask
from .engine import EngineCapabilities, EngineContext, EnginePageResult, OCREngine
from .events import JobEventPayload
from .execution import EngineExecutionTrace, FallbackInfo, StageOutcome
from .failure import FailureCategory
from .manifest import ManifestItem

__all__ = [
    "ArtifactMetadata",
    "DocumentResult",
    "EngineCapabilities",
    "EngineContext",
    "EnginePageResult",
    "EngineExecutionTrace",
    "FallbackInfo",
    "FailureCategory",
    "JobEventPayload",
    "ManifestItem",
    "MarkdownArtifacts",
    "OCREngine",
    "PageTask",
    "StageOutcome",
]
