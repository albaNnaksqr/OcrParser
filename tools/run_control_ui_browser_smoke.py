#!/usr/bin/env python3
"""Release-wheel browser smoke for the framework-free Control UI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def request_json(
    base_url: str,
    path: str,
    *,
    token: str,
    method: str = "GET",
    payload: dict | None = None,
) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": token,
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_control(base_url: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Control exited before startup with code {process.returncode}")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError("Control did not become healthy within 30 seconds")


def seed_control(base_url: str, token: str, shared_root: Path) -> None:
    shared_path = str(shared_root)
    request_json(
        base_url,
        "/api/model-profiles/dotsocr_15",
        token=token,
        method="PUT",
        payload={
            "label": "DotsOCR smoke profile",
            "engine": "dotsocr",
            "ip": "127.0.0.1",
            "port": 13080,
            "model_name": "DotsOCR",
            "page_concurrency": 2,
            "extra_args": {"file_concurrency": 1, "api_concurrency_max": 2},
            "requires_api_key": False,
            "is_default": True,
        },
    )
    request_json(
        base_url,
        "/api/servers/register",
        token=token,
        method="POST",
        payload={
            "id": "browser-smoke-worker",
            "name": "Browser smoke worker",
            "host": "worker.invalid",
            "capacity_slots": 2,
            "capabilities": {},
        },
    )
    request_json(
        base_url,
        "/api/servers/browser-smoke-worker/heartbeat",
        token=token,
        method="POST",
        payload={
            "status": "idle",
            "capabilities": {
                "shared_roots": [shared_path],
                "shared_paths": [
                    {
                        "path": shared_path,
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
                "resource_pressure": {"constrained": False},
                "event_spool": {"pending_events": 0, "pending_logs": 0},
                "pending_shard_updates": {"pending": 0},
                "git_ref": "browser-smoke",
                "script_version": "0.4.2",
            },
        },
    )


def run_browser(
    base_url: str,
    token: str,
    shared_root: Path,
    artifact_dir: Path,
) -> None:
    from playwright.sync_api import expect, sync_playwright

    console_messages: list[str] = []
    page_errors: list[str] = []
    network_errors: list[str] = []
    artifact_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda message: console_messages.append(f"{message.type}: {message.text}"))
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "requestfailed",
            lambda request: network_errors.append(
                f"{request.method} {request.url}: {request.failure}"
            ),
        )
        try:
            response = page.goto(f"{base_url}/ui/#jobs", wait_until="networkidle")
            assert response is not None and response.ok
            expect(page.locator('[data-route-link="jobs"]')).to_have_attribute("aria-current", "page")
            expect(page.locator("#jobsPanel")).to_be_visible()

            for asset in (
                "main.js", "navigation.js", "job-wizard.js", "jobs.js",
                "workers.js", "diagnostics.js", "styles.css",
            ):
                assert page.request.get(f"{base_url}/ui/{asset}").ok, asset

            with page.expect_response(
                lambda item: item.url.endswith("/api/system/diagnostics") and item.status == 200,
                timeout=10_000,
            ):
                page.locator("#controlApiToken").fill(token)

            page.locator('[data-route-link="system"]').click()
            expect(page).to_have_url(f"{base_url}/ui/#system")
            expect(page.locator("#deploymentDoctorIssues")).to_contain_text(
                "Production database is not PostgreSQL"
            )
            expect(page.locator("#deploymentDoctorIssues")).to_contain_text(
                "ocr-platform-migrate status"
            )

            page.locator('[data-route-link="workers"]').click()
            expect(page).to_have_url(f"{base_url}/ui/#workers")
            page.locator("#workerFilter").fill("browser-smoke")
            expect(page.locator("#serversBody")).to_contain_text("browser-smoke-worker")
            page.locator(".workerDetailsToggle").first.click()
            expect(page.locator("#serversBody .detail-row")).to_be_visible()
            page.go_back()
            expect(page).to_have_url(f"{base_url}/ui/#system")
            page.go_forward()
            expect(page).to_have_url(f"{base_url}/ui/#workers")

            page.goto(f"{base_url}/ui/#not-a-route")
            expect(page).to_have_url(f"{base_url}/ui/#jobs")
            page.locator("#openNewJobBtn").click()
            expect(page).to_have_url(f"{base_url}/ui/#jobs/new")

            input_dir = str(shared_root / "input")
            output_dir = str(shared_root / "output")
            page.locator("#inputDir").fill(input_dir)
            page.locator("#outputDir").fill(output_dir)
            page.locator("#wizardNextBtn").click()
            expect(page.locator('[data-wizard-step="2"]')).to_be_visible()
            expect(page.locator("#resolvedModelConfig")).to_contain_text("dotsocr")
            page.locator("#wizardBackBtn").click()
            expect(page.locator("#inputDir")).to_have_value(input_dir)
            page.locator("#wizardNextBtn").click()
            page.locator("#wizardNextBtn").click()
            expect(page.locator("#preflightMetrics")).to_contain_text("eligible")
            page.locator("#wizardNextBtn").click()
            expect(page.locator("#wizardReview")).to_contain_text("API key override")
            expect(page.locator("#createJobBtn")).to_be_disabled()
            page.locator("#preflightJobBtn").click()
            expect(page.locator("#wizardReview")).to_contain_text("Preflight passed")
            expect(page.locator("#createJobBtn")).to_be_enabled()
            page.locator("#createJobBtn").click()
            expect(page).to_have_url(f"{base_url}/ui/#jobs")
            expect(page.locator("#jobsBody")).to_contain_text(input_dir)
            expect(page.locator("#apiKey")).to_have_value("")

            for viewport in ({"width": 1440, "height": 900}, {"width": 1024, "height": 768}):
                page.set_viewport_size(viewport)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
                )
                assert not overflow, f"horizontal page overflow at {viewport}"

            unlabeled = page.evaluate(
                """Array.from(document.querySelectorAll('input:not([type=hidden]), select, textarea'))
                .filter((element) => element.id && element.labels && element.labels.length === 0)
                .map((element) => element.id)"""
            )
            assert not unlabeled, f"unlabelled controls: {unlabeled}"
            assert page.locator('[aria-live="polite"]').count() >= 2
            assert not page_errors, f"page errors: {page_errors}"
            assert not network_errors, f"network errors: {network_errors}"
        except Exception:
            page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
            (artifact_dir / "console.log").write_text("\n".join(console_messages), encoding="utf-8")
            (artifact_dir / "page-errors.log").write_text("\n".join(page_errors), encoding="utf-8")
            (artifact_dir / "network-errors.log").write_text("\n".join(network_errors), encoding="utf-8")
            raise
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18084)
    parser.add_argument("--artifact-dir", type=Path, default=Path("ui-browser-smoke-artifacts"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = "browser-smoke-control-token"
    base_url = f"http://{args.host}:{args.port}"
    temp_root = Path(tempfile.mkdtemp(prefix="ocrparser-ui-smoke-"))
    shared_root = temp_root / "shared"
    (shared_root / "input").mkdir(parents=True)
    (shared_root / "output").mkdir()
    (shared_root / ".ocr_platform" / "manifests").mkdir(parents=True)
    log_path = args.artifact_dir / "control.log"
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "OCR_PLATFORM_HOST": args.host,
        "OCR_PLATFORM_PORT": str(args.port),
        "OCR_PLATFORM_DATABASE_URL": f"sqlite:///{temp_root / 'control.db'}",
        "OCR_PLATFORM_API_TOKEN": token,
        "OCR_PLATFORM_REQUIRE_API_TOKEN": "1",
        "OCR_PLATFORM_ENABLE_REMOTE_ADMIN": "0",
    }
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "ocr_platform.control"],
            cwd=temp_root,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_control(base_url, process)
            seed_control(base_url, token, shared_root)
            run_browser(base_url, token, shared_root, args.artifact_dir)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            shutil.rmtree(temp_root, ignore_errors=True)
    for artifact in args.artifact_dir.iterdir():
        if artifact.name != "control.log":
            artifact.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    print("Control UI browser smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
