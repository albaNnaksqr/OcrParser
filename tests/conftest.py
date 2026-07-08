import asyncio
import inspect
import importlib.util
import json
import sys
from types import ModuleType

import pytest


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return name in sys.modules


def _install_if_missing(name: str, module: ModuleType) -> None:
    if not _module_available(name):
        sys.modules.setdefault(name, module)


class _AsyncFile:
    def __init__(self, path, mode="r", encoding=None):
        self.path = path
        self.mode = mode
        self.encoding = encoding
        self.handle = None

    async def __aenter__(self):
        self.handle = open(self.path, self.mode, encoding=self.encoding)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.handle is not None:
            self.handle.close()

    async def write(self, text):
        return self.handle.write(text)

    async def read(self):
        return self.handle.read()


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


class _ImageHashStub:
    def __sub__(self, other):
        return 0

    def __str__(self):
        return "0" * 16


_PDF_PAGE_COUNTS: dict[str, int] = {}


class _FakeFitzDocument:
    def __init__(self, page_count: int = 0):
        self._page_count = page_count
        self.is_closed = False

    @property
    def page_count(self) -> int:
        return self._page_count

    def __len__(self) -> int:
        return self._page_count

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def new_page(self, *args, **kwargs):
        self._page_count += 1
        return object()

    def save(self, path, *args, **kwargs):
        path_text = str(path)
        _PDF_PAGE_COUNTS[path_text] = self._page_count
        with open(path, "wb") as handle:
            handle.write(f"%PDF-FAKE pages={self._page_count}\n".encode("ascii"))

    def close(self):
        self.is_closed = True

    def insert_pdf(self, other, *args, **kwargs):
        self._page_count += int(getattr(other, "page_count", 0) or 0)


def _fitz_open(*args, **kwargs):
    if not args:
        return _FakeFitzDocument()
    path = args[0]
    if isinstance(path, str) and path == "pdf":
        return _FakeFitzDocument(page_count=1)
    path_text = str(path)
    page_count = _PDF_PAGE_COUNTS.get(path_text)
    if page_count is None:
        try:
            content = open(path, "rb").read(80).decode("ascii", errors="ignore")
        except OSError:
            content = ""
        if "pages=" in content:
            try:
                page_count = int(content.split("pages=", 1)[1].split()[0])
            except (IndexError, ValueError):
                page_count = 1
        else:
            page_count = 1
    return _FakeFitzDocument(page_count=page_count)


fitz_stub = ModuleType("fitz")
fitz_stub.Document = _FakeFitzDocument
fitz_stub.Matrix = lambda *args, **kwargs: object()
fitz_stub.Rect = lambda *args, **kwargs: tuple(args)
fitz_stub.Pixmap = lambda *args, **kwargs: object()
fitz_stub.open = _fitz_open
_install_if_missing("fitz", fitz_stub)

cv2_stub = ModuleType("cv2")
cv2_stub.COLOR_RGB2BGR = 0
cv2_stub.COLOR_BGR2GRAY = 1
cv2_stub.THRESH_BINARY = 0
cv2_stub.THRESH_OTSU = 0
cv2_stub.cvtColor = lambda image, *_args, **_kwargs: image
cv2_stub.threshold = lambda image, *_args, **_kwargs: (None, image)
cv2_stub.QRCodeDetector = lambda: object()
_install_if_missing("cv2", cv2_stub)

json_repair_stub = ModuleType("json_repair")
json_repair_stub.loads = lambda value, *args, **kwargs: json.loads(value)
_install_if_missing("json_repair", json_repair_stub)

aiofiles_stub = ModuleType("aiofiles")
aiofiles_stub.open = lambda path, mode="r", encoding=None: _AsyncFile(
    path,
    mode=mode,
    encoding=encoding,
)
_install_if_missing("aiofiles", aiofiles_stub)

imagehash_stub = ModuleType("imagehash")
imagehash_stub.phash = lambda *args, **kwargs: _ImageHashStub()
_install_if_missing("imagehash", imagehash_stub)

requests_stub = ModuleType("requests")
_install_if_missing("requests", requests_stub)

prometheus_stub = ModuleType("prometheus_client")
prometheus_stub.Counter = _MetricStub
prometheus_stub.Gauge = _MetricStub
prometheus_stub.Histogram = _MetricStub
prometheus_stub.start_http_server = lambda *args, **kwargs: None
_install_if_missing("prometheus_client", prometheus_stub)


def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


@pytest.fixture(autouse=True)
def ensure_current_event_loop():
    _ensure_event_loop()
    yield
    _ensure_event_loop()


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run async tests with a local event loop")


def pytest_pyfunc_call(pyfuncitem):
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    testargs = {
        arg: pyfuncitem.funcargs[arg]
        for arg in pyfuncitem._fixtureinfo.argnames
    }
    try:
        loop.run_until_complete(pyfuncitem.obj(**testargs))
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())
    return True
