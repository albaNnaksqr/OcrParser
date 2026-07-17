from argparse import Namespace
import json
import urllib.error

from tools import run_distributed_walkthrough


def test_walkthrough_tool_builds_distributed_mock_ocr_job_payload(tmp_path):
    args = Namespace(
        shared_root=str(tmp_path / "shared"),
        worker_id="local-worker-01",
        engine="dotsocr",
        ocr_host="127.0.0.1",
        ocr_port=18000,
        model_name="mock-ocr",
    )

    payload = run_distributed_walkthrough.build_job_payload(args)

    assert payload["input_mode"] == "distributed_remote_folder_snapshot"
    assert payload["input_dir"] == str(tmp_path / "shared" / "input")
    assert payload["output_dir"] == str(tmp_path / "shared" / "output")
    assert payload["manifest_root"] == str(tmp_path / "shared" / "manifests")
    assert payload["allowed_server_ids"] == ["local-worker-01"]
    assert payload["engine"] == "dotsocr"
    assert payload["ip"] == "127.0.0.1"
    assert payload["port"] == 18000
    assert payload["model_name"] == "mock-ocr"
    assert payload["extra_args"]["api_concurrency_max"] == 1
    assert payload["extra_args"]["no_warmup"] is True


def test_walkthrough_tool_compacts_job_summary_for_operator_logs():
    compact = run_distributed_walkthrough.compact_summary(
        {
            "status": "running",
            "lifecycle_stage": "running",
            "scan_status": "done",
            "completed_files": 0,
            "failed_files": 0,
            "total_files": 1,
            "pending_shards": 0,
            "running_shards": 1,
            "succeeded_shards": 0,
            "failed_shards": 0,
            "total_shards": 1,
            "pending_scan_units": 0,
            "running_scan_units": 0,
            "succeeded_scan_units": 1,
            "failed_scan_units": 0,
            "total_scan_units": 1,
            "worker_shards": [{"server_id": "local-worker-01"}],
            "attention_shards": [{"shard_index": 1}],
            "last_event_at": "2026-06-10T00:00:00Z",
        },
        3,
    )

    assert compact["poll"] == 3
    assert compact["files"] == [0, 0, 1]
    assert compact["shards"] == [0, 1, 0, 0, 1]
    assert compact["scan_units"] == [0, 0, 1, 0, 1]
    assert compact["worker_shards"] == [{"server_id": "local-worker-01"}]


def test_walkthrough_tool_can_reference_runtime_api_key_env_var(tmp_path):
    args = Namespace(
        shared_root=str(tmp_path / "shared"),
        worker_id="local-worker-01",
        engine="dotsocr",
        ocr_host="127.0.0.1",
        ocr_port=18000,
        model_name="mock-ocr",
        api_key_env_var="OCR_JOB_DOTSOCR_API_KEY",
    )

    payload = run_distributed_walkthrough.build_job_payload(args)

    assert payload["extra_args"]["api_key_env_var"] == "OCR_JOB_DOTSOCR_API_KEY"


def test_walkthrough_tool_builds_existing_manifest_payload_for_one_worker(tmp_path):
    args = Namespace(
        shared_root=str(tmp_path / "shared"),
        worker_id="worker-a",
        allowed_worker_id=["worker-a", "worker-b"],
        input_mode="existing_manifest",
        target_files_per_shard=10,
        max_shard_attempts=3,
        engine="dotsocr",
        ocr_host="127.0.0.1",
        ocr_port=18000,
        model_name="mock-ocr",
    )

    payload = run_distributed_walkthrough.build_job_payload(args)

    assert payload["input_mode"] == "existing_manifest"
    assert payload["assigned_server_id"] == "worker-a"
    assert "allowed_server_ids" not in payload
    assert payload["manifest_path"] == str(tmp_path / "shared" / "source-manifest.jsonl")
    assert payload["target_files_per_shard"] == 10
    assert payload["max_shard_attempts"] == 3


def test_walkthrough_tool_generates_public_batch_and_manifest(tmp_path):
    input_dir = tmp_path / "input"
    paths = run_distributed_walkthrough.create_sample_set(
        input_dir,
        pdf_name="sample.pdf",
        document_count=3,
    )
    manifest = tmp_path / "manifest.jsonl"
    run_distributed_walkthrough.write_existing_manifest(
        manifest,
        input_dir=input_dir,
        pdf_paths=paths,
    )

    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert [row["relative_path"] for row in rows] == [
        "sample.pdf",
        "sample-00002.pdf",
        "sample-00003.pdf",
    ]
    assert all(row["size_bytes"] > 0 for row in rows)


def test_walkthrough_polling_recovers_after_control_outage(tmp_path, monkeypatch, capsys):
    responses = iter(
        [
            {"id": "job-1"},
            urllib.error.URLError(OSError("control stopped")),
            {"status": "succeeded", "lifecycle_stage": "completed"},
        ]
    )

    def fake_request_json(**_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(run_distributed_walkthrough, "request_json", fake_request_json)
    monkeypatch.setattr(run_distributed_walkthrough.time, "sleep", lambda _seconds: None)
    args = Namespace(
        shared_root=str(tmp_path / "shared"),
        pdf_name="sample.pdf",
        document_count=1,
        input_mode="directory",
        worker_id="worker-a",
        allowed_worker_id=[],
        target_files_per_shard=1,
        max_shard_attempts=3,
        engine="dotsocr",
        ocr_host="127.0.0.1",
        ocr_port=18000,
        model_name="mock-ocr",
        disable_process_pool=True,
        api_key_env_var=None,
        control_url="http://127.0.0.1:38080",
        api_token="runtime-only",
        polls=3,
        interval=0.01,
    )

    assert run_distributed_walkthrough.run_walkthrough(args) == 0
    output = capsys.readouterr().out
    assert "CONTROL_UNAVAILABLE OSError" in output
    assert "CONTROL_RECOVERED" in output
    assert 'FINAL_SUMMARY {"lifecycle_stage": "completed", "status": "succeeded"}' in output
