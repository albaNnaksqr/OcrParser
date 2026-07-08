from __future__ import annotations

import asyncio
import functools
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set

from . import llm_processor, metadata_extractor, utils

MD_CHAR_LIMIT = 6000
DEFAULT_DATASOURCE = "langchao"
DEFAULT_ORIGIN_BASE_URL = "http://example.local:8009/corpus-retriev/text"


def _normalize_rel_path(value: Optional[str]) -> str:
    if not value:
        return ""
    return value.replace("\\", "/").strip()


def _safe_getsize(path: Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


@dataclass
class ProcessedIndex:
    checksums: set[str] = field(default_factory=set)
    relpath_to_size: Dict[str, int] = field(default_factory=dict)


@dataclass
class DataIndexJob:
    pdf_path: Path
    md_path: Path
    relative_pdf_path: str
    word_count_actual: int
    word_count_estimated: int
    sampled_pages: int
    total_pages: int
    total_pdf_pages: int
    page_limit: Optional[int] = None
    datasource_id: Optional[str] = None
    origin_base_url: Optional[str] = None
    origin_dataset_name: Optional[str] = None
    cid: str = ""
    bid: str = ""
    pid: str = ""
    rcid: str = ""

    def normalized_relative_path(self) -> str:
        rel = _normalize_rel_path(self.relative_pdf_path)
        if rel:
            return rel
        return self.pdf_path.name


class DataIndexRunner:
    """
    Coordinates metadata extraction + LLM calls and appends JSONL entries.
    """

    def __init__(
        self,
        output_path: Path,
        concurrency: int = 2,
        force_reprocess: bool = False,
        thread_workers: Optional[int] = None,
        datasource_id: Optional[str] = None,
        origin_base_url: Optional[str] = None,
        origin_dataset_name: Optional[str] = None,
    ):
        self.output_path = Path(output_path).expanduser()
        utils.ensure_parent_dir(self.output_path)
        self.force_reprocess = force_reprocess
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._write_lock = asyncio.Lock()
        self._processed = self._load_processed_index()
        self.success_count = 0
        self.failure_count = 0
        self.skipped_count = 0
        self.missing_pdf_count = 0
        max_workers = thread_workers or max(4, concurrency * 2)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="data-index-meta",
        )
        self._pending_tasks: Set[asyncio.Task] = set()
        self._datasource_id = datasource_id or DEFAULT_DATASOURCE
        self._origin_base_url = (origin_base_url or "").strip()
        self._origin_dataset_name = (origin_dataset_name or "").strip().strip("/\\")
        self._batch_size = 10
        self._pending_payloads: list[str] = []

    async def _run_in_executor(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        bound = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(self._executor, bound)

    def _load_processed_index(self) -> ProcessedIndex:
        idx = ProcessedIndex()
        if not self.output_path.exists():
            return idx
        try:
            with self.output_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    metadata = record.get("index", {}).get("metadata") or {}
                    rel = _normalize_rel_path(metadata.get("relative_path"))
                    file_size = metadata.get("file_size")
                    checksum = metadata.get("checksum_md5")
                    if rel and isinstance(file_size, int):
                        idx.relpath_to_size[rel] = file_size
                    if checksum:
                        idx.checksums.add(str(checksum))
        except OSError:
            pass
        return idx

    def _register_record(self, metadata: Dict[str, Any]) -> None:
        checksum = metadata.get("checksum_md5")
        if checksum:
            self._processed.checksums.add(str(checksum))
        rel = _normalize_rel_path(metadata.get("relative_path"))
        size = metadata.get("file_size")
        if rel and isinstance(size, int):
            self._processed.relpath_to_size[rel] = size

    def _should_skip_by_relpath(self, job: DataIndexJob) -> bool:
        if self.force_reprocess:
            return False
        rel = job.normalized_relative_path()
        file_size = _safe_getsize(job.pdf_path)
        recorded_size = self._processed.relpath_to_size.get(rel) if rel else None
        # When the source PDF is missing (common in S3 pipelines that delete downloads),
        # fall back to "relative_path already recorded" to avoid producing duplicate JSONL rows.
        if rel and recorded_size is not None:
            if file_size < 0:
                return True
            if recorded_size == file_size:
                return True
        return False

    async def _append_record(self, record: Dict[str, Any]) -> None:
        payload = json.dumps(record, ensure_ascii=False)
        async with self._write_lock:
            self._pending_payloads.append(payload)
            if len(self._pending_payloads) >= self._batch_size:
                await self._flush_pending_batch_locked()

    async def _flush_pending_batch_locked(self) -> None:
        if not self._pending_payloads:
            return
        payloads = self._pending_payloads
        self._pending_payloads = []
        await self._run_in_executor(self._write_lines, payloads)

    async def _flush_pending_batch(self) -> None:
        async with self._write_lock:
            await self._flush_pending_batch_locked()

    def _write_lines(self, payloads: list[str]) -> None:
        if not payloads:
            return
        with self.output_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(payloads) + "\n")

    def needs_processing_for(self, relative_path: Optional[str], pdf_path: Path) -> bool:
        if self.force_reprocess:
            return True
        rel = _normalize_rel_path(relative_path)
        if not rel:
            rel = pdf_path.name
        if not rel:
            return True
        recorded_size = self._processed.relpath_to_size.get(rel)
        if recorded_size is None:
            return True
        file_size = _safe_getsize(pdf_path)
        # If the file is gone but we already have a record for this relative_path,
        # treat it as processed for resume purposes.
        if file_size < 0:
            return False
        return recorded_size != file_size

    def _compose_origin_path(self, job: DataIndexJob, relative_path: str) -> str:
        rel = relative_path.strip().lstrip("/\\")
        if not rel:
            rel = job.pdf_path.name
        dataset = (job.origin_dataset_name or self._origin_dataset_name or "").strip().strip("/\\")
        if not dataset:
            dataset = job.pdf_path.parent.name if job.pdf_path.parent.name else ""
        base_url = (job.origin_base_url or self._origin_base_url or DEFAULT_ORIGIN_BASE_URL).strip()
        if not base_url:
            return str(job.pdf_path)
        base_url = base_url.rstrip("/")
        if dataset:
            return f"{base_url}/{dataset}/{rel}"
        return f"{base_url}/{rel}"

    def submit_job(self, job: DataIndexJob) -> asyncio.Task:
        """
        Schedule a job to run asynchronously without blocking the caller.
        """
        task = asyncio.create_task(self._run_job_guarded(job))
        self._pending_tasks.add(task)
        task.add_done_callback(lambda t: self._pending_tasks.discard(t))
        return task

    async def _run_job_guarded(self, job: DataIndexJob) -> Optional[Dict[str, Any]]:
        try:
            return await self.run_job(job)
        except Exception as exc:
            print(f"[DataIndexRunner] Job failed for {job.pdf_path}: {exc}")
            raise

    async def run_job(self, job: DataIndexJob) -> Optional[Dict[str, Any]]:
        if not job.md_path.exists():
            raise FileNotFoundError(f"Markdown not found: {job.md_path}")
        if self._should_skip_by_relpath(job):
            self.skipped_count += 1
            return None

        async with self._sem:
            try:
                if not job.pdf_path.exists():
                    self.missing_pdf_count += 1
                static_metadata = await self._run_in_executor(
                    metadata_extractor.extract_all_metadata,
                    job.pdf_path,
                    job.md_path,
                )
                checksum = static_metadata.get("checksum_md5")
                if (
                    not self.force_reprocess
                    and checksum
                    and str(checksum) in self._processed.checksums
                ):
                    self.skipped_count += 1
                    return None

                md_preview = static_metadata.get("md_head")
                if not md_preview:
                    md_preview = await self._run_in_executor(
                        utils.read_text_file, job.md_path, MD_CHAR_LIMIT
                    )
                if not md_preview.strip():
                    raise ValueError("Markdown content is empty after OCR.")

                lang = "zh" if utils.has_min_chinese_chars(md_preview) else "en"
                semantic = await llm_processor.process_text_with_llm(md_preview, lang)

                metadata = dict(static_metadata)
                md_preview_text = metadata.pop("md_head", "") or md_preview
                rel_path = job.normalized_relative_path()
                language_value = metadata.get("language") or lang
                truncated = False
                if job.page_limit and job.total_pdf_pages and job.total_pdf_pages > job.page_limit:
                    truncated = True
                if (
                    job.sampled_pages
                    and job.total_pdf_pages
                    and job.sampled_pages < job.total_pdf_pages
                ):
                    truncated = True
                word_count_value = (
                    job.word_count_estimated if truncated else job.word_count_actual
                )
                if not word_count_value:
                    word_count_value = job.word_count_estimated or job.word_count_actual
                metadata_block = {
                    "modal": "text",
                    "file_extension": metadata.get("file_extension", ""),
                    "file_name": metadata.get("file_name", job.pdf_path.name),
                    "file_size": metadata.get("file_size", 0),
                    "checksum_md5": metadata.get("checksum_md5", ""),
                    "relative_path": rel_path,
                    "page_count": metadata.get("page_count", job.total_pdf_pages),
                    "language": language_value,
                    "title": semantic.get("title", metadata.get("title", "")),
                    "author": semantic.get("author", ""),
                    "publisher": semantic.get("publisher", ""),
                    "public_date": semantic.get("public_date", ""),
                    "isbn": semantic.get("isbn", ""),
                    "doi": semantic.get("doi", ""),
                    "issn": semantic.get("issn", ""),
                    "word_count": word_count_value,
                    "contains_table": metadata.get("contains_table", False),
                    "contains_image": metadata.get("contains_image", False),
                    "contains_equation": metadata.get("contains_equation", False),
                }

                semantic_block = {
                    "abstract": semantic.get("abstract", ""),
                    "domain_1": semantic.get("domain_1", []),
                    "domain_2": semantic.get("domain_2", []),
                    "industry_1": semantic.get("industry_1", []),
                    "industry_2": semantic.get("industry_2", []),
                    "industry_3": semantic.get("industry_3", []),
                    "content_type": semantic.get("content_type", []),
                    "keyword": semantic.get("keyword", []),
                    "embedding": [],
                }
                origin_path_value = self._compose_origin_path(job, rel_path)

                record = {
                    "header": {
                        "cid": job.cid or "",
                        "bid": job.bid or "",
                        "pid": job.pid or "",
                        "rcid": job.rcid or "",
                        "origin_path": origin_path_value,
                        "datasourceID": job.datasource_id or self._datasource_id,
                    },
                    "body": md_preview_text,
                    "index": {
                        "metadata": metadata_block,
                        "semantic": semantic_block,
                    },
                }

                await self._append_record(record)
                self._register_record(metadata_block)
                self.success_count += 1
                return record
            except Exception:
                self.failure_count += 1
                raise

    async def wait_for_pending(self) -> None:
        if not self._pending_tasks:
            return
        tasks = list(self._pending_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        await self.wait_for_pending()
        await self._flush_pending_batch()
        self._executor.shutdown(wait=True)

    def get_stats(self) -> Dict[str, int]:
        return {
            "success": self.success_count,
            "failure": self.failure_count,
            "skipped": self.skipped_count,
            "missing_pdf": self.missing_pdf_count,
        }
