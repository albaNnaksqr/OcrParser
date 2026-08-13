import asyncio

from PIL import Image

from ocr_parser.engines import dotsocr


class _Monitor:
    def __init__(self):
        self.retries = []
        self.errors = []

    def record_retry(self, count):
        self.retries.append(count)

    def record_error(self, name):
        self.errors.append(name)


class _Parser:
    NonStandardModelOutputError = type(
        "NonStandardModelOutputError",
        (Exception,),
        {},
    )
    min_pixels = None
    max_pixels = None
    save_page_layout = False
    generate_origin_md = False
    filter_keywords = []
    categories_to_filter = []
    trim_first_page_summary = False
    enable_table_reparse = False
    _table_reparse_stack = 0
    badcase_collection_dir = None
    postprocess_semaphore = None

    def __init__(self, outcomes, *, max_retries=1, concurrent_retries=0):
        self.outcomes = list(outcomes)
        self.max_retries = max_retries
        self.concurrent_retries = concurrent_retries
        self.monitor = _Monitor()
        self.inference_calls = []
        self.console = []
        self.api_errors = []
        self.refine_calls = []
        self.saved = []

    def get_prompt(self, mode, **kwargs):
        return {"mode": mode, **kwargs}

    async def _run_inference_with_retries(self, payload, prompt, page, **kwargs):
        self.inference_calls.append((payload, prompt, page, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def _validate_cells_structure(self, cells):
        if cells and cells[0].get("malformed"):
            raise self.NonStandardModelOutputError("missing bbox")

    async def _maybe_refine_table_blocks(self, **kwargs):
        self.refine_calls.append(kwargs)

    async def _save_intermediate_outputs_async(self, *args, **kwargs):
        self.saved.append((args, kwargs))
        return "/tmp/page.json", "/tmp/page.jpg"

    def _console_write(self, message, level):
        self.console.append((level, message))

    def record_api_error(self, error, stage):
        self.api_errors.append((stage, type(error).__name__))

    def _merge_adjacent_text_blocks_in_same_page(self, cells):
        return cells


def _page_data(tmp_path):
    image = Image.new("RGB", (64, 64), "white")
    return {
        "page_idx": 0,
        "original_page_num": 1,
        "filename": "fixture.pdf",
        "save_dir": str(tmp_path),
        "prompt_mode": "prompt_layout_all_en",
        "origin_image": image,
        "processed_image": image,
        "processed_size": image.size,
    }


def test_process_page_success_preserves_artifacts_and_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dotsocr,
        "post_process_output",
        lambda *_args, **_kwargs: ([{"category": "Text", "text": "ok"}], None),
    )
    parser = _Parser([("raw", 1)])

    result = asyncio.run(dotsocr.DotsOCREngine(parser).process_page(_page_data(tmp_path)))

    assert result.status == "success"
    assert result.page_json_path == "/tmp/page.json"
    assert result.page_layout_path == "/tmp/page.jpg"
    assert [stage.stage for stage in result.execution_trace.stages] == [
        "primary_inference",
        "postprocess",
    ]
    assert result.execution_trace.fallback.used is False
    assert len(parser.inference_calls) == 1
    assert len(parser.saved) == 1


def test_malformed_primary_uses_remaining_budget_for_concurrent_retry(
    tmp_path,
    monkeypatch,
):
    responses = iter(
        [
            [{"malformed": True}],
            [{"category": "Text", "text": "ok"}],
        ]
    )
    monkeypatch.setattr(
        dotsocr,
        "post_process_output",
        lambda *_args, **_kwargs: (next(responses), None),
    )
    parser = _Parser(
        [("bad", 1), ("good", 1)],
        max_retries=3,
        concurrent_retries=2,
    )

    result = asyncio.run(dotsocr.DotsOCREngine(parser).process_page(_page_data(tmp_path)))

    assert result.status == "success"
    assert [call[3]["use_race"] for call in parser.inference_calls] == [False, True]
    assert parser.inference_calls[1][3]["race_attempts"] == 2
    assert parser.monitor.retries == [2]
    assert parser.api_errors == [("model_output", "NonStandardModelOutputError")]


def test_table_refinement_failure_is_nonfatal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dotsocr,
        "post_process_output",
        lambda *_args, **_kwargs: ([{"category": "Table", "text": "x"}], None),
    )
    parser = _Parser([("raw", 1)])
    parser.enable_table_reparse = True

    async def fail_refinement(**kwargs):
        parser.refine_calls.append(kwargs)
        raise RuntimeError("refine failed")

    parser._maybe_refine_table_blocks = fail_refinement

    result = asyncio.run(dotsocr.DotsOCREngine(parser).process_page(_page_data(tmp_path)))

    assert result.status == "success"
    assert len(parser.refine_calls) == 1
    assert any("Table reparse encountered an error" in line for _, line in parser.console)


def test_postprocess_failure_uses_text_fallback_with_structured_trace(
    tmp_path,
    monkeypatch,
):
    calls = 0

    def postprocess(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("broken layout")
        return ([{"category": "Text", "text": "fallback"}], None)

    monkeypatch.setattr(dotsocr, "post_process_output", postprocess)
    monkeypatch.setattr(
        dotsocr,
        "layoutjson2md_simple_extract",
        lambda *_args, **_kwargs: "fallback markdown",
    )
    parser = _Parser([("raw", 1)])

    result = asyncio.run(dotsocr.DotsOCREngine(parser).process_page(_page_data(tmp_path)))

    assert result.status == "success_fallback_text"
    assert result.md_content == "fallback markdown"
    assert [stage.status for stage in result.execution_trace.stages] == [
        "success",
        "failed",
        "success",
    ]
    assert result.execution_trace.fallback.source_stage == "postprocess"
    assert parser.api_errors == [("model_output", "ValueError")]


def test_inference_failure_uses_image_fallback_and_preserves_failure_source(tmp_path):
    parser = _Parser([TimeoutError("timed out")])

    result = asyncio.run(dotsocr.DotsOCREngine(parser).process_page(_page_data(tmp_path)))

    assert result.status == "success_fallback_image"
    assert result.md_content == "![page_1_bad.jpg](images/page_1_bad.jpg)"
    assert (tmp_path / "images" / "page_1_bad.jpg").exists()
    assert [stage.status for stage in result.execution_trace.stages] == [
        "failed",
        "skipped",
        "failed",
        "success",
    ]
    assert result.execution_trace.fallback.source_stage == "text_fallback"
