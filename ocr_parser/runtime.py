from __future__ import annotations

import asyncio
import contextlib
import math
import time
from io import BytesIO
from typing import List, Optional

import httpx


class ResizableAsyncLimiter:
    """Async concurrency limiter whose cap can be adjusted at runtime."""

    def __init__(self, limit: int):
        self._limit = max(1, int(limit))
        self._inflight = 0
        self._waiting = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def waiting(self) -> int:
        return self._waiting

    async def acquire(self) -> None:
        async with self._cond:
            self._waiting += 1
            acquired = False
            try:
                while self._inflight >= self._limit:
                    await self._cond.wait()
                self._inflight += 1
                acquired = True
            finally:
                self._waiting -= 1
                if not acquired:
                    self._cond.notify_all()

    async def release(self) -> None:
        async with self._cond:
            if self._inflight > 0:
                self._inflight -= 1
            self._cond.notify_all()

    async def resize(self, new_limit: int) -> None:
        async with self._cond:
            self._limit = max(1, int(new_limit))
            self._cond.notify_all()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()
        return False


def _ensure_runtime_counters(self) -> None:
    if not hasattr(self, "_api_inflight"):
        self._api_inflight = 0
    if not hasattr(self, "_api_inflight_peak"):
        self._api_inflight_peak = 0
    if not hasattr(self, "_api_waiting"):
        self._api_waiting = 0
    if not hasattr(self, "_api_call_count"):
        self._api_call_count = 0
    if not hasattr(self, "_api_wait_seconds_total"):
        self._api_wait_seconds_total = 0.0
    if not hasattr(self, "_api_latency_seconds_total"):
        self._api_latency_seconds_total = 0.0
    if not hasattr(self, "_api_latency_count"):
        self._api_latency_count = 0
    if not hasattr(self, "_api_error_count"):
        self._api_error_count = 0
    if not hasattr(self, "_api_timeout_count"):
        self._api_timeout_count = 0
    if not hasattr(self, "_api_cancelled_count"):
        self._api_cancelled_count = 0
    if not hasattr(self, "_api_error_categories"):
        self._api_error_categories = {}
    if not hasattr(self, "_api_error_status_codes"):
        self._api_error_status_codes = {}
    if not hasattr(self, "_api_error_types"):
        self._api_error_types = {}
    if not hasattr(self, "_api_error_stages"):
        self._api_error_stages = {}
    if not hasattr(self, "_api_last_error"):
        self._api_last_error = None
    if not hasattr(self, "_api_inflight_started"):
        self._api_inflight_started = {}
    if not hasattr(self, "_api_inflight_seq"):
        self._api_inflight_seq = 0


def _ensure_execution_control(self) -> None:
    if not hasattr(self, "_execution_resume_event"):
        self._execution_resume_event = asyncio.Event()
        self._execution_resume_event.set()
    if not hasattr(self, "_execution_control_paused"):
        self._execution_control_paused = False
    if not hasattr(self, "_execution_control_payload"):
        self._execution_control_payload = {}


async def apply_execution_control_payload(self, payload: dict) -> dict:
    _ensure_execution_control(self)
    changed = False

    paused = bool(payload.get("paused"))
    if bool(getattr(self, "_execution_control_paused", False)) != paused:
        self._execution_control_paused = paused
        changed = True
    if paused:
        self._execution_resume_event.clear()
    else:
        self._execution_resume_event.set()

    requested_limit = payload.get("api_concurrency_limit")
    applied_limit = None
    if requested_limit is not None:
        try:
            applied_limit = max(1, int(requested_limit))
        except (TypeError, ValueError):
            applied_limit = None
    limiter = getattr(self, "api_limiter", None)
    if applied_limit is not None and limiter is not None:
        current_limit = int(getattr(limiter, "limit", applied_limit))
        if current_limit != applied_limit:
            await limiter.resize(applied_limit)
            changed = True

    reason = str(payload.get("reason") or ("paused" if paused else "ready"))
    control_payload = {
        "paused": paused,
        "api_concurrency_limit": applied_limit,
        "reason": reason,
    }
    if getattr(self, "_execution_control_payload", {}) != control_payload:
        changed = True
    self._execution_control_payload = control_payload
    return {
        "changed": changed,
        "paused": paused,
        "api_concurrency_limit": applied_limit,
        "reason": reason,
    }


def _increment_counter(mapping: dict, key: str) -> None:
    mapping[key] = int(mapping.get(key, 0) or 0) + 1


def _error_status_code(error: BaseException) -> int | None:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        return None


def classify_api_error(self, error: BaseException, *, stage: str = "api_call") -> dict:
    error_type = type(error).__name__
    status_code = _error_status_code(error)
    message = str(error).lower()

    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)) or error_type in {
        "APITimeoutError",
        "TimeoutError",
    }:
        category = "timeout"
    elif status_code == 429 or error_type == "RateLimitError" or "rate limit" in message:
        category = "rate_limit"
    elif status_code is not None:
        category = "http_status"
    elif isinstance(error, (httpx.ConnectError, httpx.NetworkError, httpx.TransportError)) or error_type in {
        "APIConnectionError",
        "ConnectError",
        "ConnectionError",
    }:
        category = "network"
    elif stage == "model_output" or error_type == "NonStandardModelOutputError":
        category = "model_output"
    elif error_type in {"APIError", "OpenAIError"}:
        category = "api_error"
    elif "empty response" in message or "malformed layout output" in message:
        category = "model_output"
    else:
        category = "unexpected"

    return {
        "category": category,
        "type": error_type,
        "stage": stage,
        "status_code": status_code,
    }


def record_api_error(self, error: BaseException, *, stage: str = "api_call") -> dict:
    _ensure_runtime_counters(self)
    details = classify_api_error(self, error, stage=stage)
    category = details["category"]
    error_type = details["type"]
    status_code = details["status_code"]

    self._api_error_count += 1
    if category == "timeout":
        self._api_timeout_count += 1
    _increment_counter(self._api_error_categories, category)
    _increment_counter(self._api_error_types, error_type)
    _increment_counter(self._api_error_stages, str(details["stage"]))
    if status_code is not None:
        _increment_counter(self._api_error_status_codes, str(status_code))
    self._api_last_error = details
    return details


def get_runtime_snapshot(self) -> dict:
    _ensure_runtime_counters(self)
    api_limiter = getattr(self, "api_limiter", None)
    api_limit = getattr(api_limiter, "limit", None)
    api_waiting = getattr(api_limiter, "waiting", None)
    now = time.monotonic()
    oldest_api_inflight = (
        now - min(self._api_inflight_started.values())
        if self._api_inflight_started
        else 0.0
    )
    avg_api_latency = (
        self._api_latency_seconds_total / self._api_latency_count
        if self._api_latency_count
        else 0.0
    )
    snapshot = {
        "api_limit": api_limit if api_limit is not None else getattr(self, "api_concurrency", None),
        "api_limit_start": getattr(self, "api_concurrency_start", getattr(self, "api_concurrency", None)),
        "api_limit_max": getattr(self, "api_concurrency_max", getattr(self, "api_concurrency", None)),
        "api_inflight": self._api_inflight,
        "api_inflight_peak": self._api_inflight_peak,
        "api_waiting": api_waiting if api_waiting is not None else self._api_waiting,
        "api_call_count": self._api_call_count,
        "api_wait_seconds_total": self._api_wait_seconds_total,
        "api_avg_latency": avg_api_latency,
        "api_error_count": self._api_error_count,
        "api_timeout_count": self._api_timeout_count,
        "api_cancelled_count": self._api_cancelled_count,
        "api_error_categories": dict(self._api_error_categories),
        "api_error_status_codes": dict(self._api_error_status_codes),
        "api_error_types": dict(self._api_error_types),
        "api_error_stages": dict(self._api_error_stages),
        "api_last_error": dict(self._api_last_error) if self._api_last_error else None,
        "oldest_api_inflight": max(oldest_api_inflight, 0.0),
    }
    two_stage_metrics = getattr(self, "two_stage_metrics", None)
    if isinstance(two_stage_metrics, dict):
        snapshot.update(two_stage_metrics)
    mineru_two_stage_metrics = getattr(self, "mineru_two_stage_metrics", None)
    if isinstance(mineru_two_stage_metrics, dict):
        snapshot.update(
            {
                key: value
                for key, value in mineru_two_stage_metrics.items()
                if not str(key).startswith("_")
            }
        )
    paddleocr_vl_metrics = getattr(self, "paddleocr_vl_metrics", None)
    if isinstance(paddleocr_vl_metrics, dict):
        snapshot.update(
            {
                key: value
                for key, value in paddleocr_vl_metrics.items()
                if not str(key).startswith("_")
            }
        )
    return snapshot


async def autotune_api_concurrency(self) -> dict:
    _ensure_runtime_counters(self)
    limiter = getattr(self, "api_limiter", None)
    if limiter is None or not getattr(self, "enable_api_autotune", False):
        return {"changed": False, "reason": "disabled"}

    snapshot = get_runtime_snapshot(self)
    current_limit = int(snapshot["api_limit"] or 1)
    max_limit = int(snapshot["api_limit_max"] or current_limit)
    error_count = int(snapshot["api_error_count"] or 0)
    timeout_count = int(snapshot["api_timeout_count"] or 0)
    previous_errors = int(getattr(self, "api_autotune_last_error_count", error_count) or 0)
    previous_timeouts = int(getattr(self, "api_autotune_last_timeout_count", timeout_count) or 0)
    new_errors = max(0, error_count - previous_errors)
    new_timeouts = max(0, timeout_count - previous_timeouts)
    self.api_autotune_last_error_count = error_count
    self.api_autotune_last_timeout_count = timeout_count

    if bool(getattr(self, "_execution_control_paused", False)):
        return {
            "changed": False,
            "old_limit": current_limit,
            "new_limit": current_limit,
            "reason": "execution_control_paused",
        }

    new_limit = current_limit
    reason = ""
    if new_timeouts or new_errors:
        new_limit = max(1, math.floor(current_limit * 0.75))
        reason = "errors"
    else:
        saturated = int(snapshot["api_inflight"] or 0) >= max(1, current_limit - 1)
        waiting = int(snapshot["api_waiting"] or 0) > 0
        if (saturated or waiting) and current_limit < max_limit:
            new_limit = min(max_limit, max(current_limit + 8, math.ceil(current_limit * 1.25)))
            reason = "saturated"

    if new_limit == current_limit:
        return {"changed": False, "old_limit": current_limit, "new_limit": current_limit, "reason": reason or "steady"}

    await limiter.resize(new_limit)
    return {"changed": True, "old_limit": current_limit, "new_limit": new_limit, "reason": reason}


@contextlib.asynccontextmanager
async def api_lane(self, lane_kind: str = "primary"):
    _ensure_runtime_counters(self)
    resume_event = getattr(self, "_execution_resume_event", None)
    if resume_event is not None:
        await resume_event.wait()
    api_limiter = getattr(self, "api_limiter", None)
    api_semaphore = getattr(self, "api_semaphore", None)
    limiter = api_limiter if api_limiter is not None else api_semaphore

    wait_start = time.time()
    release_needed = False
    if limiter is not None:
        self._api_waiting += 1
        try:
            await limiter.acquire()
            release_needed = True
        finally:
            if self._api_waiting > 0:
                self._api_waiting -= 1

    self._api_wait_seconds_total += time.time() - wait_start
    self._api_call_count += 1
    self._api_inflight += 1
    self._api_inflight_peak = max(self._api_inflight_peak, self._api_inflight)
    start_time = time.monotonic()
    self._api_inflight_seq += 1
    inflight_token = self._api_inflight_seq
    self._api_inflight_started[inflight_token] = start_time
    try:
        try:
            yield
        except asyncio.CancelledError:
            self._api_cancelled_count += 1
            raise
        except BaseException as exc:
            record_api_error(self, exc, stage=lane_kind or "api_call")
            raise
    finally:
        self._api_latency_seconds_total += time.monotonic() - start_time
        self._api_latency_count += 1
        self._api_inflight_started.pop(inflight_token, None)
        self._api_inflight -= 1
        if release_needed:
            release_result = limiter.release()
            if asyncio.iscoroutine(release_result):
                await release_result


def _validate_cells_structure(self, cells: List[dict]) -> None:
    if not isinstance(cells, list) or not cells:
        raise self.NonStandardModelOutputError("Model response did not produce any layout cells.")
    for idx, cell in enumerate(cells):
        if not isinstance(cell, dict):
            raise self.NonStandardModelOutputError(f"Cell #{idx} is not a dictionary.")
        if "category" not in cell:
            raise self.NonStandardModelOutputError(f"Cell #{idx} is missing the 'category' field.")
        if not cell.get("category"):
            raise self.NonStandardModelOutputError(f"Cell #{idx} has an empty 'category' value.")
        if "bbox" not in cell:
            raise self.NonStandardModelOutputError(f"Cell #{idx} is missing the 'bbox' field.")
        bbox_val = cell.get("bbox")
        if not isinstance(bbox_val, (list, tuple)) or len(bbox_val) != 4:
            raise self.NonStandardModelOutputError(f"Cell #{idx} has an invalid 'bbox' value.")


async def _inference_with_vllm(self, image, prompt):
    start_time = time.time()
    try:
        from dots_ocr.model import inference_async as inference_module

        payload = image
        try:
            encode_semaphore = getattr(self, "encode_semaphore", None)
            if encode_semaphore is None:
                payload, _ = await inference_module.prepare_image_payload_for_vllm(image)
            else:
                async with encode_semaphore:
                    payload, _ = await inference_module.prepare_image_payload_for_vllm(image)
        except Exception:
            payload = image

        async def _call_once():
            return await inference_module.inference_with_vllm(
                payload,
                prompt,
                model_name=self.model_name,
                ip=self.ip,
                port=self.port,
                temperature=self.temperature,
                top_p=self.top_p,
                max_completion_tokens=self.max_completion_tokens,
                timeout=self.timeout,
                client=self.client,
                max_retries=0,
                retry_delay=0.0,
            )

        async with api_lane(self):
            result = await _call_once()
        if result is not None:
            self.monitor.record_inference_time(time.time() - start_time)
            return result
        raise ValueError("Inference call to vLLM returned None.")
    except Exception as exc:
        self.monitor.record_error(type(exc).__name__)
        raise


async def _race_inference_attempts(self, image, prompt, num_attempts: int):
    if num_attempts <= 0:
        return None
    payload = image
    try:
        if isinstance(image, (bytes, bytearray, str)):
            payload = image
        else:
            bio = BytesIO()
            (image.convert("RGB") if getattr(image, "mode", None) != "RGB" else image).save(bio, "JPEG", quality=92)
            payload = bio.getvalue()
            del bio
    except Exception as exc:
        self._console_write(f"[race] Failed to pre-encode image once, fallback to original object. Reason: {exc}", level="warning")

    tasks = [asyncio.create_task(self._inference_with_vllm(payload, prompt)) for _ in range(num_attempts)]
    last_exception = None
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    res = task.result()
                    if res is not None:
                        for p in pending:
                            p.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        return res
                except Exception as exc:
                    last_exception = exc
                    continue
        if last_exception:
            raise last_exception
        raise Exception("All concurrent retry attempts failed without a specific exception.")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _is_transient_inference_error(self, error: Exception) -> bool:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True
    if isinstance(error, httpx.HTTPError):
        return True
    if type(error).__name__ in {
        "APIError",
        "RateLimitError",
        "ServiceUnavailableError",
        "GatewayTimeoutError",
        "OverloadedError",
    }:
        return True
    message = str(error).lower()
    return any(
        keyword in message
        for keyword in [
            "timeout",
            "temporarily",
            "try again",
            "connection",
            "rate limit",
            "overloaded",
            "unavailable",
            "backlog",
            "empty response",
            "server busy",
        ]
    )


async def _run_inference_with_retries(
    self,
    payload,
    prompt: str,
    page_num: int,
    *,
    use_race: bool = True,
    max_attempts: Optional[int] = None,
    race_attempts: Optional[int] = None,
):
    from .infra.circuit_breaker import CircuitOpenError

    circuit_breaker = getattr(self, "circuit_breaker", None)

    # Fast-fail immediately if circuit is open (no retries burned).
    if circuit_breaker:
        await circuit_breaker.before_call()

    attempt = 0
    delay_base = self.retry_delay if self.retry_delay > 0 else 1.0
    attempt_limit = max_attempts if max_attempts is not None else (self.max_retries or 1)
    attempt_limit = max(attempt_limit, 1)
    concurrent_attempts = race_attempts if race_attempts is not None else self.concurrent_retries

    _call_succeeded = False
    _cancelled = False
    try:
        while True:
            attempt += 1
            try:
                if use_race and concurrent_attempts > 1:
                    result = await self._race_inference_attempts(payload, prompt, concurrent_attempts)
                else:
                    result = await self._inference_with_vllm(payload, prompt)
                if result is None:
                    raise ValueError("Inference returned an empty response.")
                if attempt > 1:
                    self.monitor.record_retry(attempt - 1)
                _call_succeeded = True
                return result, attempt
            except asyncio.CancelledError:
                _cancelled = True
                raise
            except CircuitOpenError:
                raise
            except Exception as exc:
                if attempt >= attempt_limit or not self._is_transient_inference_error(exc):
                    raise
                if attempt >= 100 and attempt % 100 == 0:
                    self._console_write(
                        f"Page {page_num} inference still failing after {attempt} retries. Last error: {type(exc).__name__}: {exc}",
                        level="error",
                    )
                backoff = delay_base * (2 ** min(attempt - 1, 5))
                await asyncio.sleep(min(backoff, 30.0))
    finally:
        if circuit_breaker and not _cancelled:
            if _call_succeeded:
                await circuit_breaker.on_success()
            else:
                await circuit_breaker.on_failure()
