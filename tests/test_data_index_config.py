import asyncio
import importlib
import json
import sys
from pathlib import Path


CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "dots_ocr"
    / "data_index"
    / "configs"
    / "data_index_config.json"
)
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_data_index_config_does_not_contain_committed_api_key():
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    assert data.get("api_key", "") in {"", "0"}


def test_data_index_llm_settings_can_use_environment_api_key(monkeypatch):
    monkeypatch.setenv("DATA_INDEX_API_KEY", "env-test-key")
    sys.modules.pop("dots_ocr.data_index.llm_processor", None)

    module = importlib.import_module("dots_ocr.data_index.llm_processor")

    try:
        assert module._load_llm_settings()["api_key"] == "env-test-key"
    finally:
        asyncio.run(module.http_client.aclose())


def test_repository_config_examples_do_not_contain_internal_credentials():
    config_paths = [
        *REPO_ROOT.glob("dots_ocr/*s3*_config*.json"),
        REPO_ROOT / "dots_ocr" / "data_index" / "configs" / "datasource_mapping.json",
    ]

    for path in config_paths:
        text = path.read_text(encoding="utf-8")
        assert "http://172." not in text
        assert "REAL_INTERNAL_USERNAME" not in text

        data = json.loads(text)
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, value in item.items():
                    if key in {"ak", "sk", "access_key", "secret_key"}:
                        assert value in {"", None}
                    stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
