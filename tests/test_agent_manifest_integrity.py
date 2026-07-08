import json

from ocr_platform.agent.manifest_integrity import build_worker_manifest_integrity_report
from ocr_platform.manifest.models import ManifestItem


def _write_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(item.to_json_line() for item in items) + "\n",
        encoding="utf-8",
    )


def test_worker_manifest_integrity_report_accepts_matching_manifest_and_shards(tmp_path):
    manifest_path = tmp_path / "manifest.jsonl"
    meta_path = tmp_path / "manifest.meta.json"
    shard_path = tmp_path / "shards" / "shard-000001.jsonl"
    items = [
        ManifestItem(
            input_path="/shared/input/a.pdf",
            relative_path="a.pdf",
            size_bytes=10,
            mtime_ns=1,
        ),
        ManifestItem(
            input_path="/shared/input/b.pdf",
            relative_path="b.pdf",
            size_bytes=20,
            mtime_ns=2,
        ),
    ]
    _write_jsonl(manifest_path, items)
    _write_jsonl(shard_path, items)
    meta_path.write_text(json.dumps({"file_count": 2, "total_bytes": 30}), encoding="utf-8")

    report = build_worker_manifest_integrity_report(
        {
            "job_id": "job-1",
            "manifest_id": 7,
            "manifest_path": str(manifest_path),
            "meta_path": str(meta_path),
            "manifest_expected_file_count": 2,
            "manifest_expected_total_bytes": 30,
            "shards": [
                {
                    "shard_id": 11,
                    "shard_index": 1,
                    "shard_path": str(shard_path),
                    "expected_file_count": 2,
                }
            ],
        }
    )

    assert report["ok"] is True
    assert report["status"] == "ok"
    assert report["manifest_file_exists"] is True
    assert report["manifest_file_count_matches"] is True
    assert report["manifest_total_bytes_matches"] is True
    assert report["meta_file_count_matches"] is True
    assert report["shard_file_count_matches_manifest"] is True
    assert report["bad_shard_count"] == 0


def test_worker_manifest_integrity_report_rejects_missing_shard_file(tmp_path):
    manifest_path = tmp_path / "manifest.jsonl"
    item = ManifestItem(
        input_path="/shared/input/a.pdf",
        relative_path="a.pdf",
        size_bytes=10,
        mtime_ns=1,
    )
    _write_jsonl(manifest_path, [item])

    report = build_worker_manifest_integrity_report(
        {
            "job_id": "job-1",
            "manifest_id": 7,
            "manifest_path": str(manifest_path),
            "meta_path": None,
            "manifest_expected_file_count": 1,
            "manifest_expected_total_bytes": 10,
            "shards": [
                {
                    "shard_id": 11,
                    "shard_index": 1,
                    "shard_path": str(tmp_path / "missing.jsonl"),
                    "expected_file_count": 1,
                }
            ],
        }
    )

    assert report["ok"] is False
    assert report["status"] == "failed"
    assert report["bad_shard_count"] == 1
    assert report["bad_shards"][0]["reason"] == "file_missing"
