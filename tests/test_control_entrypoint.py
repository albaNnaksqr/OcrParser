import pytest
from types import SimpleNamespace

from ocr_platform.control.__main__ import main, validate_control_bind


def test_control_bind_defaults_to_loopback(monkeypatch):
    calls = []
    monkeypatch.delenv("OCR_PLATFORM_HOST", raising=False)
    monkeypatch.delenv("OCR_PLATFORM_API_TOKEN", raising=False)
    monkeypatch.setattr("ocr_platform.control.__main__.require_extra", lambda *args: None)
    monkeypatch.setattr(
        "ocr_platform.control.__main__.import_module",
        lambda name: SimpleNamespace(run=lambda *args, **kwargs: calls.append((args, kwargs))),
    )

    main()

    assert calls[0][1]["host"] == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "control.internal"])
def test_non_loopback_bind_requires_api_token(host, monkeypatch):
    monkeypatch.delenv("OCR_PLATFORM_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="OCR_PLATFORM_API_TOKEN"):
        validate_control_bind(host)


def test_non_loopback_bind_accepts_api_token(monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "control-secret")

    validate_control_bind("0.0.0.0")
