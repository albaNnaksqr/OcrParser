import asyncio
import sys
import threading
import time
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

fitz_stub = ModuleType("fitz")
fitz_stub.Document = object
fitz_stub.Matrix = lambda *args, **kwargs: object()
fitz_stub.open = lambda *args, **kwargs: None
sys.modules.setdefault("fitz", fitz_stub)

cv2_stub = ModuleType("cv2")
cv2_stub.COLOR_RGB2GRAY = 0
cv2_stub.cvtColor = lambda image, *_args, **_kwargs: image
sys.modules.setdefault("cv2", cv2_stub)

requests_stub = ModuleType("requests")
sys.modules.setdefault("requests", requests_stub)

json_repair_stub = ModuleType("json_repair")
json_repair_stub.loads = lambda value, *args, **kwargs: __import__("json").loads(value)
sys.modules.setdefault("json_repair", json_repair_stub)

aiofiles_stub = ModuleType("aiofiles")
sys.modules.setdefault("aiofiles", aiofiles_stub)

prometheus_stub = ModuleType("prometheus_client")


class _MetricStub:
    def __init__(self, *args, **kwargs):
        pass

    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        return None

    def dec(self, *args, **kwargs):
        return None

    def observe(self, *args, **kwargs):
        return None


prometheus_stub.Counter = _MetricStub
prometheus_stub.Gauge = _MetricStub
prometheus_stub.Histogram = _MetricStub
prometheus_stub.start_http_server = lambda *args, **kwargs: None
sys.modules.setdefault("prometheus_client", prometheus_stub)

from dots_ocr.model import inference_async
from ocr_parser import runtime as runtime_ops
from ocr_parser.engines import dotsocr as dotsocr_engine
from ocr_parser.engines.api import create_chat_completion
from ocr_parser.engines.native_openai import NativeOpenAIEngine
from ocr_parser.engines.paddleocr_vl import PaddleOCRVLEngine
from ocr_parser.pipeline import document_parser


@pytest.fixture(autouse=True)
def ensure_current_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class FakeMonitor:
    def __init__(self):
        self.inference_times = []
        self.errors = []

    def record_inference_time(self, value):
        self.inference_times.append(value)

    def record_error(self, value):
        self.errors.append(value)

    def record_retry(self, _):
        pass


class FakeRuntimeParser:
    model_name = "test-model"
    ip = "127.0.0.1"
    port = 8000
    temperature = 0.1
    top_p = 0.9
    max_completion_tokens = 128
    timeout = 30.0
    max_retries = 7
    retry_delay = 0.0
    client = object()
    monitor = FakeMonitor()


class FakeRuntimeParserWithApiLane(FakeRuntimeParser):
    def __init__(self):
        self.api_semaphore = asyncio.Semaphore(1)
        self.monitor = FakeMonitor()

    def _console_write(self, *_args, **_kwargs):
        pass

    async def _inference_with_vllm(self, image, prompt):
        return await runtime_ops._inference_with_vllm(self, image, prompt)


def test_resizable_async_limiter_can_raise_limit_while_tasks_wait():
    async def run_limiter():
        limiter = runtime_ops.ResizableAsyncLimiter(1)
        order = []

        async def worker(name):
            async with limiter:
                order.append(f"{name}:start")
                await asyncio.sleep(0.02)
                order.append(f"{name}:end")

        first = asyncio.create_task(worker("a"))
        await asyncio.sleep(0)
        second = asyncio.create_task(worker("b"))
        await asyncio.sleep(0.005)
        assert limiter.inflight == 1
        assert limiter.limit == 1

        await limiter.resize(2)
        await asyncio.gather(first, second)
        return order, limiter.limit, limiter.inflight

    order, limit, inflight = asyncio.run(run_limiter())

    assert order[:2] == ["a:start", "b:start"]
    assert limit == 2
    assert inflight == 0


def test_dotsocr_inner_vllm_call_disables_library_retries(monkeypatch):
    """The parser owns retries; the low-level OpenAI call should not retry again."""
    observed = {}

    async def fake_inference_with_vllm(*args, **kwargs):
        observed.update(kwargs)
        return "[]"

    monkeypatch.setattr(inference_async, "inference_with_vllm", fake_inference_with_vllm)

    result = asyncio.run(runtime_ops._inference_with_vllm(FakeRuntimeParser(), b"img", "prompt"))

    assert result == "[]"
    assert observed["max_retries"] == 0
    assert observed["retry_delay"] == 0.0


def test_dotsocr_race_retries_obey_shared_api_semaphore(monkeypatch):
    active = 0
    max_active = 0

    async def fake_inference_with_vllm(*args, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "[]"

    monkeypatch.setattr(inference_async, "inference_with_vllm", fake_inference_with_vllm)

    async def run_race():
        parser = FakeRuntimeParserWithApiLane()
        return await runtime_ops._race_inference_attempts(parser, b"img", "prompt", 2)

    result = asyncio.run(run_race())

    assert result == "[]"
    assert max_active == 1


def test_dotsocr_payload_encoding_obeys_encode_semaphore(monkeypatch):
    active = 0
    max_active = 0

    async def fake_prepare_image_payload_for_vllm(image):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "data:image/jpeg;base64,encoded", 0.01

    async def fake_inference_with_vllm(*args, **kwargs):
        return "[]"

    class Parser(FakeRuntimeParser):
        def __init__(self):
            self.encode_semaphore = asyncio.Semaphore(1)
            self.api_semaphore = asyncio.Semaphore(4)
            self.monitor = FakeMonitor()

    monkeypatch.setattr(inference_async, "prepare_image_payload_for_vllm", fake_prepare_image_payload_for_vllm)
    monkeypatch.setattr(inference_async, "inference_with_vllm", fake_inference_with_vllm)

    async def run_two():
        parser = Parser()
        return await asyncio.gather(
            runtime_ops._inference_with_vllm(parser, "/tmp/a.jpg", "prompt"),
            runtime_ops._inference_with_vllm(parser, "/tmp/b.jpg", "prompt"),
        )

    assert asyncio.run(run_two()) == ["[]", "[]"]
    assert max_active == 1


def test_dotsocr_inference_records_global_api_lane_snapshot(monkeypatch):
    async def fake_inference_with_vllm(*args, **kwargs):
        await asyncio.sleep(0.01)
        return "[]"

    class Parser(FakeRuntimeParser):
        def __init__(self):
            self.api_concurrency = 1
            self.api_semaphore = asyncio.Semaphore(1)
            self.monitor = FakeMonitor()

    monkeypatch.setattr(inference_async, "inference_with_vllm", fake_inference_with_vllm)

    async def run_two():
        parser = Parser()
        results = await asyncio.gather(
            runtime_ops._inference_with_vllm(parser, b"a", "prompt"),
            runtime_ops._inference_with_vllm(parser, b"b", "prompt"),
        )
        return results, runtime_ops.get_runtime_snapshot(parser)

    results, snapshot = asyncio.run(run_two())
    assert results == ["[]", "[]"]
    assert snapshot["api_limit"] == 1
    assert snapshot["api_inflight_peak"] == 1
    assert snapshot["api_call_count"] == 2
    assert snapshot["api_wait_seconds_total"] > 0


def test_dotsocr_inference_uses_resizable_api_limiter_snapshot(monkeypatch):
    async def fake_inference_with_vllm(*args, **kwargs):
        await asyncio.sleep(0.01)
        return "[]"

    class Parser(FakeRuntimeParser):
        def __init__(self):
            self.api_concurrency_start = 1
            self.api_concurrency_max = 4
            self.api_limiter = runtime_ops.ResizableAsyncLimiter(1)
            self.monitor = FakeMonitor()

    monkeypatch.setattr(inference_async, "inference_with_vllm", fake_inference_with_vllm)

    async def run_batches():
        parser = Parser()
        first = await asyncio.gather(
            runtime_ops._inference_with_vllm(parser, b"a", "prompt"),
            runtime_ops._inference_with_vllm(parser, b"b", "prompt"),
        )
        await parser.api_limiter.resize(2)
        second = await asyncio.gather(
            runtime_ops._inference_with_vllm(parser, b"c", "prompt"),
            runtime_ops._inference_with_vllm(parser, b"d", "prompt"),
        )
        return first, second, runtime_ops.get_runtime_snapshot(parser)

    first, second, snapshot = asyncio.run(run_batches())

    assert first == ["[]", "[]"]
    assert second == ["[]", "[]"]
    assert snapshot["api_limit"] == 2
    assert snapshot["api_limit_start"] == 1
    assert snapshot["api_limit_max"] == 4
    assert snapshot["api_inflight_peak"] == 2
    assert snapshot["api_call_count"] == 4


def test_api_autotune_raises_limit_when_api_lanes_are_saturated():
    async def run_autotune():
        class Parser:
            enable_api_autotune = True
            api_concurrency_start = 80
            api_concurrency_max = 240
            api_autotune_last_error_count = 0
            api_autotune_last_timeout_count = 0

            def __init__(self):
                self.api_limiter = runtime_ops.ResizableAsyncLimiter(80)
                self._api_inflight = 80
                self._api_inflight_peak = 80
                self._api_waiting = 2
                self._api_call_count = 100
                self._api_wait_seconds_total = 1.0
                self._api_error_count = 0
                self._api_timeout_count = 0

        parser = Parser()
        return await runtime_ops.autotune_api_concurrency(parser)

    result = asyncio.run(run_autotune())

    assert result["changed"] is True
    assert result["old_limit"] == 80
    assert result["new_limit"] == 100
    assert result["reason"] == "saturated"


def test_api_autotune_reduces_limit_when_new_timeouts_appear():
    async def run_autotune():
        class Parser:
            enable_api_autotune = True
            api_concurrency_start = 80
            api_concurrency_max = 240
            api_autotune_last_error_count = 0
            api_autotune_last_timeout_count = 0

            def __init__(self):
                self.api_limiter = runtime_ops.ResizableAsyncLimiter(160)
                self._api_inflight = 120
                self._api_inflight_peak = 160
                self._api_waiting = 0
                self._api_call_count = 100
                self._api_wait_seconds_total = 1.0
                self._api_error_count = 3
                self._api_timeout_count = 2

        parser = Parser()
        return await runtime_ops.autotune_api_concurrency(parser)

    result = asyncio.run(run_autotune())

    assert result["changed"] is True
    assert result["old_limit"] == 160
    assert result["new_limit"] == 120
    assert result["reason"] == "errors"


def test_api_lane_classifies_http_status_network_and_timeout_errors():
    async def run_errors():
        class Parser:
            def __init__(self):
                self.api_limiter = runtime_ops.ResizableAsyncLimiter(2)

        parser = Parser()
        request = __import__("httpx").Request("POST", "http://ocr/v1/chat/completions")
        response = __import__("httpx").Response(503, request=request)
        cases = [
            __import__("httpx").HTTPStatusError("service unavailable", request=request, response=response),
            __import__("httpx").ConnectError("connection failed", request=request),
            __import__("httpx").ReadTimeout("read timeout", request=request),
        ]
        for exc in cases:
            with pytest.raises(type(exc)):
                async with runtime_ops.api_lane(parser):
                    raise exc
        return runtime_ops.get_runtime_snapshot(parser)

    snapshot = asyncio.run(run_errors())

    assert snapshot["api_error_count"] == 3
    assert snapshot["api_timeout_count"] == 1
    assert snapshot["api_error_categories"]["http_status"] == 1
    assert snapshot["api_error_categories"]["network"] == 1
    assert snapshot["api_error_categories"]["timeout"] == 1
    assert snapshot["api_error_status_codes"]["503"] == 1
    assert snapshot["api_error_types"]["HTTPStatusError"] == 1
    assert snapshot["api_error_types"]["ConnectError"] == 1
    assert snapshot["api_error_types"]["ReadTimeout"] == 1
    assert snapshot["api_last_error"]["category"] == "timeout"


def test_model_output_errors_are_counted_separately_from_transport_errors():
    class Parser:
        NonStandardModelOutputError = type("NonStandardModelOutputError", (Exception,), {})

    parser = Parser()

    runtime_ops.record_api_error(
        parser,
        Parser.NonStandardModelOutputError("missing bbox"),
        stage="model_output",
    )
    snapshot = runtime_ops.get_runtime_snapshot(parser)

    assert snapshot["api_error_count"] == 1
    assert snapshot["api_error_categories"]["model_output"] == 1
    assert snapshot["api_error_stages"]["model_output"] == 1
    assert snapshot["api_last_error"]["category"] == "model_output"
    assert snapshot["api_last_error"]["stage"] == "model_output"


def test_prepare_image_payload_for_vllm_offloads_non_data_urls(monkeypatch):
    calls = []

    async def fake_to_thread(func, image):
        calls.append((func, image))
        return "data:image/jpeg;base64,encoded"

    monkeypatch.setattr(inference_async.asyncio, "to_thread", fake_to_thread)

    payload, elapsed = asyncio.run(inference_async.prepare_image_payload_for_vllm("/tmp/page.jpg"))

    assert payload == "data:image/jpeg;base64,encoded"
    assert elapsed >= 0
    assert calls == [(inference_async._coerce_image_to_data_url, "/tmp/page.jpg")]


def test_native_openai_encoding_obeys_encode_semaphore(monkeypatch):
    active = 0
    max_active = 0

    async def fake_prepare_image_payload_for_vllm(image):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return f"data:image/jpeg;base64,{image}", 0.01

    async def fake_create(**_kwargs):
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        return SimpleNamespace(choices=[choice])

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=fake_create)))

    class FakeParser:
        model_name = "native"
        temperature = 0.1
        top_p = 0.9
        max_completion_tokens = 128

        def __init__(self):
            self.client = FakeClient()
            self.encode_semaphore = asyncio.Semaphore(1)
            self.api_semaphore = asyncio.Semaphore(4)

    monkeypatch.setattr(inference_async, "prepare_image_payload_for_vllm", fake_prepare_image_payload_for_vllm)

    async def run_two():
        engine = NativeOpenAIEngine(FakeParser(), "mineru")
        return await asyncio.gather(engine._infer("a.jpg", "prompt"), engine._infer("b.jpg", "prompt"))

    assert asyncio.run(run_two()) == ["ok", "ok"]
    assert max_active == 1


def test_native_openai_chat_completion_updates_api_lane_snapshot():
    async def fake_create(**_kwargs):
        await asyncio.sleep(0.01)
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        return SimpleNamespace(choices=[choice])

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=fake_create)))

    class Parser:
        model_name = "native"

        def __init__(self):
            self.client = FakeClient()
            self.api_semaphore = asyncio.Semaphore(1)

    async def run_two():
        parser = Parser()
        results = await asyncio.gather(
            create_chat_completion(parser, model="native", messages=[]),
            create_chat_completion(parser, model="native", messages=[]),
        )
        return results, runtime_ops.get_runtime_snapshot(parser)

    results, snapshot = asyncio.run(run_two())

    assert [result.choices[0].message.content for result in results] == ["ok", "ok"]
    assert snapshot["api_call_count"] == 2
    assert snapshot["api_inflight_peak"] == 1
    assert snapshot["api_wait_seconds_total"] > 0


def test_paddleocr_vl_encoding_obeys_encode_semaphore(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()
    image = Image.new("RGB", (120, 120), "white")

    def fake_pil_to_data_url(_image):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return "data:image/png;base64,encoded"

    class FakeParser:
        model_name = "paddleocr-vl"
        temperature = 0.1
        top_p = 0.9
        max_completion_tokens = 128
        layout_detection_url = "http://layout.local"
        block_concurrency = 0

        def __init__(self):
            self.encode_semaphore = asyncio.Semaphore(1)

    monkeypatch.setattr(PaddleOCRVLEngine, "_pil_to_data_url", staticmethod(fake_pil_to_data_url))

    async def run_two():
        engine = PaddleOCRVLEngine(FakeParser())
        return await asyncio.gather(engine._encode_async(image), engine._encode_async(image))

    assert asyncio.run(run_two()) == [
        "data:image/png;base64,encoded",
        "data:image/png;base64,encoded",
    ]
    assert max_active == 1


def test_paddleocr_vl_block_calls_obey_shared_api_semaphore(tmp_path, monkeypatch):
    """Unbounded block recognition should still honor the parser-wide API lane."""
    image_path = tmp_path / "page.jpg"
    Image.new("RGB", (120, 120), "white").save(image_path)

    active = 0
    max_active = 0

    async def fake_create(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        choice = SimpleNamespace(message=SimpleNamespace(content="text"))
        return SimpleNamespace(choices=[choice])

    class FakeClient:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=fake_create))
            )

    class FakeParser:
        model_name = "paddleocr-vl"
        temperature = 0.1
        top_p = 0.9
        max_completion_tokens = 128
        layout_detection_url = "http://layout.local"
        block_concurrency = 0

        def __init__(self):
            self.client = FakeClient()
            self.api_semaphore = asyncio.Semaphore(1)

    async def fake_detect_layout(self, image):
        return [
            {"bbox": [0, 0, 60, 60], "label": "text", "score": 0.9, "index": 0},
            {"bbox": [60, 60, 120, 120], "label": "text", "score": 0.9, "index": 1},
        ]

    monkeypatch.setattr(PaddleOCRVLEngine, "_detect_layout", fake_detect_layout)

    async def run_engine():
        engine = PaddleOCRVLEngine(FakeParser())
        await engine._infer_two_stage(str(image_path))

    asyncio.run(run_engine())

    assert max_active == 1


def test_page_consumer_obeys_render_semaphore(tmp_path, monkeypatch):
    active = 0
    max_active = 0

    def fake_process_pdf_page_worker(task_args):
        nonlocal active, max_active
        page_idx = task_args[1]
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.02)
        active -= 1
        return {
            "status": "success",
            "origin_path": str(tmp_path / f"p{page_idx}_orig.jpg"),
            "processed_path": str(tmp_path / f"p{page_idx}_proc.jpg"),
            "origin_size": (100, 100),
            "processed_size": (100, 100),
        }

    class FakeEngine:
        async def process_page(self, page_data):
            return SimpleNamespace(
                to_layout_result=lambda: {
                    "page_no": page_data["page_idx"],
                    "original_page_num": page_data["original_page_num"],
                    "status": "success",
                }
            )

    class FakeParser:
        dpi = 200
        blank_white_threshold = 0.98
        blank_noise_threshold = 0.002
        min_pixels = None
        max_pixels = None
        process_pool = None
        render_semaphore = asyncio.Semaphore(1)
        page_semaphore = asyncio.Semaphore(4)
        SUCCESS_STATUSES = {"success"}
        ocr_engine = FakeEngine()

        def _console_write(self, *_args, **_kwargs):
            pass

        async def _process_single_page_optimized_streaming(self, page_data):
            return (await self.ocr_engine.process_page(page_data)).to_layout_result()

    monkeypatch.setattr(document_parser, "process_pdf_page_worker", fake_process_pdf_page_worker)

    async def run_consumers():
        page_queue = asyncio.Queue()
        for item in (0, 1, None, None):
            await page_queue.put(item)
        results = {}
        loop = asyncio.get_running_loop()
        parser = FakeParser()
        await asyncio.gather(
            document_parser._page_consumer(
                parser,
                consumer_id=0,
                loop=loop,
                input_path="input.pdf",
                filename="doc",
                prompt_mode="prompt_layout_all_en",
                save_dir=str(tmp_path),
                page_queue=page_queue,
                results_storage=results,
                tmp_dir=str(tmp_path),
            ),
            document_parser._page_consumer(
                parser,
                consumer_id=1,
                loop=loop,
                input_path="input.pdf",
                filename="doc",
                prompt_mode="prompt_layout_all_en",
                save_dir=str(tmp_path),
                page_queue=page_queue,
                results_storage=results,
                tmp_dir=str(tmp_path),
            ),
        )
        return results

    results = asyncio.run(run_consumers())

    assert sorted(results) == [1, 2]
    assert max_active == 1


def test_dotsocr_postprocess_obeys_postprocess_semaphore(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()
    image = Image.new("RGB", (120, 120), "white")

    def fake_post_process_output(*_args, **_kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return ([{"category": "Text", "text": "ok"}], None)

    class FakeParser:
        min_pixels = None
        max_pixels = None
        save_page_layout = False
        generate_origin_md = False
        filter_keywords = []
        categories_to_filter = []
        trim_first_page_summary = False
        max_retries = 1
        concurrent_retries = 0
        enable_table_reparse = False
        _table_reparse_stack = 0

        def __init__(self):
            self.postprocess_semaphore = asyncio.Semaphore(1)
            self.monitor = FakeMonitor()

        def get_prompt(self, _prompt_mode, **_kwargs):
            return "prompt"

        async def _run_inference_with_retries(self, *_args, **_kwargs):
            return "[]", 1

        def _validate_cells_structure(self, _cells):
            pass

        async def _save_intermediate_outputs_async(self, *_args, **_kwargs):
            return None, None

    monkeypatch.setattr(dotsocr_engine, "post_process_output", fake_post_process_output)

    def page_data(page_idx):
        return {
            "page_idx": page_idx,
            "original_page_num": page_idx + 1,
            "filename": "doc.pdf",
            "save_dir": ".",
            "prompt_mode": "prompt_layout_all_en",
            "origin_image": image,
            "processed_image": image,
            "processed_size": image.size,
        }

    async def run_two():
        parser = FakeParser()
        engine = dotsocr_engine.DotsOCREngine(parser)
        return await asyncio.gather(engine.process_page(page_data(0)), engine.process_page(page_data(1)))

    results = asyncio.run(run_two())

    assert [result.status for result in results] == ["success", "success"]
    assert max_active == 1
