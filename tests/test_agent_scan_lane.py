import asyncio
import sys

from ocr_platform.agent import __main__ as agent_main
from ocr_platform.agent.config import AgentConfig


class ScanLaneClient:
    def __init__(self):
        self.scan_units = [{"id": 1, "job_id": "job-1"}]
        self.heartbeats = []

    async def claim_scan_unit(self, server_id):
        return self.scan_units.pop(0) if self.scan_units else None

    async def heartbeat(self, **payload):
        self.heartbeats.append(payload)


def test_scan_lane_can_process_scan_unit_independently_of_job_loop(tmp_path, monkeypatch):
    processed = []

    async def fake_run_scan_unit(scan_unit, config, client):
        processed.append((scan_unit, config.server_id))
        return 0

    monkeypatch.setattr(agent_main, "run_scan_unit", fake_run_scan_unit)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
        resource_guard_enabled=False,
    )
    client = ScanLaneClient()

    did_work = asyncio.run(agent_main._run_scan_once(client, config))

    assert did_work is True
    assert processed == [({"id": 1, "job_id": "job-1"}, "server-a")]
    assert client.heartbeats[0]["status"] == "busy"
    assert client.heartbeats[0]["current_job_id"] == "job-1"
    assert client.heartbeats[-1]["status"] == "idle"


def test_scan_lane_skips_claim_when_resource_constrained(tmp_path, monkeypatch):
    class GuardedScanLaneClient(ScanLaneClient):
        def __init__(self):
            super().__init__()
            self.claim_count = 0

        async def claim_scan_unit(self, server_id):
            self.claim_count += 1
            return await super().claim_scan_unit(server_id)

    monkeypatch.setattr(
        agent_main,
        "_resource_pressure",
        lambda config: {"constrained": True, "reasons": ["memory percent 95.0% >= 90.0%"]},
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = GuardedScanLaneClient()

    did_work = asyncio.run(agent_main._run_scan_once(client, config))

    assert did_work is False
    assert client.claim_count == 0


def test_manifest_integrity_lane_claims_checks_and_posts_report(tmp_path, monkeypatch):
    class IntegrityClient:
        def __init__(self):
            self.claims = 0
            self.completed = []

        async def claim_manifest_integrity(self, server_id):
            self.claims += 1
            return {
                "job_id": "job-1",
                "manifest_id": 7,
                "manifest_path": str(tmp_path / "manifest.jsonl"),
                "meta_path": None,
                "manifest_expected_file_count": 0,
                "manifest_expected_total_bytes": 0,
                "shards": [],
            }

        async def complete_manifest_integrity(self, manifest_id, payload, server_id):
            self.completed.append((manifest_id, payload, server_id))
            return {"manifest_id": manifest_id}

    monkeypatch.setattr(
        agent_main,
        "build_worker_manifest_integrity_report",
        lambda task: {"job_id": task["job_id"], "manifest_id": task["manifest_id"], "ok": True, "status": "ok"},
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
        resource_guard_enabled=False,
    )
    client = IntegrityClient()

    did_work = asyncio.run(agent_main._run_manifest_integrity_once(client, config))

    assert did_work is True
    assert client.claims == 1
    assert client.completed == [
        (
            7,
            {"report": {"job_id": "job-1", "manifest_id": 7, "ok": True, "status": "ok"}},
            "server-a",
        )
    ]
