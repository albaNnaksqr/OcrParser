#!/usr/bin/env python3
"""Verify one wheel installation profile in an otherwise empty environment."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import socket
import subprocess
import sys
import tempfile
import time
from importlib.metadata import distribution
from pathlib import Path


PLATFORM_HINT = "pip install 'ocrparser-platform[platform]'"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, process: subprocess.Popen[str], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"mock OCR service exited with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for mock OCR service on port {port}")


def _verify_base_parser(bin_dir: Path, repo_root: Path) -> None:
    parser = bin_dir / "ocr-parser"
    subprocess.run([str(parser), "--help"], check=True, stdout=subprocess.DEVNULL)

    port = _free_port()
    mock = subprocess.Popen(
        [
            sys.executable,
            str(repo_root / "tools" / "mock_ocr_service.py"),
            "--port",
            str(port),
            "--quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port, mock)
        with tempfile.TemporaryDirectory(prefix="ocrparser-base-profile-") as temp:
            output_dir = Path(temp) / "output"
            result = subprocess.run(
                [
                    str(parser),
                    "--input_file",
                    str(repo_root / "tests" / "fixtures" / "public_pdfs" / "simple_text_1p.pdf"),
                    "--output_dir",
                    str(output_dir),
                    "--ip",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--model_name",
                    "mock-ocr",
                    "--no_warmup",
                    "--disable_process_pool",
                    "--page_concurrency",
                    "1",
                    "--api_concurrency_max",
                    "1",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stdout)
            markdown = list(output_dir.rglob("*.md"))
            if not markdown:
                raise RuntimeError("base profile mock parser produced no Markdown artifact")
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mock.kill()
            mock.wait(timeout=5)


def _verify_platform_hint(bin_dir: Path) -> None:
    for name in ("ocr-platform-control", "ocr-platform-agent", "ocr-platform-migrate"):
        result = subprocess.run(
            [str(bin_dir / name), "--help"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 2, (name, result.returncode, result.stdout, result.stderr)
        assert PLATFORM_HINT in result.stderr, (name, result.stderr)
        assert "Traceback" not in result.stderr, (name, result.stderr)


def _import_modules(*names: str) -> None:
    for name in names:
        importlib.import_module(name)


def _verify_imports_use_installed_wheel(repo_root: Path) -> None:
    spec = importlib.util.find_spec("ocr_parser")
    assert spec is not None and spec.origin
    module_path = Path(spec.origin).resolve()
    source_root = repo_root.resolve()
    if module_path == source_root or source_root in module_path.parents:
        raise RuntimeError(
            f"install-profile verification imported source checkout: {module_path}"
        )
    installed_package = Path(
        distribution("ocrparser-platform").locate_file("ocr_parser")
    ).resolve()
    if module_path.parent != installed_package:
        raise RuntimeError(
            "install-profile verification did not import the installed wheel: "
            f"module={module_path}, distribution={installed_package}"
        )


def _verify_paddle_cross_page_table_merge() -> None:
    _import_modules("bs4")
    from ocr_parser.engines.paddleocr_vl import _merge_tables_across_pages

    pages = [
        [
            {
                "label": "table",
                "bbox": [0, 0, 100, 100],
                "content": (
                    "<table><tr><th>Item</th><th>Value</th></tr>"
                    "<tr><td>alpha</td><td>1</td></tr></table>"
                ),
            }
        ],
        [
            {
                "label": "table",
                "bbox": [0, 0, 100, 100],
                "content": (
                    "<table><tr><th>Item</th><th>Value</th></tr>"
                    "<tr><td>beta</td><td>2</td></tr></table>"
                ),
            }
        ],
    ]

    merged = _merge_tables_across_pages(pages)

    assert merged is pages
    assert merged[1][0]["content"] == ""
    assert merged[1][0]["_ggid"] == merged[0][0]["_gid"]
    merged_html = merged[0][0]["content"]
    assert "alpha" in merged_html and "beta" in merged_html
    assert merged_html.count("Item") == 1


def _verify_data_index_package_data() -> None:
    spec = importlib.util.find_spec("dots_ocr.data_index")
    assert spec is not None and spec.submodule_search_locations
    package_dir = Path(next(iter(spec.submodule_search_locations)))
    required = {
        "content_type_config.json",
        "data_index_config.json",
        "demain_label_config.json",
    }
    installed = {path.name for path in (package_dir / "configs").glob("*.json")}
    assert required <= installed, required - installed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("base", "platform", "s3", "layout", "full"), required=True)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()

    _import_modules("ocr_parser", "ocr_parser.cli", "ocr_parser.parser")
    _verify_imports_use_installed_wheel(args.repo_root)
    _verify_paddle_cross_page_table_merge()
    _verify_base_parser(args.bin_dir, args.repo_root)

    if args.profile in {"base", "s3", "layout"}:
        _verify_platform_hint(args.bin_dir)
    if args.profile in {"platform", "full"}:
        _import_modules(
            "ocr_platform.control.app",
            "ocr_platform.control.migrate_cli",
            "ocr_platform.agent.__main__",
        )
        subprocess.run(
            [str(args.bin_dir / "ocr-platform-migrate"), "--help"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    if args.profile in {"s3", "full"}:
        _import_modules("dots_ocr.utils.s3_downloader", "dots_ocr.utils.s3_upload")
    if args.profile == "layout":
        _import_modules("services.layout_detection.server")
    if args.profile == "full":
        _import_modules("dotenv")
        _verify_data_index_package_data()

    print(f"Verified wheel installation profile: {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
