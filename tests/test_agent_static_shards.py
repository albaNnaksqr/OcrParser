import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.sql.dml import Update

from ocr_platform.agent import runner
from ocr_platform.agent.client import ControlClient
from ocr_platform.agent.config import AgentConfig
from ocr_platform.agent.runner import build_ocr_command
from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import Job, WorkShard
from ocr_platform.control import service
from ocr_platform.control.service import POOL_SERVER_ID


def test_build_ocr_command_uses_input_manifest_for_shard(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable="/python",
    )
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "shard": {
            "id": 7,
            "shard_path": "/shared/manifests/job/shards/shard-000007.jsonl",
        },
    }

    command, _ = build_ocr_command(job, config)

    assert "--input_manifest" in command
    assert "/shared/manifests/job/shards/shard-000007.jsonl" in command
    assert "--input_root" in command
    assert "/shared/input" in command
    assert "--input_dir" not in command


def test_build_ocr_command_preserves_resume_for_shard_retries(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable="/python",
    )
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "extra_args": {"disable_resume": True, "skip_blank_pages": True},
        "shard": {
            "id": 7,
            "shard_path": "/shared/manifests/job/shards/shard-000007.jsonl",
        },
    }

    command, _ = build_ocr_command(job, config)

    assert "--input_manifest" in command
    assert "--disable_resume" not in command
    assert "--skip_blank_pages" in command


def test_build_ocr_command_uses_distinct_event_file_for_each_shard_attempt(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable="/python",
    )
    base_job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
    }

    first_command, first_event_file = build_ocr_command(
        base_job
        | {
            "shard": {
                "id": 7,
                "attempt_count": 1,
                "shard_path": "/shared/manifests/job/shards/shard-000007.jsonl",
            }
        },
        config,
    )
    second_command, second_event_file = build_ocr_command(
        base_job
        | {
            "shard": {
                "id": 8,
                "attempt_count": 1,
                "shard_path": "/shared/manifests/job/shards/shard-000008.jsonl",
            }
        },
        config,
    )

    assert first_event_file != second_event_file
    assert first_event_file.name == "shard-7-attempt-1-events.jsonl"
    assert second_event_file.name == "shard-8-attempt-1-events.jsonl"
    assert str(first_event_file) in first_command
    assert str(second_event_file) in second_command


def test_static_shard_summary_treats_retrying_and_stale_as_active():
    assert runner._summary_has_active_shards({"retrying_shards": 1}) is True
    assert runner._summary_has_active_shards({"stale_shards": 1}) is True
    assert runner._summary_has_active_shards({"pending_shards": 0, "running_shards": 0}) is False


class StaticShardClient:
    def __init__(self):
        self.claims = [
            {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 2},
            {"id": 2, "shard_path": "/manifest/shard-2.jsonl", "file_count": 3},
            None,
        ]
        self.claim_calls = []
        self.updates = []
        self.events = []

    async def claim_shard(self, job_id, server_id):
        self.claim_calls.append((job_id, server_id))
        return self.claims.pop(0)

    async def update_shard(self, shard_id, payload):
        self.updates.append((shard_id, payload))
        return {"id": shard_id, **payload}

    async def get_job(self, job_id):
        return {"id": job_id, "stop_requested": False, "status": "running"}

    async def get_job_summary(self, job_id):
        return {
            "id": job_id,
            "pending_shards": 0,
            "running_shards": 0,
            "failed_shards": 0,
            "stopped_shards": 0,
        }

    async def post_event(self, job_id, event):
        self.events.append((job_id, event))

    async def post_log(self, job_id, stream, line):
        pass


class FinishedProcess:
    returncode = 0


def test_forward_stream_tolerates_transient_log_post_failure():
    class FlakyLogClient:
        def __init__(self):
            self.calls = 0
            self.lines = []

        async def post_log(self, job_id, stream, line):
            self.calls += 1
            if self.calls == 1:
                request = httpx.Request("POST", "http://control/api/jobs/job-1/logs")
                raise httpx.ConnectError("control unavailable", request=request)
            self.lines.append((job_id, stream, line))

    async def exercise():
        stream = asyncio.StreamReader()
        stream.feed_data(b"first\nsecond\n")
        stream.feed_eof()
        client = FlakyLogClient()

        await runner._forward_stream(stream, "stdout", "job-1", client)

        return client

    client = asyncio.run(exercise())

    assert client.lines == [("job-1", "stdout", "second")]


def test_forward_events_updates_running_shard_progress(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        "\n".join(
            [
                '{"type":"page_done","payload":{"file_path":"/input/a.pdf","page_no":1}}',
                '{"type":"page_done","payload":{"file_path":"/input/a.pdf","page_no":2}}',
                '{"type":"file_done","payload":{"file_path":"/input/a.pdf","status":"success"}}',
                '{"type":"file_failed","payload":{"file_path":"/input/b.pdf","error":"boom"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = StaticShardClient()

    status = asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
        )
    )

    assert status.saw_file_failed is True
    assert [event["type"] for _job_id, event in client.events] == [
        "page_done",
        "page_done",
        "file_done",
        "file_failed",
    ]
    assert client.updates[-1] == (
        7,
        {
            "status": "running",
            "processed_files": 2,
            "failed_files": 1,
            "skipped_files": 0,
            "completed_pages": 2,
        },
    )


def test_forward_events_updates_running_shard_runtime_metrics(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        "\n".join(
            [
                (
                    '{"type":"file_done","payload":{'
                    '"file_path":"/input/a.pdf",'
                    '"status":"success",'
                    '"runtime":{'
                    '"api_inflight":7,'
                    '"api_inflight_peak":9,'
                    '"api_waiting":2,'
                    '"oldest_api_inflight":3.25'
                    '},'
                    '"execution_control":{'
                    '"paused":true,'
                    '"api_concurrency_limit":1,'
                    '"reason":"memory pressure"'
                    "}}}"
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = StaticShardClient()

    asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
        )
    )

    assert client.updates[-1] == (
        7,
        {
            "status": "running",
            "processed_files": 1,
            "failed_files": 0,
            "skipped_files": 0,
            "completed_pages": 0,
            "api_inflight": 7,
            "api_inflight_peak": 9,
            "api_waiting": 2,
            "oldest_api_inflight": 3.25,
            "execution_paused": True,
            "api_concurrency_limit": 1,
            "execution_control_reason": "memory pressure",
        },
    )


def test_forward_events_tolerates_transient_shard_progress_update_failure(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        '{"type":"file_done","payload":{"file_path":"/input/a.pdf","status":"success"}}\n',
        encoding="utf-8",
    )

    class TransientUpdateFailureClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.ConnectError("control temporarily unavailable")

    client = TransientUpdateFailureClient()

    status = asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
        )
    )

    assert status.processed_file_count == 1
    assert [event["type"] for _job_id, event in client.events] == ["file_done"]
    assert client.updates == [
        (
            7,
            {
                "status": "running",
                "processed_files": 1,
                "failed_files": 0,
                "skipped_files": 0,
                "completed_pages": 0,
            },
        )
    ]


def test_forward_events_spools_running_shard_progress_update_on_transient_failure(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        '{"type":"file_done","payload":{"file_path":"/input/a.pdf","status":"success"}}\n',
        encoding="utf-8",
    )

    class TransientUpdateFailureClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.ConnectError("control temporarily unavailable")

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = TransientUpdateFailureClient()

    asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
            shard_update_context={"assigned_server_id": "server-a", "attempt_count": 3},
            config=config,
        )
    )

    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-7.json"
    record = json.loads(pending_path.read_text(encoding="utf-8"))
    assert record["job_id"] == "job-1"
    assert record["shard_id"] == 7
    assert record["server_id"] == "server-a"
    assert record["payload"] == {
        "status": "running",
        "processed_files": 1,
        "failed_files": 0,
        "skipped_files": 0,
        "completed_pages": 0,
        "assigned_server_id": "server-a",
        "attempt_count": 3,
    }


def test_resource_execution_control_watcher_spools_shard_update_on_transient_failure(
    tmp_path, monkeypatch
):
    class RunningProcess:
        returncode = None

    process = RunningProcess()

    async def stop_after_first_sleep(seconds):
        process.returncode = 0

    def constrained_pressure(config):
        return {"constrained": True, "reasons": ["memory pressure"]}

    class TransientUpdateFailureClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.ConnectError("control temporarily unavailable")

    monkeypatch.setattr(runner, "resource_pressure", constrained_pressure)
    monkeypatch.setattr(runner.asyncio, "sleep", stop_after_first_sleep)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = TransientUpdateFailureClient()
    job = {
        "id": "job-1",
        "shard": {"id": 7, "assigned_server_id": "server-a", "attempt_count": 3},
    }

    asyncio.run(
        runner.resource_execution_control_watcher(
            job,
            config,
            runner._execution_control_path(config, "job-1"),
            process,
            client=client,
        )
    )

    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-7.json"
    record = json.loads(pending_path.read_text(encoding="utf-8"))
    assert record["payload"] == {
        "status": "running",
        "execution_paused": True,
        "api_concurrency_limit": 1,
        "execution_control_reason": "memory pressure",
        "assigned_server_id": "server-a",
        "attempt_count": 3,
    }


def test_forward_events_preserves_failure_category_for_shard_finalization(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        (
            '{"type":"job_failed","payload":{'
            '"failure_category":"model_timeout",'
            '"error_message":"OCR API timed out"'
            "}}\n"
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    status = asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
        )
    )

    assert status.failure_category == "model_timeout"
    assert status.error_message == "OCR API timed out"
    assert status.shard_progress_payload("failed")["failure_category"] == "model_timeout"
    assert status.shard_progress_payload("failed")["error_message"] == "OCR API timed out"


def test_forward_events_classifies_uncategorized_parser_failure(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        '{"type":"job_failed","payload":{"error":"one file failed"}}\n',
        encoding="utf-8",
    )
    client = StaticShardClient()

    status = asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
        )
    )

    assert status.failure_category == "parser_failed"
    assert status.error_message == "one file failed"
    assert status.shard_progress_payload("failed")["failure_category"] == "parser_failed"


def test_forward_events_infers_failure_category_from_uncategorized_error(tmp_path):
    event_file = tmp_path / "events.jsonl"
    event_file.write_text(
        '{"type":"job_failed","payload":{"error":"model request timed out after 180s"}}\n',
        encoding="utf-8",
    )
    client = StaticShardClient()

    status = asyncio.run(
        runner._forward_events_until_done(
            event_file,
            "job-1",
            client,
            FinishedProcess(),
            shard_id=7,
        )
    )

    assert status.failure_category == "api_timeout"
    assert status.error_message == "model request timed out after 180s"
    assert status.shard_progress_payload("failed")["failure_category"] == "api_timeout"


def test_failure_payload_for_return_code_distinguishes_signal_kill():
    payload = runner.failure_payload_for_return_code(-9)

    assert payload == {
        "return_code": -9,
        "failure_category": "process_killed",
        "error_message": "process killed by signal 9",
    }


def test_failure_payload_for_return_code_distinguishes_shell_signal_exit_code():
    payload = runner.failure_payload_for_return_code(137)

    assert payload == {
        "return_code": 137,
        "failure_category": "process_killed",
        "error_message": "process killed by signal 9",
    }


class RemoteSnapshotClient(StaticShardClient):
    def __init__(self):
        super().__init__()
        self.registered_manifests = []

    async def register_manifest(self, job_id, payload):
        self.registered_manifests.append((job_id, payload))
        return payload


class DistributedScanClient(RemoteSnapshotClient):
    def __init__(self, job):
        super().__init__()
        self.job = job
        self.completed_scan_units = []
        self.failed_scan_units = []

    async def get_job(self, job_id):
        return self.job

    async def complete_scan_unit(self, scan_unit_id, payload):
        self.completed_scan_units.append((scan_unit_id, payload))
        return {"id": scan_unit_id, **payload}

    async def fail_scan_unit(self, scan_unit_id, error_message, **kwargs):
        self.failed_scan_units.append((scan_unit_id, error_message, kwargs))
        return {"id": scan_unit_id, "error_message": error_message, **kwargs}


def test_run_remote_folder_snapshot_scans_on_agent_and_registers_shards(
    tmp_path, monkeypatch
):
    input_root = tmp_path / "input"
    nested = input_root / "nested"
    nested.mkdir(parents=True)
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (nested / "b.pdf").write_bytes(b"%PDF-1.4\n")
    dispatched = []

    async def fake_run_static_sharded_job(job, config, client):
        dispatched.append(job)
        return 0

    monkeypatch.setattr(runner, "run_static_sharded_job", fake_run_static_sharded_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = RemoteSnapshotClient()
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 1,
        "has_static_shards": False,
    }

    result = asyncio.run(runner.run_job(job, config, client))

    assert result == 0
    assert len(client.registered_manifests) == 1
    registered_job_id, payload = client.registered_manifests[0]
    assert registered_job_id == "job-1"
    assert payload["input_root"] == str(input_root.resolve())
    assert payload["manifest_path"].endswith("manifest.jsonl")
    assert payload["file_count"] == 2
    assert [shard["file_count"] for shard in payload["shards"]] == [1, 1]
    assert all(Path(shard["shard_path"]).exists() for shard in payload["shards"])
    assert dispatched == [job]


def test_run_remote_folder_snapshot_uses_streaming_manifest_writer(
    tmp_path, monkeypatch
):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    dispatched = []

    async def fake_run_static_sharded_job(job, config, client):
        dispatched.append(job)
        return 0

    def fail_old_full_scan(_input_dir):
        raise AssertionError("remote folder snapshot should use streaming scan")

    monkeypatch.setattr(runner, "run_static_sharded_job", fake_run_static_sharded_job)
    monkeypatch.setattr(runner, "scan_folder_snapshot", fail_old_full_scan, raising=False)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = RemoteSnapshotClient()
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 1,
        "has_static_shards": False,
    }

    result = asyncio.run(runner.run_job(job, config, client))

    assert result == 0
    assert len(client.registered_manifests) == 1
    assert client.registered_manifests[0][1]["file_count"] == 1
    assert dispatched == [job]


def test_run_remote_folder_snapshot_posts_scan_progress_events(
    tmp_path, monkeypatch
):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
    dispatched = []

    async def fake_run_static_sharded_job(job, config, client):
        dispatched.append(job)
        return 0

    monkeypatch.setattr(runner, "MANIFEST_SCAN_PROGRESS_INTERVAL_FILES", 1)
    monkeypatch.setattr(runner, "run_static_sharded_job", fake_run_static_sharded_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = RemoteSnapshotClient()
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 1,
        "has_static_shards": False,
    }

    result = asyncio.run(runner.run_job(job, config, client))

    assert result == 0
    progress_events = [
        event for _job_id, event in client.events
        if event["type"] == "manifest_scan_progress"
    ]
    assert [
        (event["payload"]["status"], event["payload"]["scanned_files"])
        for event in progress_events
    ] == [("running", 1), ("running", 2), ("done", 2)]
    assert progress_events[-1]["payload"]["status"] == "done"
    assert progress_events[-1]["payload"]["server_id"] == "server-a"
    assert dispatched == [job]


def test_run_scan_unit_writes_direct_pdf_shards_and_child_units(tmp_path):
    input_root = tmp_path / "input"
    nested = input_root / "nested"
    nested.mkdir(parents=True)
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.PDF").write_bytes(b"%PDF-1.4\n")
    (input_root / "note.txt").write_text("ignore")
    (nested / "c.pdf").write_bytes(b"%PDF-1.4\n")
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "distributed_remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 1,
    }
    unit = {
        "id": 7,
        "job_id": "job-1",
        "path": str(input_root),
        "attempt_count": 3,
    }
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = DistributedScanClient(job)

    result = asyncio.run(runner.run_scan_unit(unit, config, client))

    assert result == 0
    completed_id, payload = client.completed_scan_units[0]
    assert completed_id == 7
    assert payload["assigned_server_id"] == "server-a"
    assert payload["attempt_count"] == 3
    assert payload["file_count"] == 2
    assert payload["child_paths"] == [str(nested)]
    assert [shard["file_count"] for shard in payload["shards"]] == [1, 1]
    assert all(Path(shard["shard_path"]).exists() for shard in payload["shards"])


def test_run_scan_unit_posts_scan_progress_events(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    nested = input_root / "nested"
    nested.mkdir(parents=True)
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (input_root / "b.pdf").write_bytes(b"%PDF-1.4\n")
    (nested / "c.pdf").write_bytes(b"%PDF-1.4\n")
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "distributed_remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 1,
    }
    unit = {
        "id": 7,
        "job_id": "job-1",
        "path": str(input_root),
        "attempt_count": 3,
    }
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = DistributedScanClient(job)

    monkeypatch.setattr(runner, "MANIFEST_SCAN_PROGRESS_INTERVAL_FILES", 1)

    result = asyncio.run(runner.run_scan_unit(unit, config, client))

    assert result == 0
    progress_events = [
        event for _job_id, event in client.events
        if event["type"] == "manifest_scan_progress"
    ]
    assert [
        (event["payload"]["status"], event["payload"]["scanned_files"])
        for event in progress_events
    ] == [("running", 1), ("running", 2), ("done", 2)]
    assert progress_events[-1]["payload"]["server_id"] == "server-a"
    assert progress_events[-1]["payload"]["scan_unit_id"] == 7
    assert progress_events[-1]["payload"]["scan_unit_path"] == str(input_root)
    assert progress_events[-1]["payload"]["scanned_dirs"] == 1
    assert progress_events[-1]["payload"]["child_dir_count"] == 1


def test_run_scan_unit_samples_file_stat_errors_and_continues(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    ok_path = input_root / "ok.pdf"
    bad_path = input_root / "bad.pdf"
    ok_path.write_bytes(b"%PDF-1.4\n")
    bad_path.write_bytes(b"%PDF-1.4\n")
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "distributed_remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 10,
    }
    unit = {
        "id": 7,
        "job_id": "job-1",
        "path": str(input_root),
        "attempt_count": 3,
    }
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = DistributedScanClient(job)

    class FakeEntry:
        def __init__(self, path):
            self.path = str(path)
            self.name = path.name

        def is_dir(self, follow_symlinks=False):
            return False

        def is_file(self, follow_symlinks=False):
            return True

        def stat(self, follow_symlinks=False):
            if self.name == "bad.pdf":
                raise PermissionError("cannot stat file")
            return ok_path.stat()

    class FakeScandir:
        def __enter__(self):
            return iter([FakeEntry(ok_path), FakeEntry(bad_path)])

        def __exit__(self, exc_type, exc, traceback):
            return False

    original_scandir = runner.os.scandir

    def fake_scandir(path):
        if Path(path).resolve() == input_root.resolve():
            return FakeScandir()
        return original_scandir(path)

    monkeypatch.setattr(runner.os, "scandir", fake_scandir)

    result = asyncio.run(runner.run_scan_unit(unit, config, client))

    assert result == 0
    assert client.failed_scan_units == []
    completed_id, payload = client.completed_scan_units[0]
    assert completed_id == 7
    assert payload["file_count"] == 1
    assert payload["total_bytes"] == ok_path.stat().st_size
    meta = json.loads(Path(payload["meta_path"]).read_text(encoding="utf-8"))
    assert meta["skipped_error_count"] == 1
    assert meta["skipped_errors"] == [
        {
            "path": str(bad_path.resolve()),
            "reason": "cannot stat file",
            "failure_category": "input_invalid",
        }
    ]
    progress_events = [
        event for _job_id, event in client.events
        if event["type"] == "manifest_scan_progress"
    ]
    assert progress_events[-1]["payload"]["skipped_error_count"] == 1
    assert progress_events[-1]["payload"]["skipped_errors"] == meta["skipped_errors"]


def test_run_scan_unit_failure_reports_holder_attempt(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "distributed_remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 1,
    }
    unit = {
        "id": 9,
        "job_id": "job-1",
        "path": str(input_root),
        "attempt_count": 4,
    }
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = DistributedScanClient(job)

    def boom(**kwargs):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(runner, "_write_scan_unit_manifest_streaming", boom)

    result = asyncio.run(runner.run_scan_unit(unit, config, client))

    assert result == 1
    assert client.failed_scan_units == [
        (
            9,
            "scan exploded",
            {
                "assigned_server_id": "server-a",
                "attempt_count": 4,
                "failure_category": "parser_failed",
            },
        )
    ]
    scan_failed_events = [
        event for _job_id, event in client.events
        if event["type"] == "manifest_scan_failed"
    ]
    assert scan_failed_events == [
        {
            "type": "manifest_scan_failed",
            "payload": {
                "scan_unit_id": 9,
                "server_id": "server-a",
                "error": "scan exploded",
                "failure_category": "parser_failed",
            },
        }
    ]


def test_run_scan_unit_streams_direct_pdf_items_without_materializing(tmp_path, monkeypatch):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(3):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")
    job = {
        "id": "job-1",
        "input_dir": str(input_root),
        "output_dir": str(tmp_path / "output"),
        "engine": "dotsocr",
        "input_mode": "distributed_remote_folder_snapshot",
        "manifest_root": str(tmp_path / "manifests"),
        "target_files_per_shard": 2,
    }
    unit = {"id": 8, "job_id": "job-1", "path": str(input_root)}
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = DistributedScanClient(job)
    original_writer = runner.write_manifest_snapshot_streaming
    observed = {}

    def observing_writer(**kwargs):
        items = kwargs["items"]
        observed["items_type"] = type(items)
        assert not isinstance(items, list)
        return original_writer(**kwargs)

    monkeypatch.setattr(runner, "write_manifest_snapshot_streaming", observing_writer)

    result = asyncio.run(runner.run_scan_unit(unit, config, client))

    assert result == 0
    assert observed["items_type"].__name__ != "list"
    assert client.completed_scan_units[0][1]["file_count"] == 3


def test_run_remote_folder_snapshot_with_existing_shards_dispatches_static_job(
    tmp_path, monkeypatch
):
    dispatched = []

    async def fake_run_static_sharded_job(job, config, client):
        dispatched.append(job)
        return 0

    monkeypatch.setattr(runner, "run_static_sharded_job", fake_run_static_sharded_job)
    config = AgentConfig(
        server_id="server-b",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
    )
    client = RemoteSnapshotClient()
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "input_mode": "remote_folder_snapshot",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_job(job, config, client))

    assert result == 0
    assert dispatched == [job]
    assert client.registered_manifests == []


def test_run_static_sharded_job_claims_two_shards_without_recursive_dispatch(
    tmp_path, monkeypatch
):
    seen_jobs = []

    async def fake_run_job(job, config, client):
        seen_jobs.append(job)
        return 0

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert client.claim_calls == [
        ("job-1", "server-a"),
        ("job-1", "server-a"),
        ("job-1", "server-a"),
    ]
    assert [item["shard"]["id"] for item in seen_jobs] == [1, 2]
    assert [item["has_static_shards"] for item in seen_jobs] == [False, False]
    assert client.updates == [
        (
            1,
            {
                "status": "succeeded",
                "processed_files": 2,
                "failed_files": 0,
                "skipped_files": 0,
                "completed_pages": 0,
            },
        ),
        (
            2,
            {
                "status": "succeeded",
                "processed_files": 3,
                "failed_files": 0,
                "skipped_files": 0,
                "completed_pages": 0,
            },
        ),
    ]
    assert client.events == [
        ("job-1", {"type": "job_done", "payload": {"static_shards_final": True}})
    ]


def test_run_static_sharded_job_updates_include_holder_attempt(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        return 0

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    client.claims = [
        {
            "id": 11,
            "shard_path": "/manifest/shard-11.jsonl",
            "file_count": 2,
            "assigned_server_id": "server-a",
            "attempt_count": 5,
        },
        None,
    ]
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert client.updates[0][1]["assigned_server_id"] == "server-a"
    assert client.updates[0][1]["attempt_count"] == 5


def test_run_static_sharded_job_preserves_skipped_files_for_idempotent_shard_rerun(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        job["_shard_progress"] = {
            "status": "running",
            "processed_files": 2,
            "failed_files": 0,
            "skipped_files": 2,
            "completed_pages": 0,
        }
        return 0

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    client.claims = [
        {"id": 11, "shard_path": "/manifest/shard-11.jsonl", "file_count": 2},
        None,
    ]
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert client.updates[0] == (
        11,
        {
            "status": "succeeded",
            "processed_files": 2,
            "failed_files": 0,
            "skipped_files": 2,
            "completed_pages": 0,
        },
    )
    assert client.events == [
        ("job-1", {"type": "job_done", "payload": {"static_shards_final": True}})
    ]


def test_run_static_sharded_job_retries_transient_terminal_shard_update(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        return 0

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    class FlakyTerminalUpdateClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            if len(self.updates) < 3:
                raise httpx.ConnectError("control temporarily unavailable")
            return {"id": shard_id, **payload}

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
        control_retry_initial_seconds=0.5,
        control_retry_max_seconds=1.0,
    )
    client = FlakyTerminalUpdateClient()
    client.claims = [
        {"id": 11, "shard_path": "/manifest/shard-11.jsonl", "file_count": 2},
        None,
    ]
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert sleeps == [0.5, 1.0]
    assert client.claim_calls == [
        ("job-1", "server-a"),
        ("job-1", "server-a"),
    ]
    assert [payload["status"] for _shard_id, payload in client.updates] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert not (tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json").exists()


def test_terminal_shard_update_writes_pending_file_before_control_call(tmp_path):
    observed_pending_exists = None

    class ObservingClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            nonlocal observed_pending_exists
            observed_pending_exists = (
                tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
            ).exists()
            return await super().update_shard(shard_id, payload)

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ObservingClient()

    asyncio.run(
        runner._update_shard_with_transient_retry(
            client,
            11,
            {"status": "succeeded", "processed_files": 1},
            config,
            job_id="job-1",
        )
    )

    assert observed_pending_exists is True
    assert not (tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json").exists()


def test_replay_pending_shard_updates_removes_file_after_success(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    assert replayed == 1
    assert client.updates == [(11, {"status": "succeeded", "processed_files": 1})]
    assert not pending_path.exists()


def test_replay_pending_shard_updates_keeps_file_on_transient_failure(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )

    class StillUnavailableClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.ConnectError("control temporarily unavailable")

    client = StillUnavailableClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    assert replayed == 0
    assert client.updates == [(11, {"status": "succeeded", "processed_files": 1})]
    assert pending_path.exists()


def test_replay_pending_shard_updates_skips_records_for_other_servers(tmp_path):
    config = AgentConfig(
        server_id="server-b",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    assert replayed == 0
    assert client.updates == []
    assert pending_path.exists()


def test_replay_pending_shard_updates_quarantines_malformed_file_and_continues(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    malformed_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-10.json"
    good_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    malformed_path.parent.mkdir(parents=True)
    malformed_path.write_text('{"job_id":"job-1","shard_id":', encoding="utf-8")
    good_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    client = StaticShardClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    failed_path = malformed_path.with_suffix(".json.failed")
    assert replayed == 1
    assert client.updates == [(11, {"status": "succeeded", "processed_files": 1})]
    assert not malformed_path.exists()
    assert failed_path.exists()
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["job_id"] == "job-1"
    assert failed_record["shard_id"] == 10
    assert failed_record["raw_content"] == '{"job_id":"job-1","shard_id":'
    assert failed_record["replay_error"]["error_type"] == "JSONDecodeError"
    assert not good_path.exists()


def test_replay_pending_shard_updates_quarantines_non_transient_failure_and_continues(tmp_path):
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    first_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    second_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-12.json"
    first_path.parent.mkdir(parents=True)
    first_path.write_text(
        (
            '{"job_id":"job-1","shard_id":11,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":1}}'
        ),
        encoding="utf-8",
    )
    second_path.write_text(
        (
            '{"job_id":"job-1","shard_id":12,"server_id":"server-a",'
            '"payload":{"status":"succeeded","processed_files":2}}'
        ),
        encoding="utf-8",
    )
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    class OneConflictClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            if shard_id == 11:
                raise httpx.HTTPStatusError("stale attempt", request=request, response=response)
            return {"id": shard_id, **payload}

    client = OneConflictClient()

    replayed = asyncio.run(runner.replay_pending_shard_updates(config, client))

    failed_path = first_path.with_suffix(".json.failed")
    assert replayed == 1
    assert client.updates == [
        (11, {"status": "succeeded", "processed_files": 1}),
        (12, {"status": "succeeded", "processed_files": 2}),
    ]
    assert not first_path.exists()
    assert failed_path.exists()
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["replay_error"]["error_type"] == "HTTPStatusError"
    assert "stale attempt" in failed_record["replay_error"]["message"]
    assert not second_path.exists()


def test_terminal_shard_update_retry_does_not_hide_non_transient_control_errors(tmp_path):
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    class ConflictClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.HTTPStatusError("stale attempt", request=request, response=response)

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ConflictClient()

    async def exercise():
        with pytest.raises(httpx.HTTPStatusError):
            await runner._update_shard_with_transient_retry(
                client,
                11,
                {"status": "succeeded", "processed_files": 1},
                config,
            )

    asyncio.run(exercise())

    assert len(client.updates) == 1


def test_terminal_shard_update_quarantines_pending_file_on_non_transient_error(tmp_path):
    request = httpx.Request("POST", "http://control/api/shards/11")
    response = httpx.Response(409, request=request, json={"detail": "stale attempt"})

    class ConflictClient(StaticShardClient):
        async def update_shard(self, shard_id, payload):
            self.updates.append((shard_id, payload))
            raise httpx.HTTPStatusError("stale attempt", request=request, response=response)

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ConflictClient()

    async def exercise():
        with pytest.raises(httpx.HTTPStatusError):
            await runner._update_shard_with_transient_retry(
                client,
                11,
                {"status": "succeeded", "processed_files": 1},
                config,
                job_id="job-1",
            )

    asyncio.run(exercise())

    pending_path = tmp_path / "jobs" / "job-1" / "pending-shard-updates" / "shard-11.json"
    failed_path = pending_path.with_suffix(".json.failed")
    assert not pending_path.exists()
    failed_record = json.loads(failed_path.read_text(encoding="utf-8"))
    assert failed_record["payload"] == {"status": "succeeded", "processed_files": 1}
    assert failed_record["replay_error"]["error_type"] == "HTTPStatusError"
    assert "stale attempt" in failed_record["replay_error"]["message"]


def test_run_static_sharded_job_pauses_shard_claim_when_resource_constrained(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        return 0

    pressure_states = [
        {"constrained": True, "reasons": ["memory percent 95.0% >= 90.0%"]},
        {"constrained": False, "reasons": []},
        {"constrained": False, "reasons": []},
    ]
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    def fake_resource_pressure(config):
        return pressure_states.pop(0)

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    monkeypatch.setattr(runner, "resource_pressure", fake_resource_pressure)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
        poll_interval_seconds=7,
    )
    client = StaticShardClient()
    client.claims = [
        {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 1},
        None,
    ]
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert sleeps == [7]
    assert client.claim_calls == [
        ("job-1", "server-a"),
        ("job-1", "server-a"),
    ]
    assert client.events[0] == (
        "job-1",
        {
            "type": "resource_pressure",
            "payload": {
                "stage": "before_shard_claim",
                "server_id": "server-a",
                "pressure": {
                    "constrained": True,
                    "reasons": ["memory percent 95.0% >= 90.0%"],
                },
            },
        },
    )
    assert len(client.updates) == 1


def test_run_static_sharded_job_clears_remote_snapshot_mode_for_child_shards(
    tmp_path, monkeypatch
):
    seen_jobs = []

    async def fake_run_job(job, config, client):
        seen_jobs.append(job)
        return 0

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    job = {
        "id": "job-1",
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "input_mode": "remote_folder_snapshot",
        "has_static_shards": True,
    }

    result = asyncio.run(runner.run_static_sharded_job(job, config, client))

    assert result == 0
    assert [item["input_mode"] for item in seen_jobs] == ["folder_snapshot", "folder_snapshot"]
    assert [item["shard"]["id"] for item in seen_jobs] == [1, 2]


class StopAfterFirstShardClient(StaticShardClient):
    def __init__(self):
        super().__init__()
        self.claims = [
            {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 2},
            {"id": 2, "shard_path": "/manifest/shard-2.jsonl", "file_count": 3},
        ]
        self.get_job_calls = 0

    async def get_job(self, job_id):
        self.get_job_calls += 1
        if self.get_job_calls >= 2:
            return {"id": job_id, "stop_requested": True, "status": "stopping"}
        return {"id": job_id, "stop_requested": False, "status": "running"}


class ClaimNoneStoppingClient(StaticShardClient):
    def __init__(self):
        super().__init__()
        self.claims = [None]
        self.get_job_calls = 0

    async def get_job(self, job_id):
        self.get_job_calls += 1
        if self.get_job_calls == 1:
            return {"id": job_id, "stop_requested": False, "status": "running"}
        return {"id": job_id, "stop_requested": False, "status": "stopping"}


def test_run_static_sharded_job_emits_stopped_when_claim_none_parent_stopping(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        raise AssertionError("no shard should be dispatched")

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = ClaimNoneStoppingClient()

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == 1
    assert client.claim_calls == [("job-1", "server-a")]
    assert client.events == [
        ("job-1", {"type": "job_stopped", "payload": {"static_shards_final": True}})
    ]


def test_run_static_sharded_job_marks_current_shard_stopped_and_stops_claiming(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        return -15

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StopAfterFirstShardClient()

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == -15
    assert client.claim_calls == [("job-1", "server-a")]
    assert client.updates == [
        (
            1,
            {
                "status": "stopped",
                "processed_files": 0,
                "failed_files": 0,
                "skipped_files": 0,
                "completed_pages": 0,
                "failure_category": "operator_stopped",
            },
        )
    ]
    assert client.events == [
        (
            "job-1",
            {"type": "job_stopped", "payload": {"static_shards_final": True, "return_code": -15}},
        )
    ]


def test_run_static_sharded_job_marks_child_job_failed_exit_zero_as_failed(
    tmp_path, monkeypatch
):
    def fake_build_ocr_command(job, config):
        event_file = tmp_path / "events.jsonl"
        command = [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "path.write_text(json.dumps({"
                "'type': 'job_failed', "
                "'payload': {'error': 'one file failed'}"
                "}) + '\\n', encoding='utf-8')\n"
                "raise SystemExit(0)\n"
            ),
            str(event_file),
        ]
        return command, event_file

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StaticShardClient()
    client.claims = [
        {"id": 1, "shard_path": "/manifest/shard-1.jsonl", "file_count": 1},
        None,
    ]

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == 1
    assert client.updates[-1] == (
        1,
        {
            "status": "failed",
            "processed_files": 0,
            "failed_files": 0,
            "skipped_files": 0,
            "completed_pages": 0,
            "failure_category": "parser_failed",
            "error_message": "one file failed",
        },
    )
    assert client.events == [
        ("job-1", {"type": "job_failed", "payload": {"error": "one file failed"}}),
        (
            "job-1",
            {
                "type": "job_failed",
                "payload": {
                    "static_shards_final": True,
                    "return_code": 1,
                    "failure_category": "parser_failed",
                    "error_message": "one file failed",
                },
            },
        ),
    ]


def make_static_job_client(tmp_path, *, file_count=2):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(file_count):
        pdf = input_root / f"{index}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    api.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()
    return api, session_factory, engine, job


def test_static_shard_claim_and_update_routes(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path)

    claim_response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )
    assert claim_response.status_code == 200
    claimed = claim_response.json()
    assert claimed["shard_index"] == 1
    assert claimed["status"] == "running"
    assert claimed["assigned_server_id"] == "server-a"

    update_response = api.post(
        f"/api/shards/{claimed['id']}",
        json={"status": "succeeded", "processed_files": 1},
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "succeeded"
    assert update_response.json()["processed_files"] == 1

    next_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    assert next_claim["shard_index"] == 2


def test_eligible_agent_claims_unassigned_pool_static_job(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    api.post(
        "/api/servers/register",
        json={"id": "server-a", "name": "Server A", "host": "localhost"},
    )
    api.post(
        "/api/servers/server-a/heartbeat",
        json={
            "status": "idle",
            "capabilities": {
                "shared_paths": [
                    {
                        "path": str(tmp_path),
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                    }
                ]
            },
        },
    )
    job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()

    claimed = api.post("/api/agents/server-a/next-job").json()

    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["assigned_server_id"] is None
    assert claimed["has_static_shards"] is True
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        assert parent.assigned_server_id == POOL_SERVER_ID


def test_ineligible_agent_does_not_claim_pool_static_job(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    (input_root / "a.pdf").write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    api.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    )

    response = api.post("/api/agents/server-b/next-job")

    assert response.status_code == 200
    assert response.json() is None


def test_pool_static_job_can_be_seen_by_second_eligible_agent_while_running(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    for index in range(2):
        (input_root / f"{index}.pdf").write_bytes(b"%PDF-1.4\n")
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory)
    api = TestClient(app)
    for server_id in ["server-a", "server-b"]:
        api.post(
            "/api/servers/register",
            json={"id": server_id, "name": server_id, "host": "localhost"},
        )
        api.post(
            f"/api/servers/{server_id}/heartbeat",
            json={
                "status": "idle",
                "capabilities": {
                    "shared_paths": [
                        {
                            "path": str(tmp_path),
                            "exists": True,
                            "is_dir": True,
                            "readable": True,
                        }
                    ]
                },
            },
        )
    job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(input_root),
            "output_dir": str(tmp_path / "output"),
            "engine": "dotsocr",
            "input_mode": "folder_snapshot",
            "manifest_root": str(tmp_path / "manifests"),
            "target_files_per_shard": 1,
        },
    ).json()
    first = api.post("/api/agents/server-a/next-job").json()

    second = api.post("/api/agents/server-b/next-job").json()

    assert first["id"] == job["id"]
    assert second["id"] == job["id"]


def test_static_sharded_job_does_not_finalize_when_other_shards_are_running(
    tmp_path, monkeypatch
):
    async def fake_run_job(job, config, client):
        raise AssertionError("no shard should be dispatched")

    class RunningShardClient(StaticShardClient):
        def __init__(self):
            super().__init__()
            self.claims = [None]

        async def get_job_summary(self, job_id):
            return {
                "id": job_id,
                "pending_shards": 0,
                "running_shards": 1,
                "failed_shards": 0,
                "stopped_shards": 0,
            }

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = RunningShardClient()

    result = asyncio.run(
        runner.run_static_sharded_job(
            {
                "id": "job-1",
                "input_dir": "/shared/input",
                "output_dir": "/shared/output",
                "engine": "dotsocr",
                "has_static_shards": True,
            },
            config,
            client,
        )
    )

    assert result == 0
    assert client.events == []


def test_static_shard_claim_sets_lease_deadline(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)

    claim_response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert claim_response.status_code == 200
    claimed = claim_response.json()
    assert claimed["status"] == "running"
    assert claimed["lease_expires_at"] is not None
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.lease_expires_at is not None


def test_expired_running_shard_can_be_reclaimed_by_another_server(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    first_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        shard.lease_expires_at = service.utcnow() - timedelta(seconds=1)
        session.commit()

    second_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-b"},
    )

    assert second_claim.status_code == 200
    reclaimed = second_claim.json()
    assert reclaimed["id"] == first_claim["id"]
    assert reclaimed["assigned_server_id"] == "server-b"
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["lease_expires_at"] is not None
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        assert shard.assigned_server_id == "server-b"
        assert shard.attempt_count == 2


def test_reclaimed_expired_shard_closes_stale_attempt_history(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    first_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(
        "/api/servers/register",
        json={"id": "server-b", "name": "Server B", "host": "localhost"},
    )
    with session_factory() as session:
        shard = session.get(WorkShard, first_claim["id"])
        shard.lease_expires_at = service.utcnow() - timedelta(seconds=1)
        session.commit()

    second_claim = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-b"},
    )

    assert second_claim.status_code == 200
    attempts = api.get(
        f"/api/jobs/{job['id']}/shards/{first_claim['id']}/attempts"
    ).json()
    assert [
        (attempt["attempt_number"], attempt["server_id"], attempt["status"])
        for attempt in attempts
    ] == [
        (1, "server-a", "stale"),
        (2, "server-b", "running"),
    ]
    assert attempts[0]["finished_at"] is not None
    assert attempts[0]["failure_category"] == "lease_expired"
    assert attempts[1]["finished_at"] is None


def test_server_heartbeat_renews_running_shard_lease(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    old_deadline = service.utcnow() + timedelta(seconds=5)
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = old_deadline
        session.commit()

    heartbeat = api.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "busy", "current_job_id": job["id"]},
    )

    assert heartbeat.status_code == 200
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.lease_expires_at > old_deadline


def test_child_terminal_events_do_not_finalize_static_parent_until_final_event(tmp_path):
    api, _, _, job = make_static_job_client(tmp_path, file_count=1)
    api.post("/api/agents/server-a/next-job")

    child_done = api.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_done", "payload": {}},
    )
    assert child_done.status_code == 200
    assert child_done.json()["status"] == "running"

    final_done = api.post(
        f"/api/jobs/{job['id']}/events",
        json={"type": "job_done", "payload": {"static_shards_final": True}},
    )
    assert final_done.status_code == 200
    assert final_done.json()["status"] == "succeeded"


@pytest.mark.parametrize("status", ["stopping", "stopped", "failed", "succeeded"])
def test_claim_next_pending_shard_refuses_stopped_or_terminal_parent(tmp_path, status):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = status
        session.commit()

    response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert response.status_code == 200
    assert response.json() is None
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=job["id"]).one()
        assert shard.status == "pending"
        assert shard.assigned_server_id is None


def test_claim_next_pending_shard_refuses_stop_requested_parent(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        parent.stop_requested = True
        session.commit()

    response = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    )

    assert response.status_code == 200
    assert response.json() is None


def test_claim_next_pending_shard_does_not_claim_if_parent_stops_before_update(
    tmp_path,
):
    _, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    update_seen = False

    with session_factory() as session:
        original_execute = session.execute

        def execute_with_stop_race(statement, *args, **kwargs):
            nonlocal update_seen
            if (
                not update_seen
                and isinstance(statement, Update)
                and statement.table.name == WorkShard.__tablename__
            ):
                update_seen = True
                with session_factory() as stop_session:
                    parent = stop_session.get(Job, job["id"])
                    parent.status = "stopping"
                    parent.stop_requested = True
                    stop_session.commit()
            return original_execute(statement, *args, **kwargs)

        session.execute = execute_with_stop_race

        claimed = service.claim_next_pending_shard(session, job["id"], "server-a")

    assert update_seen is True
    assert claimed is None
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=job["id"]).one()
        parent = session.get(Job, job["id"])
        assert parent.status == "stopping"
        assert parent.stop_requested is True
        assert shard.status == "pending"
        assert shard.assigned_server_id is None
        assert shard.attempt_count == 0


def test_request_stop_stops_unclaimed_shards_and_finalizes_after_running_shard_stops(
    tmp_path,
):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=2)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        session.commit()
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()

    stop_response = api.post(f"/api/jobs/{job['id']}/request-stop")

    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "stopping"
    with session_factory() as session:
        shards = session.query(WorkShard).filter_by(job_id=job["id"]).order_by(WorkShard.shard_index).all()
        assert [shard.status for shard in shards] == ["running", "stopped"]

    update_response = api.post(
        f"/api/shards/{claimed['id']}",
        json={"status": "stopped", "processed_files": 0, "failure_category": "operator_stopped"},
    )

    assert update_response.status_code == 200
    summary = api.get(f"/api/jobs/{job['id']}/summary").json()
    assert summary["status"] == "stopped"
    assert summary["failure_category"] == "operator_stopped"
    assert summary["running_shards"] == 0
    assert summary["stopped_shards"] == 2


def test_stopping_job_finalizes_when_running_shard_lease_expires(
    tmp_path,
):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        session.commit()
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(f"/api/jobs/{job['id']}/request-stop")
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = service.utcnow() - timedelta(seconds=1)
        session.commit()

    summary = api.get(f"/api/jobs/{job['id']}/summary").json()

    assert summary["status"] == "stopped"
    assert summary["failure_category"] == "operator_stopped"
    assert summary["running_shards"] == 0
    assert summary["stopped_shards"] == 1


def test_stopping_job_lease_expiry_closes_operator_stopped_attempt_history(tmp_path):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    with session_factory() as session:
        parent = session.get(Job, job["id"])
        parent.status = "running"
        session.commit()
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()
    api.post(f"/api/jobs/{job['id']}/request-stop")
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        shard.lease_expires_at = service.utcnow() - timedelta(seconds=1)
        session.commit()

    summary = api.get(f"/api/jobs/{job['id']}/summary")

    assert summary.status_code == 200
    attempts = api.get(
        f"/api/jobs/{job['id']}/shards/{claimed['id']}/attempts"
    ).json()
    assert [
        (attempt["attempt_number"], attempt["server_id"], attempt["status"])
        for attempt in attempts
    ] == [(1, "server-a", "stopped")]
    assert attempts[0]["failure_category"] == "operator_stopped"
    assert attempts[0]["finished_at"] is not None


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "exploded", "processed_files": 1},
        {"status": "succeeded", "processed_files": -1},
    ],
)
def test_invalid_shard_update_is_rejected_and_not_persisted(tmp_path, payload):
    api, session_factory, _, job = make_static_job_client(tmp_path, file_count=1)
    claimed = api.post(
        f"/api/jobs/{job['id']}/shards/claim",
        params={"server_id": "server-a"},
    ).json()

    response = api.post(f"/api/shards/{claimed['id']}", json=payload)

    assert response.status_code in {400, 422}
    with session_factory() as session:
        shard = session.get(WorkShard, claimed["id"])
        assert shard.status == "running"
        assert shard.processed_files == 0


def test_has_static_shards_response_uses_count_not_relationship_load(tmp_path):
    api, _, engine, static_job = make_static_job_client(tmp_path, file_count=2)
    directory_job = api.post(
        "/api/jobs",
        json={
            "input_dir": str(tmp_path / "directory-input"),
            "output_dir": str(tmp_path / "directory-output"),
            "engine": "dotsocr",
            "assigned_server_id": "server-a",
        },
    ).json()
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        static_response = api.get(f"/api/jobs/{static_job['id']}").json()
        directory_response = api.get(f"/api/jobs/{directory_job['id']}").json()
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert static_response["has_static_shards"] is True
    assert directory_response["has_static_shards"] is False
    assert not any("SELECT work_shards.id" in statement for statement in statements)


def test_control_client_claims_and_updates_shards(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/shards/claim"):
            assert request.url.params["server_id"] == "server-a"
            return httpx.Response(200, json={"id": 1, "shard_path": "/s.jsonl"})
        return httpx.Response(200, json={"id": 1, "status": "succeeded"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080", "server-a")

    async def exercise():
        try:
            claimed = await client.claim_shard("job-1", "server-a")
            updated = await client.update_shard(1, {"status": "succeeded"})
            return claimed, updated
        finally:
            await client.close()

    claimed, updated = asyncio.run(exercise())

    assert claimed == {"id": 1, "shard_path": "/s.jsonl"}
    assert updated == {"id": 1, "status": "succeeded"}
    assert requests[0].url.path == "/api/jobs/job-1/shards/claim"
    assert requests[1].url.path == "/api/shards/1"


def test_control_client_fail_scan_unit_sends_failure_category(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 7, "status": "failed"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport)

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient("http://control:8080", "server-a")

    async def exercise():
        try:
            return await client.fail_scan_unit(
                7,
                "scan exploded",
                assigned_server_id="server-a",
                attempt_count=3,
                failure_category="parser_failed",
            )
        finally:
            await client.close()

    response = asyncio.run(exercise())

    assert response == {"id": 7, "status": "failed"}
    assert requests[0].url.path == "/api/scan-units/7/fail"
    assert json.loads(requests[0].content) == {
        "error_message": "scan exploded",
        "assigned_server_id": "server-a",
        "attempt_count": 3,
        "failure_category": "parser_failed",
    }
