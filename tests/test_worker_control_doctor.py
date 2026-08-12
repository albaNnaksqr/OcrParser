import io
import json
import urllib.error
from contextlib import contextmanager
from pathlib import Path

from ocr_platform.agent import control_doctor


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "worker-secret-token"


def make_opener(payload, *, captured=None):
    @contextmanager
    def _response(body: bytes):
        yield io.BytesIO(body)

    def opener(request, timeout=None):
        if captured is not None:
            captured.append(request)
        if isinstance(payload, Exception):
            raise payload
        return _response(json.dumps(payload).encode("utf-8"))

    return opener


def test_successful_probe_reports_server_count_and_registration():
    exit_code, line = control_doctor.probe_control_api(
        "http://control.example.internal:8080/",
        "worker-a",
        token=TOKEN,
        opener=make_opener([{"id": "worker-a"}, {"id": "worker-b"}]),
    )

    assert exit_code == 0
    assert "control_api=ok" in line
    assert "servers=2" in line
    assert "server_id=worker-a" in line
    assert "registered=yes" in line
    assert "auth=token" in line
    assert TOKEN not in line


def test_probe_reports_when_this_worker_has_not_registered_yet():
    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=make_opener([{"id": "worker-b"}]),
    )

    assert exit_code == 0
    assert "registered=no" in line


def test_probe_sends_the_control_supported_token_header():
    captured: list = []
    control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=make_opener([], captured=captured),
    )

    request = captured[0]
    assert request.full_url == "http://127.0.0.1:8080/api/servers"
    assert request.get_header(control_doctor.CONTROL_TOKEN_HEADER.capitalize()) == TOKEN


def test_unauthorized_is_a_concise_actionable_line_without_the_token():
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8080/api/servers", 401, "Unauthorized", {}, None
    )

    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=make_opener(error),
    )

    assert exit_code == 1
    assert "control_api=unauthorized" in line
    assert "status=401" in line
    assert "OCR_CONTROL_API_TOKEN" in line
    assert "OCR_PLATFORM_API_TOKEN" in line
    assert TOKEN not in line
    assert "Traceback" not in line
    assert line.count("\n") == 0


def test_other_http_errors_are_reported_with_status_and_a_hint():
    error = urllib.error.HTTPError(
        "http://127.0.0.1:8080/api/servers", 503, "Unavailable", {}, None
    )

    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=make_opener(error),
    )

    assert exit_code == 1
    assert "control_api=http_error" in line
    assert "status=503" in line
    assert "OCR_CONTROL_URL" in line
    assert TOKEN not in line


def test_connection_failure_is_reported_without_a_traceback():
    error = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))

    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=make_opener(error),
    )

    assert exit_code == 1
    assert "control_api=unreachable" in line
    assert "Connection refused" in line
    assert "OCR_CONTROL_URL" in line
    assert TOKEN not in line
    assert line.count("\n") == 0


def test_non_json_response_is_reported_as_an_invalid_control_url():
    def opener(request, timeout=None):
        @contextmanager
        def _response():
            yield io.BytesIO(b"<html>proxy error</html>")

        return _response()

    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=opener,
    )

    assert exit_code == 1
    assert "control_api=invalid_response" in line
    assert "reason=response_is_not_json" in line
    assert TOKEN not in line


def test_json_object_instead_of_a_server_list_is_reported_as_invalid():
    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=TOKEN,
        opener=make_opener({"detail": "not a list"}),
    )

    assert exit_code == 1
    assert "control_api=invalid_response" in line


def test_probe_stays_compatible_when_no_token_is_configured():
    captured: list = []

    exit_code, line = control_doctor.probe_control_api(
        "http://127.0.0.1:8080",
        "worker-a",
        token=None,
        opener=make_opener([{"id": "worker-a"}], captured=captured),
    )

    assert exit_code == 0
    assert "auth=anonymous" in line
    assert captured[0].get_header(
        control_doctor.CONTROL_TOKEN_HEADER.capitalize()
    ) is None


def test_main_reads_the_token_from_the_environment_and_never_prints_it(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv(control_doctor.CONTROL_TOKEN_ENV_VAR, TOKEN)
    monkeypatch.setattr(
        control_doctor,
        "probe_control_api",
        lambda control_url, server_id, *, token, timeout: (
            (0, f"control_api=ok token_seen={token == TOKEN}")
        ),
    )

    exit_code = control_doctor.main(
        ["--control_url", "http://127.0.0.1:8080", "--server_id", "worker-a"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "token_seen=True" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_doctor_uses_the_token_and_the_packaged_probe_module():
    script = (ROOT / "scripts" / "ocr_agent_worker.sh").read_text(encoding="utf-8")

    assert 'CONTROL_API_TOKEN="${OCR_CONTROL_API_TOKEN:-}"' in script
    assert "-m ocr_platform.agent.control_doctor" in script
    assert 'OCR_CONTROL_API_TOKEN="$CONTROL_API_TOKEN"' in script
    assert "control_api_token=$([[ -n \"$CONTROL_API_TOKEN\" ]] && echo set || echo unset)" in script
    assert 'echo "$CONTROL_API_TOKEN"' not in script
    assert "urlopen" not in script
