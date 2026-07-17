from __future__ import annotations

import pytest

from ocr_parser.contracts import EngineExecutionTrace, FallbackInfo, StageOutcome
from ocr_parser.contracts.execution import aggregate_execution_metadata


def test_execution_trace_serializes_stable_shape():
    trace = EngineExecutionTrace(
        stages=(StageOutcome(stage="layout", status="success", duration_seconds=0.125),),
        fallback=FallbackInfo(),
    )

    assert trace.to_dict() == {
        "stages": [
            {"stage": "layout", "status": "success", "duration_seconds": 0.125}
        ],
        "fallback": {"used": False, "reason": None, "source_stage": None},
    }


def test_used_fallback_requires_bounded_reason_and_source_stage():
    with pytest.raises(ValueError, match="requires reason and source_stage"):
        FallbackInfo(used=True)


def test_document_trace_aggregates_multiple_fallback_categories():
    metadata = aggregate_execution_metadata(
        [
            {
                "page_no": 1,
                "stages": [{"stage": "layout", "status": "failed"}],
                "fallback": {
                    "used": True,
                    "reason": "layout_empty",
                    "source_stage": "layout",
                },
            },
            {
                "page_no": 2,
                "stages": [{"stage": "text_fallback", "status": "success"}],
                "fallback": {
                    "used": True,
                    "reason": "primary_stage_failed",
                    "source_stage": "postprocess",
                },
            },
        ]
    )

    assert metadata["fallback"] == {
        "used": True,
        "reason": "multiple",
        "source_stage": "multiple",
    }
    assert metadata["stages"][0]["page_no"] == 1
    assert metadata["stages"][1]["page_no"] == 2
