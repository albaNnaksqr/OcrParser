from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DEFAULT_LAYOUT_TEXT = "Mock OCR page text for local distributed job walkthrough."


def build_layout_response(text: str = DEFAULT_LAYOUT_TEXT) -> str:
    return json.dumps(
        [
            {
                "category": "Text",
                "bbox": [50, 50, 950, 180],
                "text": text,
            }
        ],
        ensure_ascii=False,
    )


def build_chat_completion_response(*, model: str, content: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


class MockOCRHandler(BaseHTTPRequestHandler):
    server_version = "OCRMock/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        super().log_message(format, *args)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/readyz"}:
            self._write_json(200, {"ok": True})
            return
        if self.path == "/v1/models":
            model = getattr(self.server, "model_name", "mock-ocr")
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": model, "object": "model", "owned_by": "local"}],
                },
            )
            return
        self._write_json(404, {"error": {"message": f"unknown path: {self.path}"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload: dict[str, Any] = {}
        if length:
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                self._write_json(400, {"error": {"message": "request body must be JSON"}})
                return
        if self.path != "/v1/chat/completions":
            self._write_json(404, {"error": {"message": f"unknown path: {self.path}"}})
            return
        model = str(payload.get("model") or getattr(self.server, "model_name", "mock-ocr"))
        content = build_layout_response(getattr(self.server, "layout_text", DEFAULT_LAYOUT_TEXT))
        self._write_json(200, build_chat_completion_response(model=model, content=content))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a local OpenAI-compatible OCR mock for distributed job walkthroughs."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--model-name", default="mock-ocr")
    parser.add_argument("--text", default=DEFAULT_LAYOUT_TEXT)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), MockOCRHandler)
    server.model_name = args.model_name
    server.layout_text = args.text
    server.quiet = args.quiet
    print(f"Mock OCR service listening on http://{args.host}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
