from __future__ import annotations

from typing import Any


def infer_failure_category(payload: dict[str, Any]) -> str:
    explicit_category = payload.get("failure_category")
    if explicit_category:
        return str(explicit_category)

    return_code = payload.get("return_code")
    try:
        if return_code is not None:
            numeric_return_code = int(return_code)
            if numeric_return_code < 0:
                return "process_killed"
            if numeric_return_code in {128 + 9, 128 + 15}:
                return "process_killed"
            if numeric_return_code > 0:
                return "process_failed"
    except (TypeError, ValueError):
        pass

    error_text = str(
        payload.get("error")
        or payload.get("error_message")
        or payload.get("message")
        or ""
    ).lower()
    if not error_text:
        return "unknown"
    if any(token in error_text for token in ("sigkill", "signal 9", "killed", "terminated by signal")):
        return "process_killed"
    if any(
        token in error_text
        for token in (
            "cuda out of memory",
            "outofmemoryerror",
            "out of memory",
            "cannot allocate memory",
            "memoryerror",
        )
    ):
        return "resource_exhausted"
    if "input file missing" in error_text or ("input path" in error_text and "does not exist" in error_text):
        return "input_missing"
    if "no such file" in error_text and ("/input" in error_text or ".pdf" in error_text):
        return "input_missing"
    if "timed out" in error_text or "timeout" in error_text:
        return "api_timeout"
    if any(
        token in error_text
        for token in (
            "connection refused",
            "connection reset",
            "connection aborted",
            "connect error",
            "failed to connect",
            "ssl certificate verify failed",
            "sslerror",
            "tls handshake failed",
            "name or service not known",
            "temporary failure in name resolution",
            "network is unreachable",
            "no route to host",
            "model unreachable",
        )
    ):
        return "model_unreachable"
    if any(
        token in error_text
        for token in (
            "invalid model response",
            "model response json",
            "failed to parse model response",
            "model output invalid",
            "jsondecodeerror",
            "invalid json from model",
            "expecting value",
            "response schema",
        )
    ):
        return "model_output_invalid"
    if any(
        token in error_text
        for token in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "authentication failed",
            "permission denied for model",
        )
    ):
        return "model_auth_failed"
    if any(
        token in error_text
        for token in (
            "429",
            "rate limit",
            "too many requests",
            "throttled",
        )
    ):
        return "model_rate_limited"
    if any(
        token in error_text
        for token in (
            "502",
            "503",
            "bad gateway",
            "service unavailable",
            "temporarily unavailable",
            "model overloaded",
            "server overloaded",
            "upstream unavailable",
        )
    ):
        return "model_unavailable"
    if (
        "http " in error_text
        or "httpstatuserror" in error_text
        or "status code" in error_text
        or "rate limit" in error_text
        or "too many requests" in error_text
        or "model server" in error_text
    ):
        return "model_error"
    if any(
        token in error_text
        for token in (
            "no space left on device",
            "disk quota exceeded",
            "read-only file system",
            "isadirectoryerror",
            "notadirectoryerror",
        )
    ):
        return "output_unwritable"
    if "no such file or directory" in error_text and any(
        token in error_text
        for token in ("output", "/out", "artifact", ".md", ".json", ".ocr_status.json")
    ):
        return "output_unwritable"
    if "permission denied" in error_text and any(token in error_text for token in ("output", "/out", "writing", "artifact")):
        return "output_unwritable"
    if "permission denied" in error_text and any(token in error_text for token in ("input", "/in", "source", ".pdf")):
        return "input_invalid"
    if any(
        token in error_text
        for token in (
            "cannot open broken document",
            "failed to open file",
            "malformed pdf",
            "corrupt pdf",
            "filedataerror",
            "requires a password",
            "password protected",
            "encrypted pdf",
        )
    ):
        return "input_invalid"
    if "artifact_missing" in error_text or "declared ocr artifacts are missing" in error_text:
        return "artifact_missing"
    if "input file changed" in error_text or "changed since manifest snapshot" in error_text:
        return "input_changed"
    if error_text.startswith("file extension "):
        return "input_invalid"
    return "parser_failed"
