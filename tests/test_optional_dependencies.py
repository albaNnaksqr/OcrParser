from __future__ import annotations

import pytest

from ocr_platform import optional


def test_missing_extra_message_contains_exact_install_command(monkeypatch, capsys):
    monkeypatch.setattr(optional, "missing_modules", lambda modules: ("fastapi", "sqlalchemy"))

    with pytest.raises(SystemExit) as exc_info:
        optional.require_extra("platform", optional.PLATFORM_MODULES)

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "pip install 'ocrparser-platform[platform]'" in stderr
    assert "fastapi, sqlalchemy" in stderr
    assert "Traceback" not in stderr


def test_available_extra_returns_without_output(monkeypatch, capsys):
    monkeypatch.setattr(optional, "missing_modules", lambda modules: ())

    optional.require_extra("platform", optional.PLATFORM_MODULES)

    assert capsys.readouterr().err == ""
