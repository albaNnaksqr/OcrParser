import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

from tools import run_stability_soak as soak


def test_command_gate_records_success_without_exposing_secret(tmp_path: Path) -> None:
    result = soak.run_command_gate(
        "probe",
        [sys.executable, "-c", "print('token-value')"],
        cwd=tmp_path,
        env_overrides={"PRIVATE_VALUE": "token-value"},
        secret_values=["token-value"],
    )

    assert result.ok is True
    assert result.detail == "***"
    assert result.duration_seconds >= 0


def write_wheel(path: Path, *, version: str = soak.EXPECTED_VERSION, revision: str = soak.EXPECTED_REVISION, dirty=False):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"ocrparser_platform-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: ocrparser-platform\nVersion: {version}\n",
        )
        archive.writestr(
            "ocr_platform/_build_info.json",
            json.dumps({"source_revision": revision, "dirty": dirty, "build_timestamp": "2026-07-17T00:00:00Z"}),
        )


def test_release_wheel_gate_requires_exact_clean_revision(tmp_path):
    wheel = tmp_path / "release.whl"
    write_wheel(wheel)

    result = soak.verify_release_wheel(
        wheel,
        expected_version=soak.EXPECTED_VERSION,
        expected_revision=soak.EXPECTED_REVISION,
    )

    assert result.ok is True
    assert soak.EXPECTED_REVISION in result.detail

    dirty_wheel = tmp_path / "dirty.whl"
    write_wheel(dirty_wheel, dirty=True)
    dirty = soak.verify_release_wheel(
        dirty_wheel,
        expected_version=soak.EXPECTED_VERSION,
        expected_revision=soak.EXPECTED_REVISION,
    )
    assert dirty.status == "fail"
    assert "clean release" in dirty.detail


def test_fault_plan_is_ordered_and_rejects_shell_strings(tmp_path):
    path = tmp_path / "faults.json"
    path.write_text(
        json.dumps(
            {
                "hooks": [
                    {"name": "second", "cycle": 2, "after_seconds": 1, "argv": ["/bin/true"]},
                    {"name": "first", "cycle": 1, "after_seconds": 4, "argv": ["/bin/true"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    hooks = soak.load_fault_plan(path)

    assert [hook.name for hook in hooks] == ["first", "second"]
    assert hooks[0].argv == ("/bin/true",)


def test_sidecar_scan_reports_bounded_stages_fallbacks_and_failures(tmp_path):
    ok_dir = tmp_path / "output" / "ok"
    ok_dir.mkdir(parents=True)
    (ok_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "success_fallback_text",
                "stages": [
                    {"stage": "layout", "status": "failed"},
                    {"stage": "single_stage_ocr", "status": "success"},
                ],
                "fallback": {"used": True, "source_stage": "layout", "reason": "layout_unavailable"},
            }
        ),
        encoding="utf-8",
    )
    bad_dir = tmp_path / "output" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / ".ocr_status.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "failure_category": "model_error",
                "stages": [{"stage": "request-id-123", "status": "exploded"}],
                "fallback": {"used": False, "source_stage": None, "reason": None},
            }
        ),
        encoding="utf-8",
    )

    stages, fallbacks, unknown, failures = soak.scan_sidecars(tmp_path / "output")

    assert stages["layout:failed"] == 1
    assert stages["single_stage_ocr:success"] == 1
    assert fallbacks == {"layout:layout_unavailable": 1}
    assert "stage:request-id-123" in unknown
    assert "stage_status:exploded" in unknown
    assert failures[0]["status"] == "failed"


def test_directory_output_audit_requires_one_success_sidecar_and_combined_markdown(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    (input_dir / "document.pdf").write_bytes(b"public fixture")
    document_dir = output_dir / "document" / "native" / "dotsocr"
    document_dir.mkdir(parents=True)
    (output_dir / "document" / ".ocr_status.json").write_text(
        json.dumps({"status": "success"}),
        encoding="utf-8",
    )
    (document_dir / "document.md").write_text("ok\n", encoding="utf-8")

    healthy = soak.audit_directory_outputs(input_dir=input_dir, output_dir=output_dir)
    assert healthy["ok"] is True

    (document_dir / "document.md").unlink()
    broken = soak.audit_directory_outputs(input_dir=input_dir, output_dir=output_dir)
    assert broken["ok"] is False
    assert broken["missing_or_duplicate_combined_markdown"] == ["document"]


def make_cycle(index: int, *, duration: float, ok: bool = True) -> soak.CycleResult:
    return soak.CycleResult(
        cycle=index,
        input_mode="directory",
        shared_root=f"/tmp/cycle-{index}",
        document_count=100,
        status="pass" if ok else "fail",
        duration_seconds=duration,
        job_summary={"status": "succeeded" if ok else "failed"},
        manifest_integrity={"ok": ok},
        output_audit={"ok": ok},
    )


def test_cycle_rejects_empty_audit_or_failed_samples():
    cycle = make_cycle(1, duration=1)
    cycle.output_audit = {}
    assert cycle.ok is False

    cycle.output_audit = {"ok": True}
    cycle.failed_samples = [{"path": "document", "status": "failed"}]
    assert cycle.ok is False


def test_throughput_and_resource_gates_enforce_twenty_and_ten_percent_limits():
    throughput = soak.analyze_throughput(
        [make_cycle(1, duration=10), make_cycle(2, duration=10), make_cycle(3, duration=12), make_cycle(4, duration=12)]
    )
    assert throughput.status == "fail"
    assert "regression=16.667%" in throughput.detail

    resources = soak.analyze_resource_growth(
        [
            soak.ResourceSample("agent", 1, 1000, 100, 1.0, "ok"),
            soak.ResourceSample("agent", 1, 1210, 100, 2.0, "ok"),
        ]
    )
    assert resources.status == "fail"
    assert "RSS growth" in resources.detail


def test_resource_gate_allows_low_bounded_fd_fluctuation():
    result = soak.analyze_resource_growth(
        [
            soak.ResourceSample("agent", 1, 1000, 7, 1.0, "ok"),
            soak.ResourceSample("agent", 1, 1001, 9, 2.0, "ok"),
            soak.ResourceSample("agent", 1, 1002, 8, 3.0, "ok"),
            soak.ResourceSample("agent", 1, 1002, 9, 4.0, "ok"),
            soak.ResourceSample("agent", 1, 1002, 7, 5.0, "ok"),
            soak.ResourceSample("agent", 1, 1002, 9, 6.0, "ok"),
        ]
    )

    assert result.status == "pass"
    assert "fd_delta=2" in result.detail


def test_resource_gate_rejects_sustained_fd_growth():
    result = soak.analyze_resource_growth(
        [
            soak.ResourceSample("agent", 1, 1000, fd, float(index), "ok")
            for index, fd in enumerate([7, 8, 9, 10, 11], start=1)
        ]
    )

    assert result.status == "fail"
    assert "FD sustained growth" in result.detail


def test_resource_gate_rejects_staircase_fd_growth_with_plateaus():
    result = soak.analyze_resource_growth(
        [
            soak.ResourceSample("agent", 1, 1000, fd, float(index), "ok")
            for index, fd in enumerate([7, 8, 8, 9, 9, 10, 10, 11], start=1)
        ]
    )

    assert result.status == "fail"
    assert "non_decreasing_ratio=100.0%" in result.detail


def test_resource_gate_rejects_sustained_rss_growth():
    result = soak.analyze_resource_growth(
        [
            soak.ResourceSample("agent", 1, rss, 7, float(index), "ok")
            for index, rss in enumerate([1000, 1050, 1125, 1250], start=1)
        ]
    )

    assert result.status == "fail"
    assert "RSS growth" in result.detail


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def run_fake_schedule(*, cycles: int, duration: float, cycle_seconds: float):
    clock = FakeClock()
    executed: list[int] = []
    samples: list[float] = []
    args = SimpleNamespace(cycles=cycles, duration_seconds=duration)

    def execute(**kwargs):
        executed.append(kwargs["cycle"])
        clock.now += cycle_seconds
        return make_cycle(kwargs["cycle"], duration=cycle_seconds)

    def collect(label, _pid_file):
        samples.append(clock.now)
        return soak.ResourceSample(label, 1, 1000, 7, clock.now, "ok")

    results, resources = soak.run_soak_cycles(
        args=args,
        modes=["directory"],
        token="runtime-only",
        hooks=[],
        report_dir=Path("/unused"),
        resource_pid_files=[("agent", Path("/unused.pid"))],
        clock=clock.monotonic,
        sleeper=clock.sleep,
        cycle_executor=execute,
        resource_collector=collect,
    )
    return clock, executed, samples, results, resources


def test_twenty_cycle_day_waits_until_full_duration_without_extra_cycle():
    clock, executed, samples, results, resources = run_fake_schedule(
        cycles=20,
        duration=24 * 60 * 60,
        cycle_seconds=1,
    )

    assert clock.now == 24 * 60 * 60
    assert executed == list(range(1, 21))
    assert len(results) == 20
    assert samples[-1] == 24 * 60 * 60
    assert samples.count(24 * 60 * 60) == 1
    assert len(resources) > 20


def test_non_positive_duration_runs_cycles_without_waiting():
    for duration in (0, -1):
        clock, executed, _samples, results, _resources = run_fake_schedule(
            cycles=3,
            duration=duration,
            cycle_seconds=1,
        )

        assert clock.now == 3
        assert clock.sleeps == []
        assert executed == [1, 2, 3]
        assert len(results) == 3


def test_cycles_exceeding_deadline_do_not_wait_or_add_cycle():
    clock, executed, _samples, results, _resources = run_fake_schedule(
        cycles=2,
        duration=10,
        cycle_seconds=6,
    )

    assert clock.now == 12
    assert clock.sleeps == []
    assert executed == [1, 2]
    assert len(results) == 2


def test_reports_do_not_need_secret_values_and_include_failure_index(tmp_path):
    report = soak.write_reports(
        tmp_path,
        configuration={
            "expected_version": soak.EXPECTED_VERSION,
            "expected_revision": soak.EXPECTED_REVISION,
            "control_token_env_var": "OCR_SOAK_CONTROL_TOKEN",
        },
        gates=[soak.GateResult("release_wheel", "pass", "ok")],
        cycles=[make_cycle(1, duration=1.0)],
        resources=[],
    )

    assert report["status"] == "pass"
    assert (tmp_path / "report.json").is_file()
    assert (tmp_path / "report.md").is_file()
    assert "OCR_SOAK_CONTROL_TOKEN" in (tmp_path / "report.json").read_text(encoding="utf-8")
