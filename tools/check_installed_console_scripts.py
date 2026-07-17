#!/usr/bin/env python3
"""Smoke-test all console scripts from an installed wheel environment."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    args = parser.parse_args()

    for name in ("ocr-parser", "ocr-platform-agent", "ocr-platform-migrate"):
        subprocess.run([str(args.bin_dir / name), "--help"], check=True, stdout=subprocess.DEVNULL)

    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="ocrparser-wheel-control-") as temp:
        env = os.environ.copy()
        env.update(
            {
                "OCR_PLATFORM_HOST": "127.0.0.1",
                "OCR_PLATFORM_PORT": str(port),
                "OCR_PLATFORM_DATABASE_URL": f"sqlite:///{Path(temp) / 'control.db'}",
            }
        )
        control = subprocess.Popen(
            [str(args.bin_dir / "ocr-platform-control")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if control.poll() is not None:
                    raise RuntimeError(control.stderr.read() if control.stderr else "control exited")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("installed ocr-platform-control did not become healthy")
        finally:
            control.terminate()
            try:
                control.wait(timeout=5)
            except subprocess.TimeoutExpired:
                control.kill()
                control.wait(timeout=5)

    print("Installed parser, agent, control, and migration console scripts are healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
