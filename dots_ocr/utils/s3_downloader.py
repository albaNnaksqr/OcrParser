import asyncio
import contextlib
import hashlib
import json
import inspect
import logging
import os
import queue
import re
import threading
import random
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, AsyncIterator, Iterator, Dict, Any

import aiofiles
from aiobotocore.session import get_session
from botocore.config import Config
from botocore.exceptions import ClientError
from tqdm import tqdm

try:
    _DEFAULT_PATH_ROOT = "/" if os.name != "nt" else "C:\\"
    PATH_MAX = os.pathconf(_DEFAULT_PATH_ROOT, "PC_PATH_MAX")
    NAME_MAX = os.pathconf(_DEFAULT_PATH_ROOT, "PC_NAME_MAX")
except (AttributeError, OSError, ValueError):
    PATH_MAX = 4096
    NAME_MAX = 255

# Hard upper bound for prefetched downloads waiting on OCR.
MAX_DOWNLOAD_BACKLOG = 100


@dataclass
class S3ConnectionConfig:
    endpoint: str
    ak: str
    sk: str
    bucket_name: str
    region_name: Optional[str] = None


@dataclass
class S3DownloadSettings:
    folder_prefixes: List[str]
    download_dir_base: Path
    resume: bool = True
    download_gb_limit: int = 0
    allowed_suffixes: Optional[List[str]] = None
    max_single_file_gb: float = 0.0


@dataclass
class S3PerformanceSettings:
    max_workers: int = 64
    max_pool_connections: int = 64
    multipart_threshold_mb: int = 16
    multipart_chunksize_mb: int = 8
    per_object_max_concurrency: int = 8
    stream_chunk_size_kb: int = 1024
    head_workers: int = 16
    head_connect_timeout_sec: int = 3
    head_read_timeout_sec: int = 8
    head_max_attempts: int = 2
    head_batch_size: int = 20000
    enable_clock_skew_correction: bool = False


@dataclass
class OcrSettings:
    output_dir_base: Path
    max_files: int = 0
    max_inflight_downloaded_files: int = 100
    use_s3_key_as_subdir: bool = True
    resume_enabled: bool = True


@dataclass
class S3OcrConfig:
    s3: S3ConnectionConfig
    download: S3DownloadSettings
    perf: S3PerformanceSettings
    ocr: OcrSettings


@dataclass
class S3DownloadJob:
    s3_key: str
    local_path: Path
    size_bytes: int
    downloaded: bool = True


def _parse_size_to_bytes(val) -> int:
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(float(val) * (1024 ** 3))
    if isinstance(val, str):
        s = val.strip().lower().replace(" ", "")
        if not s:
            return 0
        number_str = ""
        unit_str = ""
        for ch in s:
            if ch.isdigit() or ch == ".":
                number_str += ch
            else:
                unit_str += ch
        if not number_str:
            return 0
        try:
            num = float(number_str)
        except ValueError:
            return 0
        unit_str = unit_str or "gb"
        mapping = {
            "b": 1,
            "kb": 1024,
            "k": 1024,
            "mb": 1024 ** 2,
            "m": 1024 ** 2,
            "gb": 1024 ** 3,
            "g": 1024 ** 3,
            "tb": 1024 ** 4,
            "t": 1024 ** 4,
        }
        mul = mapping.get(unit_str, 1024 ** 3)
        return int(num * mul)
    return 0


def _parse_datetime(value: str) -> Optional[datetime]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


_CLOCK_SKEW_OFFSET = timedelta(0)
_ORIG_BOTOCORE_AUTH_GET_CURRENT_DATETIME = None
_ORIG_BOTOCORE_COMPAT_GET_CURRENT_DATETIME = None


def _parse_http_date(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        value = str(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _install_botocore_clock_offset(offset_seconds: float) -> None:
    global _CLOCK_SKEW_OFFSET
    global _ORIG_BOTOCORE_AUTH_GET_CURRENT_DATETIME, _ORIG_BOTOCORE_COMPAT_GET_CURRENT_DATETIME

    _CLOCK_SKEW_OFFSET = timedelta(seconds=float(offset_seconds))

    import botocore.auth as _bc_auth
    import botocore.compat as _bc_compat

    if _ORIG_BOTOCORE_AUTH_GET_CURRENT_DATETIME is None:
        _ORIG_BOTOCORE_AUTH_GET_CURRENT_DATETIME = _bc_auth.get_current_datetime
    if _ORIG_BOTOCORE_COMPAT_GET_CURRENT_DATETIME is None:
        _ORIG_BOTOCORE_COMPAT_GET_CURRENT_DATETIME = _bc_compat.get_current_datetime

    def _get_current_datetime_with_offset(remove_tzinfo: bool = True):
        now = datetime.now(timezone.utc) + _CLOCK_SKEW_OFFSET
        if remove_tzinfo:
            now = now.replace(tzinfo=None)
        return now

    _bc_auth.get_current_datetime = _get_current_datetime_with_offset
    _bc_compat.get_current_datetime = _get_current_datetime_with_offset


async def _estimate_s3_clock_offset_seconds(client, bucket_name: str) -> Optional[float]:
    async def _call_and_extract_date(coro):
        t0 = datetime.now(timezone.utc)
        meta = {}
        try:
            resp = await coro
            if isinstance(resp, dict):
                meta = resp.get("ResponseMetadata") or {}
        except ClientError as exc:
            meta = exc.response.get("ResponseMetadata") or {}
        finally:
            t1 = datetime.now(timezone.utc)

        headers = meta.get("HTTPHeaders") or {}
        date_value = headers.get("date") or headers.get("Date")
        server_dt = _parse_http_date(date_value)
        if server_dt is None:
            return None
        local_mid = t0 + (t1 - t0) / 2
        return (server_dt - local_mid).total_seconds()

    offset = await _call_and_extract_date(client.head_bucket(Bucket=bucket_name))
    if offset is None:
        offset = await _call_and_extract_date(client.list_objects_v2(Bucket=bucket_name, MaxKeys=1))
    return offset


def _log_request_time_skew(exc: Exception, *, key: str, operation: str) -> None:
    if not isinstance(exc, ClientError):
        return
    code = exc.response.get("Error", {}).get("Code", "")
    if code != "RequestTimeTooSkewed":
        return
    server_time = None
    try:
        err = exc.response.get("Error", {}) or {}
        server_time = err.get("ServerTime") or err.get("RequestTime")
        if not server_time:
            headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}
            server_time = headers.get("date") or headers.get("Date")
    except Exception:
        server_time = None

    server_dt = _parse_datetime(server_time) if server_time else None
    if server_dt is None:
        logging.warning("[%s] %s RequestTimeTooSkewed; server time unavailable", key, operation)
        return
    local_dt = datetime.now(timezone.utc)
    skew_sec = (server_dt - local_dt).total_seconds()
    logging.warning(
        "[%s] %s RequestTimeTooSkewed: server_time=%s local_time=%s skew=%+.1fs",
        key,
        operation,
        server_dt.isoformat(),
        local_dt.isoformat(),
        skew_sec,
    )


def _sanitize_path_component(name: str) -> str:
    """
    Sanitize a single path component for safe filesystem usage.
    """
    name = (name or "").strip()
    if not name:
        return "unnamed"
    name = name.replace("/", "_")
    return re.sub(r'[:*?"<>|]', "_", name)


def _sanitize_prefix_dir_name(prefix: str) -> str:
    prefix = (prefix or "").strip() or "bucket_root"
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", prefix)
    if len(cleaned.encode("utf-8", "ignore")) <= max(16, NAME_MAX - 16):
        return cleaned
    return hashlib.md5(prefix.encode("utf-8", "ignore")).hexdigest()


def _safe_local_path(base_dir: Path, key: str) -> Path:
    safe_key = (key or "").replace("..", "").lstrip("/")
    if not safe_key:
        safe_key = "unnamed"
    parts = []
    for raw in safe_key.split("/"):
        cleaned = re.sub(r'[:*?"<>|]', "_", raw.strip()) or "unnamed"
        parts.append(cleaned)
    relative = Path(*parts)
    local_path = base_dir / relative

    try:
        path_bytes = len(str(local_path).encode("utf-8", "ignore"))
    except Exception:
        path_bytes = 0

    def _needs_shorten() -> bool:
        if path_bytes and path_bytes > PATH_MAX - 12:
            return True
        last = parts[-1] if parts else ""
        if len(last.encode("utf-8", "ignore")) > NAME_MAX - 4:
            return True
        return False

    if not _needs_shorten():
        return local_path

    digest = hashlib.md5(safe_key.encode("utf-8", "ignore")).hexdigest()[:8]
    filename = parts[-1] if parts else "file"
    stem, suffix = os.path.splitext(filename)
    suffix_bytes = len(suffix.encode("utf-8", "ignore"))
    max_body_bytes = max(8, NAME_MAX - suffix_bytes - len(digest) - 2)
    encoded_stem = stem.encode("utf-8", "ignore")
    if len(encoded_stem) > max_body_bytes:
        stem = encoded_stem[:max_body_bytes].decode("utf-8", "ignore").rstrip("_- .")
    if not stem:
        stem = "file"
    new_name = f"{stem}_{digest}{suffix}"
    parent = base_dir.joinpath(*parts[:-1]) if len(parts) > 1 else base_dir
    return parent / new_name


def map_s3_key_to_output(cfg: S3OcrConfig, key: str) -> tuple[Path, str]:
    """
    Map an S3 object key to a deterministic output directory and base filename.

    Returns:
        (output_dir, base_filename_without_extension)
    """
    safe_key = (key or "").strip().lstrip("/")
    if not safe_key:
        safe_key = "root"
    parts = safe_key.split("/")
    filename_part = _sanitize_path_component(parts[-1])
    stem, _ext = os.path.splitext(filename_part)
    if not stem:
        stem = "file"

    base_dir = cfg.ocr.output_dir_base

    if cfg.ocr.use_s3_key_as_subdir:
        # Preserve directory hierarchy under output_dir_base
        dir_parts = [_sanitize_path_component(p) for p in parts[:-1] if p]
        if dir_parts:
            output_dir = base_dir.joinpath(*dir_parts)
        else:
            output_dir = base_dir
        return output_dir, stem
    else:
        # Short hashed layout for extremely deep/long keys
        digest = re.sub(r'[^0-9a-f]', "", str(hash(key)))
        if not digest:
            import hashlib

            digest = hashlib.md5(key.encode("utf-8")).hexdigest()
        short_hash = digest[:8]
        output_dir = base_dir / short_hash
        return output_dir, f"{stem}_{short_hash}"


def looks_like_complete_local_file(local_path: Path, expected_size: Optional[int] = None) -> bool:
    """
    Heuristic to decide whether a local download is complete.

    - Must exist and have no side-car .part
    - If expected_size is provided (>=0), size must match
    - Reject zero-size files and files whose tail is all zeros (common after truncate)
    """
    if not local_path.exists():
        return False

    suffix = local_path.suffix
    temp_path = local_path.with_suffix(suffix + ".part") if suffix else local_path.with_name(local_path.name + ".part")
    if temp_path.exists():
        return False

    try:
        size_on_disk = local_path.stat().st_size
    except OSError:
        return False

    if expected_size is not None and expected_size >= 0 and size_on_disk != expected_size:
        return False

    if expected_size == 0:
        return True

    sample_bytes = min(512, size_on_disk)
    if expected_size is not None and expected_size >= 0:
        sample_bytes = min(sample_bytes, expected_size)
    if sample_bytes <= 0:
        return False

    try:
        with local_path.open("rb") as f:
            head = f.read(sample_bytes)
            if size_on_disk > sample_bytes:
                f.seek(max(size_on_disk - sample_bytes, 0))
                tail = f.read(sample_bytes)
            else:
                tail = head
    except OSError:
        return False

    zero_head = (not head) or (not head.strip(b"\x00"))
    zero_tail = (not tail) or (not tail.strip(b"\x00"))
    if zero_tail or (zero_head and size_on_disk <= sample_bytes):
        return False

    return True


def load_s3_ocr_config(path: str) -> S3OcrConfig:
    cfg_path = Path(path).expanduser().resolve()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"S3 config json not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    s3_cfg = data.get("s3_config") or {}
    dl_cfg = data.get("download_settings") or {}
    perf_cfg = dict(data.get("performance") or {})
    if "OFFSET" in data:
        perf_cfg["enable_clock_skew_correction"] = bool(data.get("OFFSET", False))
    ocr_cfg = data.get("ocr_settings") or {}

    conn = S3ConnectionConfig(
        endpoint=s3_cfg["endpoint"],
        ak=s3_cfg["ak"],
        sk=s3_cfg["sk"],
        bucket_name=s3_cfg["bucket_name"],
        region_name=s3_cfg.get("region_name"),
    )

    download_dir_base = Path(dl_cfg.get("download_dir_base", "./s3_downloads")).expanduser().resolve()
    folder_prefixes = dl_cfg.get("folder_prefixes") or []
    if not folder_prefixes:
        raise ValueError("download_settings.folder_prefixes must not be empty in S3 config")

    download = S3DownloadSettings(
        folder_prefixes=folder_prefixes,
        download_dir_base=download_dir_base,
        resume=bool(dl_cfg.get("resume", True)),
        download_gb_limit=int(dl_cfg.get("download_gb_limit", 0)),
        allowed_suffixes=list(dl_cfg.get("allowed_suffixes") or []),
        max_single_file_gb=float(dl_cfg.get("max_single_file_gb", 0)),
    )

    max_workers = int(perf_cfg.get("max_workers", 64))
    max_pool_connections_cfg = perf_cfg.get("max_pool_connections")
    if max_pool_connections_cfg is None:
        # Default to a sane ceiling instead of botocore's default 10, while
        # avoiding "unbounded" pools when callers set max_workers extremely high.
        max_pool_connections = min(64, max(1, max_workers))
    else:
        max_pool_connections = int(max_pool_connections_cfg)

    perf = S3PerformanceSettings(
        max_workers=max_workers,
        max_pool_connections=max_pool_connections,
        multipart_threshold_mb=int(perf_cfg.get("multipart_threshold_mb", 16)),
        multipart_chunksize_mb=int(perf_cfg.get("multipart_chunksize_mb", 8)),
        per_object_max_concurrency=int(perf_cfg.get("per_object_max_concurrency", 5)),
        stream_chunk_size_kb=int(perf_cfg.get("stream_chunk_size_kb", 1024)),
        head_workers=int(perf_cfg.get("head_workers", 16)),
        head_connect_timeout_sec=int(perf_cfg.get("head_connect_timeout_sec", 3)),
        head_read_timeout_sec=int(perf_cfg.get("head_read_timeout_sec", 8)),
        head_max_attempts=int(perf_cfg.get("head_max_attempts", 2)),
        head_batch_size=int(perf_cfg.get("head_batch_size", 20000)),
        enable_clock_skew_correction=bool(perf_cfg.get("enable_clock_skew_correction", False)),
    )

    raw_output_dir_base = ocr_cfg.get("output_dir_base")
    if raw_output_dir_base is not None:
        ocr_output_dir = Path(raw_output_dir_base).expanduser().resolve()
    else:
        # 如果未提供单独的 OCR 输出目录，则默认与下载目录相同；
        # 在 OCR 解析流程中可以根据需要覆盖为 CLI 的 --output_dir。
        ocr_output_dir = download_dir_base

    ocr = OcrSettings(
        output_dir_base=ocr_output_dir,
        max_files=int(ocr_cfg.get("max_files", 0)),
        max_inflight_downloaded_files=int(ocr_cfg.get("max_inflight_downloaded_files", 100)),
        use_s3_key_as_subdir=bool(ocr_cfg.get("use_s3_key_as_subdir", True)),
        resume_enabled=bool(ocr_cfg.get("resume_enabled", True)),
    )

    return S3OcrConfig(s3=conn, download=download, perf=perf, ocr=ocr)


class S3DownloadManager:
    def __init__(
        self,
        cfg: S3OcrConfig,
        *,
        enable_progress_bar: bool = True,
        is_upload_mode_enabled: bool = True,
    ):
        self.cfg = cfg
        self.enable_progress_bar = enable_progress_bar
        # Backward-compatible default: keep the historical "isolated prefix dir" layout unless
        # the caller explicitly opts into mirror mode.
        self.is_upload_mode_enabled = bool(is_upload_mode_enabled)

        self.bytes_downloaded_session = 0
        self.stat = {
            "completed": 0,
            "skipped": 0,
            "cached": 0,
            "failed": 0,
            "not_found": 0,
            "timeout": 0,
            "oversize": 0,
        }

        self._max_pool_connections = max(1, int(getattr(cfg.perf, "max_pool_connections", 0) or 1))
        stream_chunk_kb = int(getattr(cfg.perf, "stream_chunk_size_kb", 0) or 0)
        if stream_chunk_kb <= 0:
            stream_chunk_kb = 1024
        self._stream_chunk_size = max(16 * 1024, stream_chunk_kb * 1024)

        allowed = cfg.download.allowed_suffixes or []
        normalized_suffix_set = set()
        for suffix in allowed:
            s_norm = str(suffix).strip().lower()
            if s_norm:
                normalized_suffix_set.add(s_norm)
        self._allowed_suffix_set = normalized_suffix_set

        limit_gb = cfg.download.download_gb_limit or 0
        self.limit_in_bytes = limit_gb * (1024 ** 3) if isinstance(limit_gb, int) and limit_gb > 0 else 0
        self.max_single_file_bytes = _parse_size_to_bytes(cfg.download.max_single_file_gb)

        self._multipart_threshold = max(1, cfg.perf.multipart_threshold_mb) * 1024 * 1024
        self._multipart_chunksize = max(1, cfg.perf.multipart_chunksize_mb) * 1024 * 1024
        self._per_object_max_concurrency = max(
            1,
            min(int(cfg.perf.per_object_max_concurrency), self._max_pool_connections),
        )

        requested_workers = max(1, int(cfg.perf.max_workers))
        self._consumer_concurrency = max(1, min(requested_workers, self._max_pool_connections))
        self._task_queue_size = max(8, self._consumer_concurrency * 2)

        inflight_limit = cfg.ocr.max_inflight_downloaded_files or 0
        if inflight_limit <= 0:
            inflight_limit = MAX_DOWNLOAD_BACKLOG
        inflight_limit = max(1, min(inflight_limit, MAX_DOWNLOAD_BACKLOG))
        self._output_queue_size = inflight_limit

        self._shutdown_event: Optional[asyncio.Event] = None
        self._pipeline_task: Optional[asyncio.Task] = None
        self._active_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._output_queue: Optional[asyncio.Queue] = None
        self._should_skip_key = None

    def _temp_path_for(self, local_path: Path) -> Path:
        """
        Return a side-car temp path for an in-progress download.
        """
        suffix = local_path.suffix
        if suffix:
            return local_path.with_suffix(suffix + ".part")
        return local_path.with_name(local_path.name + ".part")

    def _is_existing_download_complete(self, local_path: Path, expected_size: int) -> bool:
        return looks_like_complete_local_file(local_path, expected_size)

    def set_skip_callback(self, cb):
        """
        Optional async/sync predicate: (key: str, size: int) -> bool
        Returning True skips download for the given key.
        """
        self._should_skip_key = cb

    def _should_stop(self) -> bool:
        return self._shutdown_event is not None and self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        """
        Signal running download tasks to stop as soon as practical.
        """
        if self._shutdown_event is None or self._shutdown_event.is_set():
            return
        loop = self._active_loop
        loop_thread = self._loop_thread
        if loop is not None and loop_thread is not None and threading.current_thread() is not loop_thread:
            loop.call_soon_threadsafe(self._shutdown_event.set)
        else:
            self._shutdown_event.set()

    def _matches_allowed_suffix(self, key: str) -> bool:
        if not self._allowed_suffix_set:
            return True
        lowered = key.lower()
        filename = lowered.rsplit("/", 1)[-1]
        dot_idx = len(filename)
        while True:
            dot_idx = filename.rfind(".", 0, dot_idx)
            if dot_idx == -1:
                break
            candidate = filename[dot_idx:]
            if candidate in self._allowed_suffix_set:
                return True
            if candidate.startswith(".") and candidate[1:] in self._allowed_suffix_set:
                return True
        for suffix in self._allowed_suffix_set:
            if "." not in suffix and filename.endswith(suffix):
                return True
        return False

    def _get_local_path(self, base_dir: Path, key: str) -> Path:
        return _safe_local_path(base_dir, key)

    def _build_client_kwargs(self) -> Dict[str, Any]:
        return {
            "endpoint_url": self.cfg.s3.endpoint,
            "aws_access_key_id": self.cfg.s3.ak,
            "aws_secret_access_key": self.cfg.s3.sk,
            "region_name": self.cfg.s3.region_name or "us-east-1",
        }

    def _create_progress_bar(self):
        if not self.enable_progress_bar:
            return None
        return tqdm(total=0, unit="B", unit_scale=True, desc="S3 Downloading", dynamic_ncols=True)

    async def _copy_body_to_file(self, body, file_obj) -> None:
        """
        Stream an S3 response body into an aiofiles handle.

        Notes:
        - aiobotocore StreamingBody's default iterator chunk is only 1KB, which is very slow for large files.
          Prefer iter_chunks with a larger chunk size.
        """
        iter_chunks = getattr(body, "iter_chunks", None)
        if callable(iter_chunks):
            async for chunk in iter_chunks(self._stream_chunk_size):
                if chunk:
                    await file_obj.write(chunk)
            return
        while True:
            chunk = await body.read(self._stream_chunk_size)
            if not chunk:
                break
            await file_obj.write(chunk)

    def _normalize_folder_entries(self, base_root: Path) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for raw_entry in self.cfg.download.folder_prefixes:
            text = str(raw_entry or "").strip()
            if not text:
                continue
            clean_value = text.lstrip("/\\")
            is_dir = text.endswith("/")
            if is_dir:
                if self.is_upload_mode_enabled:
                    name_seed = clean_value.rstrip("/") or clean_value or "bucket_root"
                    safe_dir_name = _sanitize_prefix_dir_name(name_seed)
                    local_base = base_root / safe_dir_name
                else:
                    # In mirror mode, keep the S3 key's original hierarchy under base_root:
                    # local_path = base_root / <full s3 key>
                    local_base = base_root
                local_base.mkdir(parents=True, exist_ok=True)
            else:
                local_base = base_root
            entries.append(
                {
                    "raw": text,
                    "value": clean_value,
                    "base_dir": local_base,
                    "is_dir": is_dir,
                }
            )
        return entries

    async def _filter_and_queue_task(
        self,
        task_queue: asyncio.Queue,
        *,
        key: str,
        size: int,
        base_dir: Path,
        progress_bar,
    ) -> None:
        if self._should_stop():
            return
        if not self._matches_allowed_suffix(key):
            return
        if self.max_single_file_bytes and size > self.max_single_file_bytes:
            self.stat["oversize"] += 1
            return

        local_path = self._get_local_path(base_dir, key)
        already_complete = False
        if self.cfg.download.resume and local_path.exists():
            already_complete = self._is_existing_download_complete(local_path, size)

        if self._should_skip_key:
            try:
                decision = self._should_skip_key(key, size)
                if inspect.isawaitable(decision):
                    decision = await decision
                if decision:
                    self.stat["skipped"] += 1
                    return
            except Exception as exc:
                logging.warning("Skip callback failed for %s: %s", key, exc)

        if progress_bar is not None and size > 0 and not already_complete:
            progress_bar.total += size
            progress_bar.refresh()

        await task_queue.put(
            {
                "Key": key,
                "Size": size,
                "base_dir": str(base_dir),
                "local_path": str(local_path),
                "already_complete": already_complete,
            }
        )

    async def _produce_exact_key_task(
        self,
        client,
        bucket: str,
        key: str,
        base_dir: Path,
        task_queue: asyncio.Queue,
        progress_bar,
    ) -> str:
        if self._should_stop():
            return "stopped"
        try:
            resp = await client.head_object(Bucket=bucket, Key=key)
            size = int(resp.get("ContentLength", 0))
            await self._filter_and_queue_task(
                task_queue,
                key=key,
                size=size,
                base_dir=base_dir,
                progress_bar=progress_bar,
            )
            return "ok"
        except ClientError as e:
            _log_request_time_skew(e, key=key, operation="head_object")
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                logging.warning("S3 object not found: %s", key)
                self.stat["not_found"] += 1
                return "not_found"
            logging.error("HEAD failed for %s: %s", key, e)
            self.stat["failed"] += 1
            return "error"
        except Exception as e:
            logging.error("HEAD exception for %s: %s", key, e)
            self.stat["failed"] += 1
            return "error"

    async def _produce_prefix_tasks(
        self,
        client,
        bucket: str,
        prefix: str,
        base_dir: Path,
        task_queue: asyncio.Queue,
        progress_bar,
    ) -> None:
        paginator = client.get_paginator("list_objects_v2")
        try:
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix or ""):
                if self._should_stop():
                    break
                for obj in page.get("Contents", []):
                    if self._should_stop():
                        break
                    key = obj.get("Key")
                    if not key or key.endswith("/"):
                        continue
                    size = int(obj.get("Size", 0))
                    await self._filter_and_queue_task(
                        task_queue,
                        key=key,
                        size=size,
                        base_dir=base_dir,
                        progress_bar=progress_bar,
                    )
        except ClientError as exc:
            _log_request_time_skew(exc, key=prefix or "bucket", operation="list_objects_v2")
            raise

    async def _handle_exact_or_fallback(
        self,
        client,
        bucket: str,
        entry: Dict[str, Any],
        task_queue: asyncio.Queue,
        progress_bar,
    ) -> None:
        status = await self._produce_exact_key_task(
            client, bucket, entry["value"], entry["base_dir"], task_queue, progress_bar
        )
        if status in ("not_found", "error"):
            logging.info("Exact key '%s' not found; falling back to prefix scan.", entry["raw"])
            await self._produce_prefix_tasks(
                client, bucket, entry["value"], entry["base_dir"], task_queue, progress_bar
            )

    async def _download_part(
        self,
        client,
        bucket: str,
        key: str,
        local_path: Path,
        part_num: int,
        start: int,
        end: int,
        *,
        semaphore: Optional[asyncio.Semaphore] = None,
    ) -> bool:
        for attempt in range(1, 6):
            if self._should_stop():
                return False
            try:
                if semaphore is None:
                    resp = await client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end-1}")
                    body = resp.get("Body")
                    if body is None:
                        raise RuntimeError(f"Missing S3 response body for key={key}")
                    async with body:
                        async with aiofiles.open(local_path, "r+b") as f:
                            await f.seek(start)
                            await self._copy_body_to_file(body, f)
                else:
                    async with semaphore:
                        resp = await client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end-1}")
                        body = resp.get("Body")
                        if body is None:
                            raise RuntimeError(f"Missing S3 response body for key={key}")
                        async with body:
                            async with aiofiles.open(local_path, "r+b") as f:
                                await f.seek(start)
                                await self._copy_body_to_file(body, f)
                return True
            except Exception as exc:
                _log_request_time_skew(exc, key=key, operation=f"get_object part {part_num}")
                logging.warning(
                    "[%s] Part %d failed (attempt %d/5): %s",
                    key,
                    part_num,
                    attempt,
                    exc,
                )
                await asyncio.sleep(min(30, 2 ** attempt))
        return False

    async def _download_one(
        self,
        client,
        bucket: str,
        key: str,
        base_dir: Path,
        size: int,
        *,
        local_path: Optional[Path] = None,
    ) -> str:
        local_path = local_path or self._get_local_path(base_dir, key)
        temp_path = self._temp_path_for(local_path)

        if self.cfg.download.resume and self._is_existing_download_complete(local_path, size):
            return "skipped"

        if self.limit_in_bytes > 0 and self.bytes_downloaded_session >= self.limit_in_bytes:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            return "skipped"

        local_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            temp_path.unlink()

        for attempt in range(1, 6):
            if self._should_stop():
                break
            try:
                if size > self._multipart_threshold:
                    async with aiofiles.open(temp_path, "wb") as f:
                        await f.truncate(size)
                    part_tasks = []
                    part_semaphore = asyncio.Semaphore(self._per_object_max_concurrency)
                    for part_idx, start in enumerate(range(0, size, self._multipart_chunksize)):
                        end = min(start + self._multipart_chunksize, size)
                        part_tasks.append(
                            self._download_part(
                                client,
                                bucket,
                                key,
                                temp_path,
                                part_idx + 1,
                                start,
                                end,
                                semaphore=part_semaphore,
                            )
                        )
                    results = await asyncio.gather(*part_tasks)
                    if not all(results):
                        raise RuntimeError("Multipart download failed")
                else:
                    resp = await client.get_object(Bucket=bucket, Key=key)
                    body = resp.get("Body")
                    if body is None:
                        raise RuntimeError(f"Missing S3 response body for key={key}")
                    async with body:
                        async with aiofiles.open(temp_path, "wb") as f:
                            await self._copy_body_to_file(body, f)

                with contextlib.suppress(OSError):
                    local_path.unlink()
                temp_path.replace(local_path)

                self.bytes_downloaded_session += size
                return "downloaded"
            except ClientError as exc:
                _log_request_time_skew(exc, key=key, operation="get_object")
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return "not_found"
                if code in ("SlowDown", "Throttling", "RequestTimeout"):
                    sleep_s = min(60, (2 ** attempt) + (hash(key) & 3))
                    logging.warning("[%s] throttled (%s). Retrying in %.1fs", key, code, sleep_s)
                    await asyncio.sleep(sleep_s)
                    continue
                sleep_s = min(60, (2 ** attempt) + random.random())
                logging.warning("[%s] client error (%s). Retrying in %.1fs: %s", key, code or "unknown", sleep_s, exc)
                await asyncio.sleep(sleep_s)
            except Exception as exc:
                _log_request_time_skew(exc, key=key, operation="get_object")
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    self.stat["timeout"] += 1
                sleep_s = min(60, (2 ** attempt) + random.random())
                logging.warning("[%s] transport error. Retrying in %.1fs: %s", key, sleep_s, exc)
                await asyncio.sleep(sleep_s)

        with contextlib.suppress(OSError):
            temp_path.unlink()
        return "failed"

    async def _consumer_worker(
        self,
        client,
        bucket: str,
        task_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
        progress_bar,
    ) -> None:
        while True:
            task = await task_queue.get()
            if task is None:
                task_queue.task_done()
                break
            key = task["Key"]
            size = int(task.get("Size", 0))
            base_dir = Path(task.get("base_dir", str(self.cfg.download.download_dir_base)))
            local_path_str = task.get("local_path")
            local_path = Path(local_path_str) if local_path_str else None
            already_complete = bool(task.get("already_complete", False))

            if self._should_stop():
                task_queue.task_done()
                continue

            resolved_local_path = local_path or self._get_local_path(base_dir, key)
            status: str
            if already_complete and self.cfg.download.resume and resolved_local_path.exists():
                # Avoid re-downloading when a previous run left a complete file on disk.
                status = "cached"
            else:
                status = await self._download_one(
                    client,
                    bucket,
                    key,
                    base_dir,
                    size,
                    local_path=resolved_local_path,
                )

            if status == "downloaded":
                self.stat["completed"] += 1
                if progress_bar is not None and size > 0:
                    progress_bar.update(size)
                job = S3DownloadJob(
                    s3_key=key,
                    local_path=resolved_local_path,
                    size_bytes=size,
                    downloaded=True,
                )
                await output_queue.put(job)
            elif status == "cached":
                # Local file already exists and looks complete; emit it to downstream OCR.
                self.stat["skipped"] += 1
                self.stat["cached"] += 1
                job = S3DownloadJob(
                    s3_key=key,
                    local_path=resolved_local_path,
                    size_bytes=size,
                    downloaded=False,
                )
                await output_queue.put(job)
            elif status == "skipped":
                self.stat["skipped"] += 1
                # For robustness, if _download_one decided to skip because the file already exists,
                # we still want to send it to OCR; but never emit "skipped" caused by download limits.
                if self.cfg.download.resume and self._is_existing_download_complete(resolved_local_path, size):
                    self.stat["cached"] += 1
                    job = S3DownloadJob(
                        s3_key=key,
                        local_path=resolved_local_path,
                        size_bytes=size,
                        downloaded=False,
                    )
                    await output_queue.put(job)
            elif status == "not_found":
                self.stat["not_found"] += 1
            else:
                self.stat["failed"] += 1

            task_queue.task_done()

    async def _master_producer(
        self,
        client,
        bucket: str,
        base_root: Path,
        task_queue: asyncio.Queue,
        progress_bar,
    ) -> None:
        entries = self._normalize_folder_entries(base_root)
        if not entries:
            logging.warning("S3 download config has no usable prefixes or keys.")
            return
        for entry in entries:
            if self._should_stop():
                break
            if entry["is_dir"]:
                await self._produce_prefix_tasks(
                    client,
                    bucket,
                    entry["value"],
                    entry["base_dir"],
                    task_queue,
                    progress_bar,
                )
            else:
                await self._handle_exact_or_fallback(
                    client,
                    bucket,
                    entry,
                    task_queue,
                    progress_bar,
                )

    async def _run_pipeline(self, output_queue: asyncio.Queue) -> None:
        session = get_session()
        base_root = self.cfg.download.download_dir_base
        base_root.mkdir(parents=True, exist_ok=True)
        bucket = self.cfg.s3.bucket_name
        task_queue: asyncio.Queue = asyncio.Queue(maxsize=self._task_queue_size)
        progress_bar = self._create_progress_bar()

        head_cfg = Config(
            signature_version="s3v4",
            connect_timeout=self.cfg.perf.head_connect_timeout_sec,
            read_timeout=self.cfg.perf.head_read_timeout_sec,
            max_pool_connections=max(2, min(self._max_pool_connections, 64)),
            retries={"max_attempts": self.cfg.perf.head_max_attempts, "mode": "standard"},
        )
        download_cfg = Config(
            signature_version="s3v4",
            connect_timeout=20,
            read_timeout=120,
            tcp_keepalive=True,
            max_pool_connections=self._max_pool_connections,
            retries={"max_attempts": 10, "mode": "adaptive"},
        )

        consumers: List[asyncio.Task] = []
        producer_task: Optional[asyncio.Task] = None
        try:
            async with session.create_client("s3", **self._build_client_kwargs(), config=head_cfg) as head_client, \
                       session.create_client("s3", **self._build_client_kwargs(), config=download_cfg) as download_client:
                if self.cfg.perf.enable_clock_skew_correction:
                    try:
                        offset = await _estimate_s3_clock_offset_seconds(head_client, bucket)
                    except Exception as exc:
                        logging.warning("Clock skew correction failed; using local time. Error=%s", exc)
                        offset = None
                    if offset is None:
                        logging.warning(
                            "Clock skew correction enabled, but no Date header found; using local time."
                        )
                    else:
                        _install_botocore_clock_offset(offset)
                        logging.warning(
                            "Clock skew correction enabled: offset=%+.3fs (server - local)", offset
                        )

                producer_task = asyncio.create_task(
                    self._master_producer(head_client, bucket, base_root, task_queue, progress_bar)
                )
                consumers = [
                    asyncio.create_task(
                        self._consumer_worker(
                            download_client,
                            bucket,
                            task_queue,
                            output_queue,
                            progress_bar,
                        )
                    )
                    for _ in range(self._consumer_concurrency)
                ]

                try:
                    await producer_task
                finally:
                    await task_queue.join()
        finally:
            if producer_task and not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer_task

            for _ in consumers:
                await task_queue.put(None)
            if consumers:
                await asyncio.gather(*consumers, return_exceptions=True)

            if progress_bar is not None:
                progress_bar.close()

            await output_queue.put(None)

    async def stream_downloaded_jobs(self) -> AsyncIterator[S3DownloadJob]:
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - defensive
            raise RuntimeError("stream_downloaded_jobs must be awaited inside an event loop") from exc

        if self._pipeline_task is not None:
            raise RuntimeError("S3DownloadManager pipeline already running")

        self._active_loop = loop
        self._loop_thread = threading.current_thread()
        self._shutdown_event = asyncio.Event()
        output_queue: asyncio.Queue = asyncio.Queue(maxsize=self._output_queue_size)
        self._output_queue = output_queue
        self._pipeline_task = loop.create_task(self._run_pipeline(output_queue))

        try:
            while True:
                job = await output_queue.get()
                if job is None:
                    break
                yield job
        finally:
            self.request_shutdown()
            if self._pipeline_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pipeline_task
            self._pipeline_task = None
            self._active_loop = None
            self._loop_thread = None
            self._output_queue = None
            self._shutdown_event = None

    def iter_downloaded_jobs(self) -> Iterator[S3DownloadJob]:
        """
        Synchronous wrapper for legacy callers. Spawns a helper thread running the async pipeline.
        """
        result_queue: "queue.Queue[Optional[S3DownloadJob]]" = queue.Queue()
        sentinel = object()

        def _run():
            async def _drain():
                try:
                    async for job in self.stream_downloaded_jobs():
                        result_queue.put(job)
                except Exception as exc:  # pragma: no cover - bridge
                    result_queue.put(exc)
                finally:
                    result_queue.put(sentinel)

            asyncio.run(_drain())

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        try:
            while True:
                item = result_queue.get()
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.request_shutdown()
            thread.join(timeout=1)
