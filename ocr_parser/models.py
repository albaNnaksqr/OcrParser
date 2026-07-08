from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PageTask:
    page_idx: int
    original_page_num: int
    prompt_mode: str
    filename: str
    save_dir: str
    bbox: Optional[Tuple[int, int, int, int]] = None
    origin_image_path: Optional[str] = None
    processed_image_path: Optional[str] = None
    origin_size: Optional[Tuple[int, int]] = None
    processed_size: Optional[Tuple[int, int]] = None

    def to_payload(self) -> Dict[str, Any]:
        return {
            "page_idx": self.page_idx,
            "original_page_num": self.original_page_num,
            "prompt_mode": self.prompt_mode,
            "bbox": self.bbox,
            "filename": self.filename,
            "save_dir": self.save_dir,
            "origin_image_path": self.origin_image_path,
            "processed_image_path": self.processed_image_path,
            "origin_size": self.origin_size,
            "processed_size": self.processed_size,
        }


@dataclass
class MarkdownArtifacts:
    combined_md_path: str
    origin_md_path: Optional[str] = None
    layout_pdf_path: Optional[str] = None
    document_json_path: Optional[str] = None


@dataclass
class DocumentResult:
    file_path: str
    filename: str
    page_results: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: Optional[MarkdownArtifacts] = None
