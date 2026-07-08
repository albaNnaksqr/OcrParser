import asyncio
import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

import aiofiles
from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import ClientError


def _default_console(message: str, level: str = "info") -> None:
    print(f"[{level}] {message}")


def _default_temp_dir() -> Path:
    base = Path(tempfile.gettempdir()) / "dotsocr_s3_upload"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _ensure_path(value: Optional[str], fallback: Path) -> Path:
    if not value:
        return fallback
    try:
        path = Path(value).expanduser().resolve()
    except Exception:
        return fallback
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class S3UploadConnection:
    endpoint: str
    ak: str
    sk: str
    bucket_name: str
    target_prefix: str = ""
    region_name: Optional[str] = None
    storage_class: Optional[str] = None


@dataclass
class S3UploadOptions:
    temp_dir: Path
    mirror_mode: str = "input_relative"
    enable_resume: bool = True
    resume_manifest: Optional[Path] = None
    check_remote: bool = True
    include_layout_pdf: bool = True
    include_document_json: bool = True
    include_origin_md: bool = True
    include_page_json: bool = False


@dataclass
class S3UploadPerformance:
    max_workers: int = 16
    max_queue: int = 128
    multipart_threshold_bytes: int = 256 * 1024 * 1024
    multipart_chunk_bytes: int = 64 * 1024 * 1024


@dataclass
class S3UploadRetry:
    max_attempts: int = 5
    backoff_seconds: List[float] = field(default_factory=lambda: [1, 2, 4, 8, 16])


@dataclass
class S3UploadLogging:
    progress_interval_sec: int = 30


@dataclass
class S3UploadConfig:
    connection: S3UploadConnection
    options: S3UploadOptions
    performance: S3UploadPerformance
    retry: S3UploadRetry
    logging: S3UploadLogging


@dataclass
class OCRArtifacts:
    md_path: Optional[str] = None
    origin_md_path: Optional[str] = None
    layout_pdf_path: Optional[str] = None
    document_json_path: Optional[str] = None
    page_json_paths: List[str] = field(default_factory=list)
    images_dir: Optional[str] = None
    extra_files: List[tuple[str, str]] = field(default_factory=list)


@dataclass
class UploadTask:
    local_path: Path
    remote_key: str
    artifact_type: str
    size_bytes: int
    cleanup_after: bool = False
    force: bool = False


def _safe_relative_key(relative_key: Optional[str]) -> str:
    key = (relative_key or "").strip().replace("\\", "/")
    key = "/".join(part for part in key.split("/") if part not in ("", ".", ".."))
    return key or "document"


def _bytes_from_mb(val: Optional[int], default: int) -> int:
    if val is None:
        return default
    try:
        return int(val) * 1024 * 1024
    except (TypeError, ValueError):
        return default


def load_s3_upload_config(path: str) -> S3UploadConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"S3 upload config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    # Backwards compatibility with the legacy uploader schema.
    if "s3" in raw:
        s3_section = raw.get("s3", {})
        upload_section = raw.get("upload", {})
        perf_section = raw.get("upload", {})
    else:
        s3_section = raw.get("s3_config", {})
        upload_section = raw.get("upload_settings", {})
        perf_section = raw.get("performance", raw.get("upload", {}))

    connection = S3UploadConnection(
        endpoint=s3_section.get("endpoint") or s3_section.get("url") or s3_section.get("endpoint_url"),
        ak=s3_section.get("ak") or s3_section.get("access_key") or s3_section.get("access_key_id"),
        sk=s3_section.get("sk") or s3_section.get("secret_key"),
        bucket_name=s3_section.get("bucket") or s3_section.get("bucket_name"),
        target_prefix=(s3_section.get("target_prefix") or upload_section.get("target_prefix") or "").strip("/"),
        region_name=s3_section.get("region"),
        storage_class=s3_section.get("storage_class"),
    )

    if not (connection.endpoint and connection.ak and connection.sk and connection.bucket_name):
        raise ValueError("Incomplete S3 credentials in upload config.")

    temp_dir = _ensure_path(upload_section.get("temp_dir"), _default_temp_dir())
    resume_manifest = upload_section.get("resume_manifest")
    resume_manifest_path = (
        Path(resume_manifest).expanduser().resolve() if resume_manifest else temp_dir / "upload_manifest.jsonl"
    )
    resume_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    options = S3UploadOptions(
        temp_dir=temp_dir,
        mirror_mode=(upload_section.get("mirror_mode") or "input_relative"),
        enable_resume=upload_section.get("enable_resume", upload_section.get("resume", True)),
        resume_manifest=resume_manifest_path,
        check_remote=upload_section.get("check_remote", True),
        include_layout_pdf=upload_section.get("include_layout_pdf", True),
        include_document_json=upload_section.get("include_document_json", True),
        include_origin_md=upload_section.get("include_origin_md", True),
        include_page_json=upload_section.get("include_page_json", False),
    )

    workers_val = int(perf_section.get("max_workers", perf_section.get("upload_workers", 16)))
    queue_val = int(perf_section.get("max_queue", perf_section.get("upload_queue", 256)))
    perf = S3UploadPerformance(
        max_workers=max(1, workers_val),
        max_queue=max(1, queue_val),
        multipart_threshold_bytes=max(8 * 1024 * 1024, _bytes_from_mb(perf_section.get("multipart_threshold_mb"), 256 * 1024 * 1024)),
        multipart_chunk_bytes=max(5 * 1024 * 1024, _bytes_from_mb(perf_section.get("multipart_chunksize_mb"), 64 * 1024 * 1024)),
    )

    retry_section = raw.get("retry", {})
    retry = S3UploadRetry(
        max_attempts=int(retry_section.get("max_attempts", 5)),
        backoff_seconds=list(retry_section.get("backoff_seconds", [1, 2, 4, 8, 16])),
    )

    logging_section = raw.get("logging", {})
    logging_cfg = S3UploadLogging(
        progress_interval_sec=int(logging_section.get("progress_interval_sec", 30)),
    )

    return S3UploadConfig(
        connection=connection,
        options=options,
        performance=perf,
        retry=retry,
        logging=logging_cfg,
    )


class S3UploadManager:
    def __init__(self, cfg: S3UploadConfig, *, log_fn: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self._queue: "asyncio.Queue[Optional[UploadTask]]" = asyncio.Queue(maxsize=max(1, cfg.performance.max_queue))
        self._workers: List[asyncio.Task] = []
        self._session = None
        self._client_cm = None
        self._client = None
        self._started = False
        self._shutdown = False
        self._stats = {"uploaded": 0, "skipped": 0, "failed": 0, "bytes": 0}
        self._manifest_entries: Dict[str, int] = {}
        self._manifest_lock = asyncio.Lock()
        self._log = log_fn or _default_console
        self._head_semaphore = asyncio.Semaphore(32)
        self._on_task_prepared: Optional[Callable[[UploadTask], None]] = None
        self._on_task_done: Optional[Callable[[UploadTask, str], None]] = None

    def set_progress_callbacks(
        self,
        *,
        on_prepared: Optional[Callable[[UploadTask], None]] = None,
        on_done: Optional[Callable[[UploadTask, str], None]] = None,
    ) -> None:
        """
        Optional hooks for progress reporting.
        - on_prepared is called once per UploadTask when it is queued.
        - on_done is called once per UploadTask when it finishes with status in {"uploaded","skipped","failed"}.
        """
        self._on_task_prepared = on_prepared
        self._on_task_done = on_done

    def _emit_prepared(self, task: UploadTask) -> None:
        if not self._on_task_prepared:
            return
        try:
            self._on_task_prepared(task)
        except Exception:
            pass

    def _emit_done(self, task: UploadTask, status: str) -> None:
        if not self._on_task_done:
            return
        try:
            self._on_task_done(task, status)
        except Exception:
            pass

    async def start(self) -> None:
        if self._started:
            return
        loop = asyncio.get_running_loop()
        self.cfg.options.temp_dir.mkdir(parents=True, exist_ok=True)
        if self.cfg.options.resume_manifest:
            manifest_path = self.cfg.options.resume_manifest
            if manifest_path.exists():
                try:
                    async with aiofiles.open(manifest_path, "r", encoding="utf-8") as f:
                        async for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                payload = json.loads(line)
                                self._manifest_entries[payload.get("id")] = int(payload.get("size", 0))
                            except Exception:
                                continue
                except FileNotFoundError:
                    pass

        self._session = get_session()
        conn = self.cfg.connection
        client_params = {
            "endpoint_url": conn.endpoint,
            "aws_access_key_id": conn.ak,
            "aws_secret_access_key": conn.sk,
        }
        if conn.region_name:
            client_params["region_name"] = conn.region_name
        config = Config(
            signature_version="s3v4",
            max_pool_connections=max(32, self.cfg.performance.max_workers * 2),
            retries={"max_attempts": 10, "mode": "adaptive"},
            read_timeout=120,
            connect_timeout=30,
        )
        self._client_cm = self._session.create_client("s3", **client_params, config=config)
        self._client = await self._client_cm.__aenter__()
        self._workers = [asyncio.create_task(self._worker(i), name=f"s3-upload-{i}") for i in range(self.cfg.performance.max_workers)]
        self._started = True
        self._log("S3 upload manager started", level="always")

    def can_check_remote(self) -> bool:
        return bool(self.cfg.options.check_remote and self._client and not self._shutdown and self._started)

    def _build_base_dir(self, relative_key: Optional[str], override_stem: Optional[str] = None) -> tuple[str, str]:
        key = _safe_relative_key(relative_key)
        rel_path = Path(key)
        parent = rel_path.parent.as_posix() if str(rel_path.parent) not in (".", "") else ""
        stem = override_stem or rel_path.stem or "document"
        components = [self.cfg.connection.target_prefix.strip("/")]
        if parent:
            components.append(parent)
        components.append(stem)
        base_dir = "/".join([c for c in components if c])
        return base_dir, stem

    async def artifact_exists(
        self,
        relative_key: Optional[str],
        *,
        override_stem: Optional[str] = None,
        expected_types: Optional[Set[str]] = None,
        use_manifest: bool = True,
    ) -> bool:
        """
        Check whether a previously uploaded bundle already exists.
        If expected_types is provided, all requested artifact categories must be present.
        Remote HEAD checks are authoritative when available; the resume manifest is only
        trusted when remote checks are disabled.
        """
        if not self._started or self._shutdown:
            return False
        base_dir, stem = self._build_base_dir(relative_key, override_stem=override_stem)
        expected: set[str] = set(t.lower() for t in (expected_types or {"markdown"}))
        deterministic_types = {"markdown", "origin_md", "layout_pdf", "document_json"}

        def _manifest_complete() -> bool:
            found: set[str] = set()
            prefix = f"{base_dir}/"
            for manifest_key in self._manifest_entries.keys():
                parts = manifest_key.split("|")
                if len(parts) < 3:
                    continue
                remote_key = parts[0]
                artifact_type = parts[2]
                if not remote_key.startswith(prefix):
                    continue
                if artifact_type == "markdown" and remote_key.endswith(f"{stem}.md"):
                    found.add("markdown")
                elif artifact_type == "origin_md" and remote_key.endswith(f"{stem}_origin.md"):
                    found.add("origin_md")
                elif artifact_type == "layout_pdf" and remote_key.endswith(f"{stem}_layout.pdf"):
                    found.add("layout_pdf")
                elif artifact_type == "document_json" and remote_key.endswith(f"{stem}_pages.json"):
                    found.add("document_json")
                elif artifact_type == "page_json" and "/pages/" in remote_key:
                    found.add("page_json")
                elif artifact_type == "image" and "/images/" in remote_key:
                    found.add("image")
                elif artifact_type == "extra":
                    found.add("extra")
            return expected.issubset(found)

        manifest_complete = use_manifest and _manifest_complete()

        # Remote checks are authoritative when available.
        if self.can_check_remote():
            deterministic_needed = expected & deterministic_types

            async def _head(key: str) -> bool:
                async with self._head_semaphore:
                    try:
                        resp = await self._client.head_object(
                            Bucket=self.cfg.connection.bucket_name,
                            Key=key,
                        )
                        return int(resp.get("ContentLength", -1)) >= 0
                    except ClientError as exc:
                        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                            return False
                    except Exception:
                        return False
                    return False

            for typ in deterministic_needed:
                if typ == "markdown":
                    key_to_check = "/".join(filter(None, [base_dir, f"{stem}.md"]))
                elif typ == "origin_md":
                    key_to_check = "/".join(filter(None, [base_dir, f"{stem}_origin.md"]))
                elif typ == "layout_pdf":
                    key_to_check = "/".join(filter(None, [base_dir, f"{stem}_layout.pdf"]))
                elif typ == "document_json":
                    key_to_check = "/".join(filter(None, [base_dir, f"{stem}_pages.json"]))
                else:
                    continue
                exists = await _head(key_to_check)
                if not exists:
                    return False

            # If expected includes non-deterministic artifacts (images/page_json/extra), only trust manifest when remote checks passed.
            if expected - deterministic_types:
                return manifest_complete
            return True

        # Fallback: rely on manifest only when remote HEAD is unavailable.
        return manifest_complete

    async def enqueue_artifacts(self, relative_key: Optional[str], artifacts: OCRArtifacts, *, force: bool = False) -> None:
        if not self._started or self._shutdown:
            return
        prepared = await self._prepare_tasks(relative_key, artifacts)
        if not prepared:
            return
        for task in prepared:
            self._emit_prepared(task)
        for task in prepared:
            if force:
                task.force = True
                manifest_id = self._manifest_id(task)
                async with self._manifest_lock:
                    self._manifest_entries.pop(manifest_id, None)
            await self._queue.put(task)

    async def flush_and_close(self) -> None:
        if not self._started:
            return
        if self._shutdown:
            return
        self._shutdown = True
        await self._queue.join()
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if self._client_cm is not None:
            await self._client_cm.__aexit__(None, None, None)
            self._client_cm = None
            self._client = None
        self._log(
            f"S3 uploads finished: uploaded={self._stats['uploaded']} skipped={self._stats['skipped']} failed={self._stats['failed']} total_bytes={self._stats['bytes']}",
            level="always",
        )

    def get_stats(self) -> dict:
        """
        Return a shallow copy of current upload stats.
        Keys: uploaded, skipped, failed, bytes.
        """
        return dict(self._stats)

    async def _prepare_tasks(self, relative_key: Optional[str], artifacts: OCRArtifacts) -> List[UploadTask]:
        key = _safe_relative_key(relative_key)
        rel_path = Path(key)
        parent = rel_path.parent.as_posix() if str(rel_path.parent) not in (".", "") else ""
        stem = rel_path.stem or "document"
        components = [self.cfg.connection.target_prefix.strip("/")]
        if parent:
            components.append(parent)
        components.append(stem)
        base_dir = "/".join([c for c in components if c])

        tasks: List[UploadTask] = []

        def _register(local_path: Optional[str], filename: str, artifact_type: str, cleanup: bool = False):
            if not local_path:
                return
            p = Path(local_path)
            if not p.exists() or not p.is_file():
                return
            remote_key = "/".join(filter(None, [base_dir, filename]))
            try:
                size = p.stat().st_size
            except OSError:
                return
            tasks.append(UploadTask(local_path=p, remote_key=remote_key, artifact_type=artifact_type, size_bytes=size, cleanup_after=cleanup))

        _register(artifacts.md_path, f"{stem}.md", "markdown")
        if self.cfg.options.include_origin_md:
            _register(artifacts.origin_md_path, f"{stem}_origin.md", "origin_md")
        if self.cfg.options.include_layout_pdf:
            _register(artifacts.layout_pdf_path, f"{stem}_layout.pdf", "layout_pdf")
        if self.cfg.options.include_document_json:
            _register(artifacts.document_json_path, f"{stem}_pages.json", "document_json")
        if self.cfg.options.include_page_json:
            for page_path in artifacts.page_json_paths or []:
                rel_name = Path(page_path).name
                _register(page_path, f"pages/{rel_name}", "page_json")
        for extra_name, extra_path in artifacts.extra_files or []:
            _register(extra_path, extra_name, "extra")

        if artifacts.images_dir:
            image_dir = Path(artifacts.images_dir)
            if image_dir.exists() and image_dir.is_dir():
                for file_path in image_dir.rglob("*"):
                    if not file_path.is_file():
                        continue
                    rel = file_path.relative_to(image_dir).as_posix()
                    _register(str(file_path), f"images/{rel}", "image")

        return tasks

    async def _worker(self, worker_id: int) -> None:
        while True:
            task = await self._queue.get()
            if task is None:
                self._queue.task_done()
                break
            status = "failed"
            try:
                skip = await self._should_skip(task)
                if skip:
                    status = "skipped"
                    self._stats["skipped"] += 1
                    continue
                success = await self._upload_task(task)
                status = "uploaded" if success else "failed"
                if success:
                    self._stats["uploaded"] += 1
                    self._stats["bytes"] += task.size_bytes
                    await self._record_manifest(task)
                else:
                    self._stats["failed"] += 1
            finally:
                self._emit_done(task, status)
                if task.cleanup_after:
                    with contextlib.suppress(OSError):
                        task.local_path.unlink()
                self._queue.task_done()

    async def _upload_task(self, task: UploadTask) -> bool:
        try:
            if task.size_bytes >= self.cfg.performance.multipart_threshold_bytes:
                return await self._multipart_upload(task)
            return await self._simple_upload(task)
        except Exception as exc:
            self._log(f"Upload failed for {task.remote_key}: {exc}", level="error")
            return False

    async def _simple_upload(self, task: UploadTask) -> bool:
        async with aiofiles.open(task.local_path, "rb") as f:
            data = await f.read()
        extra = {}
        if self.cfg.connection.storage_class:
            extra["StorageClass"] = self.cfg.connection.storage_class
        await self._client.put_object(
            Bucket=self.cfg.connection.bucket_name,
            Key=task.remote_key,
            Body=data,
            **extra,
        )
        return True

    async def _multipart_upload(self, task: UploadTask) -> bool:
        upload_id = None
        extra = {}
        if self.cfg.connection.storage_class:
            extra["StorageClass"] = self.cfg.connection.storage_class
        try:
            resp = await self._client.create_multipart_upload(
                Bucket=self.cfg.connection.bucket_name,
                Key=task.remote_key,
                **extra,
            )
            upload_id = resp["UploadId"]
            parts = []
            part_number = 1
            async with aiofiles.open(task.local_path, "rb") as f:
                while True:
                    chunk = await f.read(self.cfg.performance.multipart_chunk_bytes)
                    if not chunk:
                        break
                    response = await self._client.upload_part(
                        Bucket=self.cfg.connection.bucket_name,
                        Key=task.remote_key,
                        PartNumber=part_number,
                        UploadId=upload_id,
                        Body=chunk,
                    )
                    parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                    part_number += 1
            await self._client.complete_multipart_upload(
                Bucket=self.cfg.connection.bucket_name,
                Key=task.remote_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            return True
        except Exception as exc:
            self._log(f"Multipart upload failed for {task.remote_key}: {exc}", level="error")
            if upload_id:
                with contextlib.suppress(Exception):
                    await self._client.abort_multipart_upload(
                        Bucket=self.cfg.connection.bucket_name,
                        Key=task.remote_key,
                        UploadId=upload_id,
                    )
            return False

    async def _should_skip(self, task: UploadTask) -> bool:
        if task.force or not self.cfg.options.enable_resume:
            return False
        manifest_id = self._manifest_id(task)
        async with self._manifest_lock:
            manifest_seen = manifest_id in self._manifest_entries
        if not self.cfg.options.check_remote:
            # Without remote checks, fall back to manifest only.
            return manifest_seen

        async with self._head_semaphore:
            try:
                resp = await self._client.head_object(
                    Bucket=self.cfg.connection.bucket_name,
                    Key=task.remote_key,
                )
                remote_size = int(resp.get("ContentLength", -1))
                if remote_size == task.size_bytes:
                    async with self._manifest_lock:
                        self._manifest_entries[manifest_id] = task.size_bytes
                    await self._append_manifest(manifest_id, task.size_bytes)
                    return True
                # Remote exists but size mismatch -> fall through to re-upload and refresh manifest entry.
                if remote_size >= 0 and remote_size != task.size_bytes:
                    async with self._manifest_lock:
                        self._manifest_entries.pop(manifest_id, None)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey"}:
                    # Remote missing; invalidate stale manifest entry if any.
                    async with self._manifest_lock:
                        if manifest_seen:
                            self._manifest_entries.pop(manifest_id, None)
                    return False
            except Exception:
                return False
        return False

    async def _record_manifest(self, task: UploadTask) -> None:
        manifest_id = self._manifest_id(task)
        async with self._manifest_lock:
            self._manifest_entries[manifest_id] = task.size_bytes
        await self._append_manifest(manifest_id, task.size_bytes)

    async def _append_manifest(self, manifest_id: str, size: int) -> None:
        if not self.cfg.options.resume_manifest:
            return
        try:
            async with aiofiles.open(self.cfg.options.resume_manifest, "a", encoding="utf-8") as f:
                payload = json.dumps({"id": manifest_id, "size": size, "ts": time.time()}, ensure_ascii=False)
                await f.write(payload + "\n")
        except Exception:
            pass

    @staticmethod
    def _manifest_id(task: UploadTask) -> str:
        return f"{task.remote_key}|{task.size_bytes}|{task.artifact_type}"
