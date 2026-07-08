import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "pdf_worker_cache_under_test",
    ROOT / "ocr_parser" / "domain" / "pdf_worker.py",
)
pdf_worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pdf_worker
SPEC.loader.exec_module(pdf_worker)


class FakePdfDocument:
    def __init__(self, path: str):
        self.path = path
        self.is_closed = False

    def close(self):
        self.is_closed = True


def test_pdf_document_cache_does_not_close_active_leases(tmp_path, monkeypatch):
    opened = []

    def fake_open(path):
        doc = FakePdfDocument(path)
        opened.append(doc)
        return doc

    paths = []
    for index in range(pdf_worker._PDF_DOC_CACHE_MAX + 1):
        path = tmp_path / f"doc-{index}.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        paths.append(path)

    monkeypatch.setattr(pdf_worker.fitz, "open", fake_open)
    pdf_worker.clear_cached_documents()

    leases = []
    try:
        leases = [pdf_worker.lease_cached_pdf_document(str(path)) for path in paths]
        docs = [lease.__enter__() for lease in leases]

        assert all(doc is not None for doc in docs)
        assert all(not doc.is_closed for doc in docs)
    finally:
        for lease in reversed(leases):
            lease.__exit__(None, None, None)
        pdf_worker.clear_cached_documents()
