from types import SimpleNamespace

import pytest

from ocr_platform.control import scheduling
from ocr_platform.control.domains.common import ShardAttemptConflictError
from ocr_platform.control.schemas import WorkShardUpdateRequest


def _shard(*, status: str = "running"):
    return SimpleNamespace(
        id=17,
        status=status,
        assigned_server_id="server-a",
        attempt_count=1,
        processed_files=4,
        failed_files=2,
        skipped_files=1,
        completed_pages=8,
        api_inflight=3,
        api_inflight_peak=5,
        api_waiting=1,
        oldest_api_inflight=0.5,
        execution_paused=False,
        api_concurrency_limit=4,
        execution_control_reason=None,
        failure_category=None,
        error_message=None,
        finished_at=None,
        lease_expires_at=object(),
    )


def _attempt():
    return SimpleNamespace(
        status="running",
        processed_files=4,
        failed_files=2,
        skipped_files=1,
        completed_pages=8,
        execution_paused=False,
        api_concurrency_limit=4,
        execution_control_reason=None,
        failure_category=None,
        error_message=None,
        finished_at=None,
    )


@pytest.mark.parametrize(
    ("assigned_server_id", "attempt_count", "message"),
    [
        ("wrong-server", 1, "different server attempt"),
        ("server-a", 0, "stale attempt"),
    ],
)
def test_work_shard_policy_fences_before_terminal_replay(
    assigned_server_id,
    attempt_count,
    message,
) -> None:
    shard = _shard(status="succeeded")

    with pytest.raises(ShardAttemptConflictError, match=message):
        scheduling.apply_work_shard_update(
            object(),
            shard=shard,
            job=None,
            request=WorkShardUpdateRequest(
                status="running",
                assigned_server_id=assigned_server_id,
                attempt_count=attempt_count,
                processed_files=999,
            ),
        )

    assert shard.status == "succeeded"
    assert shard.processed_files == 4


@pytest.mark.parametrize("status", ["retrying", "stale"])
def test_work_shard_policy_preserves_reclaimable_noop(
    monkeypatch,
    status,
) -> None:
    shard = _shard(status=status)
    monkeypatch.setattr(
        scheduling,
        "_latest_current_shard_attempt",
        lambda *args, **kwargs: pytest.fail(
            "no-op update must not project an attempt"
        ),
    )

    updated = scheduling.apply_work_shard_update(
        object(),
        shard=shard,
        job=None,
        request=WorkShardUpdateRequest(
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            processed_files=999,
        ),
    )

    assert updated is shard
    assert shard.status == status
    assert shard.processed_files == 4


def test_work_shard_policy_terminal_replay_is_idempotent(
    monkeypatch,
) -> None:
    shard = _shard(status="failed")
    job = SimpleNamespace()
    finalizations = []
    monkeypatch.setattr(
        scheduling,
        "_latest_current_shard_attempt",
        lambda *args, **kwargs: pytest.fail(
            "terminal replay must not project an attempt"
        ),
    )
    monkeypatch.setattr(
        scheduling,
        "_finalize_job_after_shard_change",
        lambda *args, **kwargs: finalizations.append((args, kwargs)),
    )

    updated = scheduling.apply_work_shard_update(
        object(),
        shard=shard,
        job=job,
        request=WorkShardUpdateRequest(
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            processed_files=999,
        ),
    )

    assert updated is shard
    assert shard.status == "failed"
    assert shard.processed_files == 4
    assert len(finalizations) == 1


@pytest.mark.parametrize(
    ("max_attempts", "expected_status", "expected_finalizations"),
    [
        (2, "retrying", 0),
        (1, "failed", 1),
    ],
)
def test_work_shard_policy_projects_retry_and_sparse_fields(
    monkeypatch,
    max_attempts,
    expected_status,
    expected_finalizations,
) -> None:
    shard = _shard()
    attempt = _attempt()
    job = SimpleNamespace(max_shard_attempts=max_attempts)
    finalizations = []
    monkeypatch.setattr(
        scheduling,
        "_latest_current_shard_attempt",
        lambda session, current_shard: attempt,
    )
    monkeypatch.setattr(
        scheduling,
        "_finalize_job_after_shard_change",
        lambda *args, **kwargs: finalizations.append((args, kwargs)),
    )

    updated = scheduling.apply_work_shard_update(
        object(),
        shard=shard,
        job=job,
        request=WorkShardUpdateRequest(
            status="failed",
            assigned_server_id="server-a",
            attempt_count=1,
            completed_pages=0,
            failure_category="model_error",
            error_message="OCR failed",
        ),
    )

    assert updated.status == expected_status
    assert updated.processed_files == 4
    assert updated.completed_pages == 0
    assert updated.failure_category == "model_error"
    assert updated.error_message == "OCR failed"
    assert updated.lease_expires_at is None
    assert attempt.status == expected_status
    assert attempt.processed_files == 4
    assert attempt.completed_pages == 0
    assert attempt.failure_category == "model_error"
    assert attempt.error_message == "OCR failed"
    assert attempt.finished_at is not None
    assert len(finalizations) == expected_finalizations
