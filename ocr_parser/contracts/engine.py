from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class EngineCapabilities:
    uses_shared_postprocess: bool = False
    emits_native_artifacts: bool = True
    requires_layout_service: bool = False


@dataclass
class EnginePageResult:
    page_no: int
    original_page_num: int
    status: str
    raw_response: Any = None
    md_content: str = ""
    cells: Optional[List[dict]] = None
    original_cells: Optional[List[dict]] = None
    native_artifacts: List[Dict[str, str]] = field(default_factory=list)
    page_json_path: Optional[str] = None
    page_layout_path: Optional[str] = None
    error: Optional[str] = None

    def to_layout_result(self) -> Dict[str, Any]:
        payload = {
            "page_no": self.page_no,
            "original_page_num": self.original_page_num,
            "status": self.status,
            "native_artifacts": self.native_artifacts,
        }
        if self.cells is not None:
            payload["cells"] = self.cells
        payload["original_cells"] = self.original_cells
        if self.md_content:
            payload["md_content"] = self.md_content
        payload["page_json_path"] = self.page_json_path
        payload["page_layout_path"] = self.page_layout_path
        if self.error:
            payload["error"] = self.error
        return payload


class EngineContext(Protocol):
    """Narrow parser services consumed by engine adapters."""

    config: Any
    runtime: Any
    _console_write: Callable[[str, str], None]
    _run_inference_with_retries: Callable[..., Awaitable[Any]]
    _save_intermediate_outputs_async: Callable[..., Awaitable[Any]]


class OCREngine(Protocol):
    name: str
    capabilities: EngineCapabilities

    async def process_page(self, page_data: Dict[str, Any]) -> EnginePageResult:
        ...

    async def finalize_document(
        self, page_results: List[Dict[str, Any]], save_dir: str, filename: str
    ) -> List[Dict[str, str]]:
        ...
