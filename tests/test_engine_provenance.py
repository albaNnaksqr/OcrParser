from __future__ import annotations

import asyncio
import json
import sys
import traceback

import pytest
from fastapi.testclient import TestClient

from ocr_platform.agent import __main__ as agent_main
from ocr_platform.agent.config import AgentConfig, parse_args
from ocr_platform.agent.lanes import _heartbeat_capabilities
from ocr_platform.agent.runtime import AgentRuntime
from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.engine_provenance import (
    ENGINE_PROVENANCE_FILE_TOO_LARGE,
    ENGINE_PROVENANCE_FILE_UNAVAILABLE,
    ENGINE_PROVENANCE_INVALID,
    MAX_ENGINE_PROVENANCE_FILE_BYTES,
    EngineProvenanceError,
    build_engine_provenance_capability,
    load_engine_provenance_file,
)


DIGEST_A = f"sha256:{'a' * 64}"
DIGEST_B = f"sha256:{'b' * 64}"


def _valid_file_payload() -> dict[str, object]:
    return {
        "profiles": {
            "dotsocr_public": {
                "model_revision": "v1.2.3",
                "model_digest": DIGEST_A,
                "runtime_revision": "0123456789abcdef",
                "runtime_digest": DIGEST_B,
                "layout_revision": "main",
                "layout_digest": DIGEST_A,
            }
        }
    }


def _write_payload(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_control_client(tmp_path) -> TestClient:
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    init_db(engine)
    return TestClient(create_app(session_factory=session_factory))


def _register_payload(capabilities: dict[str, object]) -> dict[str, object]:
    return {
        "id": "server-a",
        "name": "Server A",
        "host": "worker-a",
        "capacity_slots": 1,
        "capabilities": capabilities,
    }


def test_engine_provenance_file_loads_only_whitelisted_fields(tmp_path):
    path = tmp_path / "provenance.json"
    _write_payload(path, _valid_file_payload())

    payload = load_engine_provenance_file(path)

    assert payload == _valid_file_payload()


@pytest.mark.parametrize(
    "payload",
    [
        {"profiles": {}, "api_key": "do-not-store"},
        {"profiles": {"dotsocr": {"endpoint": "https://private.invalid"}}},
        {"profiles": {"api_key_profile": {"model_revision": "main"}}},
        {"profiles": {"dotsocr": {"model_revision": "secret-token"}}},
        {"profiles": {"dotsocr": {"model_revision": "10.0.0.8"}}},
        {"profiles": {"dotsocr": {"model_revision": "model.internal"}}},
        {"profiles": {"dotsocr": {"model_revision": "/private/model"}}},
        {"profiles": {"dotsocr": {"model_revision": "customer-document"}}},
        {"profiles": {"dotsocr": {"model_revision": "private model notes"}}},
        {"profiles": {"dotsocr": {"model_digest": "sha256:not-a-digest"}}},
    ],
)
def test_engine_provenance_file_rejects_unknown_sensitive_or_free_text(
    tmp_path,
    payload,
):
    path = tmp_path / "sensitive-value-never-echoed.json"
    _write_payload(path, payload)

    with pytest.raises(EngineProvenanceError) as exc_info:
        load_engine_provenance_file(path)

    assert str(exc_info.value) == ENGINE_PROVENANCE_INVALID
    assert "sensitive-value-never-echoed" not in str(exc_info.value)
    assert "private" not in str(exc_info.value)


def test_engine_provenance_file_errors_are_fixed_and_path_free(tmp_path):
    missing = tmp_path / "customer-secret-file.json"

    with pytest.raises(EngineProvenanceError) as missing_error:
        load_engine_provenance_file(missing)

    assert str(missing_error.value) == ENGINE_PROVENANCE_FILE_UNAVAILABLE
    assert str(missing) not in str(missing_error.value)
    missing_traceback = "".join(
        traceback.format_exception(
            missing_error.type,
            missing_error.value,
            missing_error.tb,
        )
    )
    assert str(missing) not in missing_traceback

    oversized = tmp_path / "oversized-secret-file.json"
    oversized.write_bytes(b"x" * (MAX_ENGINE_PROVENANCE_FILE_BYTES + 1))

    with pytest.raises(EngineProvenanceError) as oversized_error:
        load_engine_provenance_file(oversized)

    assert str(oversized_error.value) == ENGINE_PROVENANCE_FILE_TOO_LARGE
    assert str(oversized) not in str(oversized_error.value)

    invalid = tmp_path / "invalid-secret-file.json"
    invalid.write_text("{", encoding="utf-8")

    with pytest.raises(EngineProvenanceError) as invalid_error:
        load_engine_provenance_file(invalid)

    assert str(invalid_error.value) == ENGINE_PROVENANCE_INVALID
    assert str(invalid) not in str(invalid_error.value)


def test_engine_provenance_validation_traceback_does_not_echo_input(tmp_path):
    path = tmp_path / "private-provenance.json"
    leaked_key = "api-key-value-must-never-appear"
    leaked_endpoint = "https://10.0.0.8/private"
    _write_payload(
        path,
        {
            "profiles": {
                "dotsocr": {
                    "api_key": leaked_key,
                    "endpoint": leaked_endpoint,
                }
            }
        },
    )

    with pytest.raises(EngineProvenanceError) as exc_info:
        load_engine_provenance_file(path)

    rendered = "".join(
        traceback.format_exception(
            exc_info.type,
            exc_info.value,
            exc_info.tb,
        )
    )
    assert str(exc_info.value) == ENGINE_PROVENANCE_INVALID
    assert str(path) not in rendered
    assert leaked_key not in rendered
    assert leaked_endpoint not in rendered


def test_engine_provenance_cli_overrides_environment_file(tmp_path, monkeypatch):
    env_file = tmp_path / "environment.json"
    cli_file = tmp_path / "command-line.json"
    _write_payload(env_file, {"profiles": {"environment": {"model_revision": "main"}}})
    _write_payload(cli_file, {"profiles": {"command_line": {"model_revision": "v1.0"}}})
    monkeypatch.setenv("OCR_AGENT_ENGINE_PROVENANCE_FILE", str(env_file))
    monkeypatch.setattr("ocr_platform.legal.build_provenance", lambda: {})

    config = parse_args(["--engine_provenance_file", str(cli_file)])

    assert config.engine_provenance_file == str(cli_file)
    assert config.engine_provenance == {
        "profiles": {"command_line": {"model_revision": "v1.0"}}
    }


def test_engine_provenance_environment_file_is_loaded_at_startup(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "environment.json"
    _write_payload(path, {"profiles": {"dotsocr": {"model_revision": "main"}}})
    monkeypatch.setenv("OCR_AGENT_ENGINE_PROVENANCE_FILE", str(path))
    monkeypatch.setattr("ocr_platform.legal.build_provenance", lambda: {})

    config = parse_args([])

    assert config.engine_provenance_file == str(path)
    assert config.engine_provenance == {
        "profiles": {"dotsocr": {"model_revision": "main"}}
    }


def test_engine_provenance_missing_environment_file_fails_fast_without_path(
    tmp_path,
    monkeypatch,
):
    missing = tmp_path / "never-echo-this-customer-path.json"
    monkeypatch.setenv("OCR_AGENT_ENGINE_PROVENANCE_FILE", str(missing))

    with pytest.raises(EngineProvenanceError) as exc_info:
        parse_args([])

    assert str(exc_info.value) == ENGINE_PROVENANCE_FILE_UNAVAILABLE
    assert str(missing) not in str(exc_info.value)


def test_agent_main_exits_with_fixed_path_free_provenance_error(
    tmp_path,
    monkeypatch,
):
    missing = tmp_path / "never-echo-this-customer-path.json"
    monkeypatch.setenv("OCR_AGENT_ENGINE_PROVENANCE_FILE", str(missing))
    monkeypatch.setattr(agent_main, "require_extra", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["ocr-platform-agent"])

    with pytest.raises(SystemExit) as exc_info:
        agent_main.main()

    assert exc_info.value.code == ENGINE_PROVENANCE_FILE_UNAVAILABLE
    assert str(missing) not in str(exc_info.value)


def test_engine_provenance_uses_only_immutable_build_metadata(monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_SOURCE_REVISION", "poisoned-environment")
    monkeypatch.setenv("OCR_PLATFORM_SOURCE_URL", "https://private.invalid/source")
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {
            "source_revision": "0123456789abcdef",
            "dirty": False,
            "build_timestamp": "2026-07-28T00:00:00Z",
            "source_url": "https://private.invalid/source",
        },
    )

    payload = build_engine_provenance_capability({})

    assert payload == {
        "profiles": {},
        "source_revision": "0123456789abcdef",
        "dirty": False,
    }
    assert "poisoned-environment" not in json.dumps(payload)
    assert "source_url" not in payload
    assert "build_timestamp" not in payload


def test_agent_runtime_loads_file_once_and_heartbeats_use_cached_payload(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "provenance.json"
    _write_payload(path, _valid_file_payload())
    load_calls = 0
    real_loader = load_engine_provenance_file

    def counted_loader(file_path):
        nonlocal load_calls
        load_calls += 1
        return real_loader(file_path)

    monkeypatch.setattr(
        "ocr_platform.agent.config.load_engine_provenance_file",
        counted_loader,
    )
    monkeypatch.setattr("ocr_platform.legal.build_provenance", lambda: {})
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        resource_guard_enabled=False,
        engine_provenance_file=str(path),
    )

    runtime = AgentRuntime(config, client=object())
    first = _heartbeat_capabilities(runtime.config)["engine_provenance"]
    second = _heartbeat_capabilities(runtime.config)["engine_provenance"]

    assert first == second == _valid_file_payload()
    assert load_calls == 1


def test_agent_config_repr_does_not_include_provenance_path(tmp_path):
    path = tmp_path / "private-provenance.json"

    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        engine_provenance_file=str(path),
    )

    assert str(path) not in repr(config)


def test_agent_runtime_registers_exact_cached_engine_provenance(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "provenance.json"
    _write_payload(
        path,
        {
            "profiles": {
                "dotsocr_public": {
                    "model_revision": "main",
                    "runtime_digest": DIGEST_A,
                }
            }
        },
    )
    expected = {
        "profiles": {
            "dotsocr_public": {
                "model_revision": "main",
                "runtime_digest": DIGEST_A,
            }
        },
        "source_revision": "0123456789abcdef",
        "dirty": False,
    }
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": "0123456789abcdef", "dirty": False},
    )

    class RuntimeClient:
        def __init__(self):
            self.register_payload = None

        async def register(self, **payload):
            self.register_payload = payload

        async def close(self):
            pass

    client = RuntimeClient()
    runtime = AgentRuntime(
        AgentConfig(
            server_id="server-a",
            control_url="http://control:8080",
            work_dir=str(tmp_path / "work"),
            engine_provenance_file=str(path),
        ),
        client=client,
    )
    runtime.start_lanes = runtime.request_shutdown

    asyncio.run(runtime.run())

    assert client.register_payload == {
        "host": client.register_payload["host"],
        "capabilities": {"engine_provenance": expected},
    }
    assert set(expected) == {"profiles", "source_revision", "dirty"}
    assert set(expected["profiles"]["dotsocr_public"]) == {
        "model_revision",
        "runtime_digest",
    }


def test_control_old_agent_register_and_heartbeat_fail_closed_to_empty(tmp_path):
    client = _make_control_client(tmp_path)
    valid = {
        "engine_provenance": {
            "profiles": {"dotsocr": {"model_revision": "main"}},
        }
    }
    assert client.post(
        "/api/servers/register",
        json=_register_payload(valid),
    ).status_code == 200

    old_register = client.post(
        "/api/servers/register",
        json=_register_payload({"agent": "old-agent"}),
    )
    assert old_register.status_code == 200
    assert old_register.json()["capabilities"]["engine_provenance"] == {
        "profiles": {}
    }

    assert client.post(
        "/api/servers/register",
        json=_register_payload(valid),
    ).status_code == 200
    old_heartbeat = client.post(
        "/api/servers/server-a/heartbeat",
        json={"status": "idle", "capabilities": {"agent": "old-agent"}},
    )
    assert old_heartbeat.status_code == 200
    assert old_heartbeat.json()["capabilities"]["engine_provenance"] == {
        "profiles": {}
    }


@pytest.mark.parametrize("endpoint", ["register", "heartbeat"])
def test_control_rejects_malicious_provenance_without_storing_it(
    tmp_path,
    endpoint,
):
    client = _make_control_client(tmp_path)
    original = {
        "engine_provenance": {
            "profiles": {"dotsocr": {"model_revision": "main"}},
        },
        "stable": True,
    }
    assert client.post(
        "/api/servers/register",
        json=_register_payload(original),
    ).status_code == 200
    malicious = {
        "engine_provenance": {
            "profiles": {
                "dotsocr": {
                    "model_revision": "main",
                    "api_key": "must-never-be-stored",
                }
            }
        },
        "stable": False,
    }
    response = (
        client.post(
            "/api/servers/register",
            json=_register_payload(malicious),
        )
        if endpoint == "register"
        else client.post(
            "/api/servers/server-a/heartbeat",
            json={"status": "busy", "capabilities": malicious},
        )
    )

    assert response.status_code == 400
    assert response.json() == {"detail": ENGINE_PROVENANCE_INVALID}
    assert "must-never-be-stored" not in response.text
    server = next(
        item for item in client.get("/api/servers").json()
        if item["id"] == "server-a"
    )
    assert server["capabilities"] == original
    assert server["status"] == "online"
