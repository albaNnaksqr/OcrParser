"""Worker-side Control API reachability probe used by `ocr_agent_worker.sh doctor`.

Every failure is reported as a single actionable line. The Control API token is
read from the environment, is never echoed, and no traceback ever reaches the
operator running `doctor`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Sequence


CONTROL_TOKEN_HEADER = "X-OCR-Platform-Token"
CONTROL_TOKEN_ENV_VAR = "OCR_CONTROL_API_TOKEN"
SERVERS_PATH = "/api/servers"

UNAUTHORIZED_HINT = (
    f"set {CONTROL_TOKEN_ENV_VAR} in the worker env file to the same value as "
    "OCR_PLATFORM_API_TOKEN on the control host"
)
UNREACHABLE_HINT = (
    "verify OCR_CONTROL_URL and that the control service is listening and reachable "
    "from this host"
)
HTTP_ERROR_HINT = (
    "check the control service logs and that OCR_CONTROL_URL points at the control "
    "API root"
)
INVALID_RESPONSE_HINT = (
    "OCR_CONTROL_URL must point at the control API root, not a proxy or HTML page"
)


def servers_url(control_url: str) -> str:
    return control_url.rstrip("/") + SERVERS_PATH


def build_request(control_url: str, *, token: str | None) -> urllib.request.Request:
    request = urllib.request.Request(servers_url(control_url))
    if token:
        request.add_header(CONTROL_TOKEN_HEADER, token)
    return request


def _auth_state(token: str | None) -> str:
    return "token" if token else "anonymous"


def probe_control_api(
    control_url: str,
    server_id: str,
    *,
    token: str | None = None,
    timeout: float = 10.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, str]:
    """Return an ``(exit_code, diagnostic_line)`` pair that never contains the token."""
    url = servers_url(control_url)
    auth = _auth_state(token)
    try:
        with opener(build_request(control_url, token=token), timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return 1, (
                f"control_api=unauthorized status=401 url={url} auth={auth} "
                f"hint={UNAUTHORIZED_HINT}"
            )
        return 1, (
            f"control_api=http_error status={exc.code} url={url} auth={auth} "
            f"hint={HTTP_ERROR_HINT}"
        )
    except urllib.error.URLError as exc:
        return 1, (
            f"control_api=unreachable url={url} auth={auth} reason={exc.reason} "
            f"hint={UNREACHABLE_HINT}"
        )
    except OSError as exc:
        return 1, (
            f"control_api=unreachable url={url} auth={auth} "
            f"reason={type(exc).__name__} hint={UNREACHABLE_HINT}"
        )
    try:
        rows = json.loads(body)
    except ValueError:
        return 1, (
            f"control_api=invalid_response url={url} auth={auth} "
            f"reason=response_is_not_json hint={INVALID_RESPONSE_HINT}"
        )
    if not isinstance(rows, list):
        return 1, (
            f"control_api=invalid_response url={url} auth={auth} "
            f"hint={INVALID_RESPONSE_HINT}"
        )
    registered = any(
        isinstance(row, dict) and str(row.get("id")) == server_id for row in rows
    )
    return 0, (
        f"control_api=ok servers={len(rows)} server_id={server_id} "
        f"registered={'yes' if registered else 'no'} auth={auth}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control_url", required=True)
    parser.add_argument("--server_id", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exit_code, line = probe_control_api(
        args.control_url,
        args.server_id,
        token=os.environ.get(CONTROL_TOKEN_ENV_VAR) or None,
        timeout=args.timeout,
    )
    print(line, file=sys.stderr if exit_code else sys.stdout)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
