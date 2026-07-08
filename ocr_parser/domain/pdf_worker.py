from __future__ import annotations

import atexit
import contextlib
import math
import os
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional

import fitz
from PIL import Image

from dots_ocr.utils.doc_utils import is_blank_page
from dots_ocr.utils.image_utils import fetch_image

PAGE_IMAGE_SAVE_QUALITY = 92


@dataclass
class _PdfDocCacheEntry:
    doc: fitz.Document
    mtime: float
    refcount: int = 0
    close_when_idle: bool = False
    use_lock: threading.Lock = field(default_factory=threading.Lock)


_PDF_DOC_CACHE: Dict[str, _PdfDocCacheEntry] = {}
_PDF_DOC_CACHE_LOCK = threading.RLock()
_PDF_DOC_LEGACY_LEASES = []
# Maximum number of distinct PDFs to keep open simultaneously per worker process.
# Prevents unbounded file-descriptor and memory growth in long-lived workers.
_PDF_DOC_CACHE_MAX = 4


def _close_pdf_doc(doc: fitz.Document) -> None:
    with contextlib.suppress(Exception):
        if not getattr(doc, "is_closed", False):
            doc.close()


def _evict_idle_pdf_docs_locked() -> None:
    while len(_PDF_DOC_CACHE) > _PDF_DOC_CACHE_MAX:
        for cached_path, entry in list(_PDF_DOC_CACHE.items()):
            if entry.refcount == 0:
                _PDF_DOC_CACHE.pop(cached_path, None)
                _close_pdf_doc(entry.doc)
                break
        else:
            break


@contextmanager
def lease_cached_pdf_document(input_path: str) -> Iterator[Optional[fitz.Document]]:
    try:
        current_mtime = os.path.getmtime(input_path)
    except OSError:
        yield None
        return

    entry: Optional[_PdfDocCacheEntry] = None
    with _PDF_DOC_CACHE_LOCK:
        cached_entry = _PDF_DOC_CACHE.get(input_path)
        if cached_entry is not None:
            if cached_entry.mtime == current_mtime and not getattr(cached_entry.doc, "is_closed", False):
                _PDF_DOC_CACHE.pop(input_path, None)
                _PDF_DOC_CACHE[input_path] = cached_entry
                cached_entry.refcount += 1
                entry = cached_entry
            else:
                _PDF_DOC_CACHE.pop(input_path, None)
                if cached_entry.refcount == 0:
                    _close_pdf_doc(cached_entry.doc)
                else:
                    cached_entry.close_when_idle = True

        if entry is None:
            try:
                doc = fitz.open(input_path)
            except Exception:
                doc = None
            if doc is None:
                entry = None
            else:
                entry = _PdfDocCacheEntry(doc=doc, mtime=current_mtime, refcount=1)
                _PDF_DOC_CACHE[input_path] = entry
                _evict_idle_pdf_docs_locked()

    if entry is None:
        yield None
        return

    try:
        with entry.use_lock:
            yield entry.doc
    finally:
        with _PDF_DOC_CACHE_LOCK:
            entry.refcount = max(0, entry.refcount - 1)
            if entry.close_when_idle:
                _close_pdf_doc(entry.doc)
            _evict_idle_pdf_docs_locked()


def get_cached_pdf_document(input_path: str) -> Optional[fitz.Document]:
    lease = lease_cached_pdf_document(input_path)
    doc = lease.__enter__()
    if doc is None:
        lease.__exit__(None, None, None)
        return None
    _PDF_DOC_LEGACY_LEASES.append(lease)
    return doc


def clear_cached_documents():
    legacy_leases = list(_PDF_DOC_LEGACY_LEASES)
    _PDF_DOC_LEGACY_LEASES.clear()
    for lease in legacy_leases:
        with contextlib.suppress(Exception):
            lease.__exit__(None, None, None)
    with _PDF_DOC_CACHE_LOCK:
        for entry in list(_PDF_DOC_CACHE.values()):
            _close_pdf_doc(entry.doc)
        _PDF_DOC_CACHE.clear()


atexit.register(clear_cached_documents)


def process_pdf_page_worker(task_args):
    (
        input_path,
        page_idx,
        dpi,
        skip_blank_pages,
        white_threshold,
        noise_threshold,
        min_pixels,
        max_pixels,
        tmp_dir,
    ) = task_args

    try:
        pdf_lease = lease_cached_pdf_document(input_path)
        doc = pdf_lease.__enter__()
        if doc is None:
            return {"status": "error", "error": f"Unable to open document: {input_path}", "page_idx": page_idx}
        if page_idx >= doc.page_count:
            return {"status": "error", "error": f"Page index {page_idx} out of bounds.", "page_idx": page_idx}

        page = doc.load_page(page_idx)
        if max_pixels:
            w_pt, h_pt = page.rect.width, page.rect.height
            scale = dpi / 72.0
            est_pixels = (w_pt * scale) * (h_pt * scale)
            if est_pixels > max_pixels:
                shrink = math.sqrt(max_pixels / max(1.0, est_pixels))
                dpi_eff = max(36, int(dpi * shrink))
            else:
                dpi_eff = dpi
        else:
            dpi_eff = dpi

        pix = page.get_pixmap(dpi=dpi_eff)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if skip_blank_pages and is_blank_page(img, white_threshold, noise_threshold):
            return {"status": "skipped_blank", "page_idx": page_idx}

        processed = fetch_image(img, min_pixels=min_pixels, max_pixels=max_pixels)
        os.makedirs(tmp_dir, exist_ok=True)
        orig_path = os.path.join(tmp_dir, f"p{page_idx:06d}_orig.jpg")
        proc_path = os.path.join(tmp_dir, f"p{page_idx:06d}_proc.jpg")
        if img.mode != "RGB":
            img = img.convert("RGB")
        if processed.mode != "RGB":
            processed = processed.convert("RGB")
        origin_size = img.size
        processed_size = processed.size
        img.save(orig_path, "JPEG", quality=PAGE_IMAGE_SAVE_QUALITY)
        processed.save(proc_path, "JPEG", quality=PAGE_IMAGE_SAVE_QUALITY)
        del img, processed
        return {
            "status": "success",
            "page_idx": page_idx,
            "origin_path": orig_path,
            "processed_path": proc_path,
            "origin_size": origin_size,
            "processed_size": processed_size,
        }
    except Exception as exc:
        return {"status": "error", "error": f"{exc}\n{traceback.format_exc()}", "page_idx": page_idx}
    finally:
        if "pdf_lease" in locals():
            pdf_lease.__exit__(None, None, None)
