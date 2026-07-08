from fastapi.testclient import TestClient

from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.remote_workers import (
    RemoteWorkerExecutor,
    RemoteWorkerResult,
    RemoteWorkerScaleResult,
    parse_scale_plan_items,
    load_remote_worker_targets,
)
from ocr_platform.control.schemas import RemoteWorkerInstallDryRunRequest, RemoteWorkerScaleRequest


class FakeRemoteWorkerExecutor:
    def __init__(self):
        self.calls = []

    def preflight(self, request):
        self.calls.append(("preflight", request))
        return RemoteWorkerResult(
            action="preflight",
            host=request.host,
            command=["ssh", request.ssh_target(), "preflight"],
            return_code=0,
            stdout="beegfs ok",
            stderr="",
        )

    def install_dry_run(self, request):
        self.calls.append(("install_dry_run", request))
        return RemoteWorkerResult(
            action="install_dry_run",
            host=request.host,
            command=["ssh", request.ssh_target(), "install"],
            return_code=0,
            stdout="Install plan: worker",
            stderr="",
        )

    def install_apply(self, request):
        self.calls.append(("install_apply", request))
        return RemoteWorkerResult(
            action="install_apply",
            host=request.host,
            command=["ssh", request.ssh_target(), "install", "--yes"],
            return_code=0,
            stdout="Applied install plan",
            stderr="",
        )

    def service_action(self, request):
        self.calls.append(("service_action", request))
        return RemoteWorkerResult(
            action=request.action,
            host=request.host,
            command=["ssh", request.ssh_target(), request.action],
            return_code=0,
            stdout="restarted",
            stderr="",
        )

    def scale_plan(self, request):
        self.calls.append(("scale_plan", request))
        return RemoteWorkerScaleResult(
            action="scale_plan",
            host=request.host,
            command=["ssh", request.ssh_target(), "scale-plan"],
            return_code=0,
            stdout='{"action":"create_env","status":"pending","instance":"worker-02","server_id":"ocr-worker-a-02","message":"create /etc/ocr-agent/worker-02.env"}\n',
            stderr="",
            plan_items=[
                {
                    "action": "create_env",
                    "status": "pending",
                    "instance": "worker-02",
                    "server_id": "ocr-worker-a-02",
                    "message": "create /etc/ocr-agent/worker-02.env",
                }
            ],
        )

    def scale_apply(self, request):
        self.calls.append(("scale_apply", request))
        return RemoteWorkerScaleResult(
            action="scale_apply",
            host=request.host,
            command=["ssh", request.ssh_target(), "scale-apply"],
            return_code=0,
            stdout='{"action":"start_service","status":"ok","instance":"worker-02","server_id":"ocr-worker-a-02","message":"started ocr-agent-worker@worker-02"}\n',
            stderr="",
            plan_items=[
                {
                    "action": "start_service",
                    "status": "ok",
                    "instance": "worker-02",
                    "server_id": "ocr-worker-a-02",
                    "message": "started ocr-agent-worker@worker-02",
                }
            ],
        )


def make_client(tmp_path, executor):
    session_factory, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    init_db(engine)
    app = create_app(session_factory=session_factory, remote_worker_executor=executor)
    return TestClient(app)


def test_remote_worker_preflight_uses_configured_executor(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.post(
        "/api/remote-workers/preflight",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "service_user": "ocr_user",
            "service_group": "ocr_user",
            "shared_roots": ["/shared/ocr-data"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["action"] == "preflight"
    assert payload["stdout"] == "beegfs ok"
    assert executor.calls[0][0] == "preflight"
    assert executor.calls[0][1].ssh_target() == "ocr_user@ocr-prod-40-3"


def test_remote_worker_targets_can_be_loaded_from_json_allowlist(tmp_path, monkeypatch):
    config_path = tmp_path / "remote-workers.json"
    config_path.write_text(
        """{
          "targets": [
            {
              "id": "prod-3",
              "host": "ocr-prod-40-3",
              "hostname": "worker-1.example.internal",
              "ssh_user": "ocr_user",
              "server_id": "worker-1.example.internal",
              "service_user": "ocr_user",
              "service_group": "ocr_user",
              "shared_roots": ["/shared/ocr-data"]
            }
          ]
        }""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCR_PLATFORM_REMOTE_WORKER_CONFIG", str(config_path))

    targets = load_remote_worker_targets()

    assert len(targets) == 1
    assert targets[0].host == "ocr-prod-40-3"
    assert targets[0].hostname == "worker-1.example.internal"
    assert targets[0].ssh_user == "ocr_user"
    assert targets[0].server_id == "worker-1.example.internal"


def test_remote_worker_targets_ignore_invalid_config_entries(tmp_path, monkeypatch):
    config_path = tmp_path / "remote-workers.json"
    config_path.write_text(
        """[
          {"host": "bad host", "ssh_user": "ocr_user"},
          {"host": "ocr-prod-40-3", "ssh_user": "bad user"},
          {"host": "ocr-prod-40-4", "ssh_user": "ocr_user"}
        ]""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OCR_PLATFORM_REMOTE_WORKER_CONFIG", str(config_path))

    targets = load_remote_worker_targets()

    assert [target.host for target in targets] == ["ocr-prod-40-4"]


def test_remote_worker_targets_ignore_invalid_json_allowlist(tmp_path, monkeypatch):
    config_path = tmp_path / "remote-workers.json"
    config_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("OCR_PLATFORM_REMOTE_WORKER_CONFIG", str(config_path))

    assert load_remote_worker_targets() == []


def test_remote_worker_targets_parse_ssh_config_with_prefix_filter(tmp_path, monkeypatch):
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        """
Host github.com
  HostName github.com
  User git

Host ocr-prod-40-3
  HostName worker-1.example.internal
  User ocr_user

Host ocr-prod-40-4
  HostName worker-2.example.internal
  User ocr_user
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("OCR_PLATFORM_REMOTE_WORKER_CONFIG", raising=False)
    monkeypatch.setenv("OCR_PLATFORM_REMOTE_WORKER_SSH_CONFIG", str(ssh_config))

    targets = load_remote_worker_targets()

    assert [target.host for target in targets] == ["ocr-prod-40-3", "ocr-prod-40-4"]
    assert [target.server_id for target in targets] == ["ocr-prod-40-3", "ocr-prod-40-4"]


def test_remote_worker_targets_endpoint_exposes_inventory(tmp_path, monkeypatch):
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        """
Host ocr-prod-40-3
  HostName worker-1.example.internal
  User ocr_user
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("OCR_PLATFORM_REMOTE_WORKER_CONFIG", raising=False)
    monkeypatch.setenv("OCR_PLATFORM_REMOTE_WORKER_SSH_CONFIG", str(ssh_config))
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.get("/api/remote-workers/targets")

    assert response.status_code == 200
    payload = response.json()
    assert payload["targets"][0]["host"] == "ocr-prod-40-3"
    assert payload["targets"][0]["hostname"] == "worker-1.example.internal"
    assert payload["targets"][0]["ssh_user"] == "ocr_user"


def test_remote_worker_install_dry_run_requires_control_url_and_shared_roots(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.post(
        "/api/remote-workers/install-dry-run",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "server_id": "worker-1.example.internal",
            "service_user": "ocr_user",
            "service_group": "ocr_user",
            "control_url": "http://control.example.internal:8080",
            "shared_roots": ["/shared/ocr-data"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["action"] == "install_dry_run"
    assert "Install plan" in payload["stdout"]
    assert executor.calls[0][0] == "install_dry_run"


def test_remote_worker_install_dry_run_command_does_not_require_sudo():
    executor = RemoteWorkerExecutor()
    request = RemoteWorkerInstallDryRunRequest(
        host="ocr-worker-1",
        ssh_user="ocr_admin",
        server_id="ocr-worker-1",
        service_user="ocr_agent",
        service_group="ocr_runtime",
        repo_dir="/srv/ocrparser",
        control_url="http://control.internal:8080",
        shared_roots=["/mnt/shared"],
    )

    command = executor._install_command(request, apply=False)

    assert " python3 /srv/ocrparser/tools/install_production.py worker " in command
    assert "sudo" not in command
    assert "--repo-dir /srv/ocrparser" in command
    assert "--dry-run" in command
    assert "--yes" not in command


def test_remote_worker_install_apply_command_uses_non_interactive_sudo():
    executor = RemoteWorkerExecutor()
    request = RemoteWorkerInstallDryRunRequest(
        host="ocr-worker-1",
        ssh_user="ocr_admin",
        server_id="ocr-worker-1",
        service_user="ocr_agent",
        service_group="ocr_runtime",
        repo_dir="/srv/ocrparser",
        control_url="http://control.internal:8080",
        shared_roots=["/mnt/shared"],
    )

    command = executor._install_command(request, apply=True)

    assert "sudo -n python3 /srv/ocrparser/tools/install_production.py worker" in command
    assert "--repo-dir /srv/ocrparser" in command
    assert "--yes" in command
    assert "--dry-run" not in command


def test_remote_worker_install_apply_uses_explicit_apply_endpoint(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.post(
        "/api/remote-workers/install-apply",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "server_id": "worker-1.example.internal",
            "service_user": "ocr_user",
            "service_group": "ocr_user",
            "control_url": "http://control.example.internal:8080",
            "shared_roots": ["/shared/ocr-data"],
        },
    )

    assert response.status_code == 200
    assert response.json()["action"] == "install_apply"
    assert executor.calls[0][0] == "install_apply"


def test_remote_worker_scale_plan_endpoint_returns_structured_items(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.post(
        "/api/remote-workers/scale-plan",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "repo_dir": "/opt/ocr-platform/ocrparser",
            "service_user": "ocr_user",
            "service_group": "ocr_user",
            "target_count": 4,
            "seed_server_id": "ocr-worker-a-01",
            "server_id_prefix": "ocr-worker-a",
            "shared_roots": ["/shared/ocr-data"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["action"] == "scale_plan"
    assert payload["plan_items"][0]["action"] == "create_env"
    assert payload["plan_items"][0]["instance"] == "worker-02"
    assert executor.calls[0][0] == "scale_plan"
    assert executor.calls[0][1].target_count == 4


def test_remote_worker_scale_apply_endpoint_returns_structured_items(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.post(
        "/api/remote-workers/scale-apply",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "repo_dir": "/opt/ocr-platform/ocrparser",
            "service_user": "ocr_user",
            "service_group": "ocr_user",
            "target_count": 2,
            "server_id_prefix": "ocr-worker-a",
            "shared_roots": ["/shared/ocr-data"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["action"] == "scale_apply"
    assert payload["plan_items"][0]["action"] == "start_service"
    assert executor.calls[0][0] == "scale_apply"


def test_remote_worker_scale_rejects_invalid_prefix_and_large_target(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    invalid_prefix = client.post(
        "/api/remote-workers/scale-plan",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "target_count": 2,
            "server_id_prefix": "bad prefix",
        },
    )
    too_many = client.post(
        "/api/remote-workers/scale-plan",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "target_count": 17,
            "server_id_prefix": "ocr-worker-a",
        },
    )

    assert invalid_prefix.status_code == 400
    assert too_many.status_code == 422


def test_remote_worker_scale_plan_command_is_read_only():
    executor = RemoteWorkerExecutor()
    request = RemoteWorkerScaleRequest(
        host="ocr-worker-1",
        ssh_user="ocr_admin",
        repo_dir="/srv/ocrparser",
        service_user="ocr_agent",
        service_group="ocr_runtime",
        target_count=4,
        seed_server_id="ocr-worker-a-01",
        server_id_prefix="ocr-worker-a",
        shared_roots=["/mnt/shared"],
    )

    command = executor._scale_command(request, apply=False)

    assert "sudo" not in command
    assert "systemctl enable" not in command
    assert "systemctl stop" not in command
    assert "write_text" not in command
    assert "mkdir" not in command
    assert "worker-04" in command
    assert "scale_plan" in command


def test_remote_worker_scale_apply_command_writes_instance_env_and_services():
    executor = RemoteWorkerExecutor()
    request = RemoteWorkerScaleRequest(
        host="ocr-worker-1",
        ssh_user="ocr_admin",
        repo_dir="/srv/ocrparser",
        service_user="ocr_agent",
        service_group="ocr_runtime",
        target_count=2,
        seed_server_id="ocr-worker-a-01",
        server_id_prefix="ocr-worker-a",
        shared_roots=["/mnt/shared"],
    )

    command = executor._scale_command(request, apply=True)

    assert "sudo -n python3" in command
    assert "worker-02.env" in command
    assert "OCR_AGENT_SERVER_ID" in command
    assert "OCR_AGENT_WORK_DIR" in command
    assert "OCR_AGENT_LOG_DIR" in command
    assert "OCR_AGENT_EVENT_SPOOL_DIR" in command
    assert "systemctl enable --now ocr-agent-worker@worker-02" in command


def test_remote_worker_scale_apply_command_generates_shrink_actions():
    executor = RemoteWorkerExecutor()
    request = RemoteWorkerScaleRequest(
        host="ocr-worker-1",
        ssh_user="ocr_admin",
        repo_dir="/srv/ocrparser",
        target_count=1,
        server_id_prefix="ocr-worker-a",
        shared_roots=["/mnt/shared"],
    )

    command = executor._scale_command(request, apply=True)

    assert "stop_service" in command
    assert "disable_service" in command
    assert "systemctl stop" in command
    assert "systemctl disable" in command


def test_parse_scale_plan_items_preserves_unparsed_output_as_warning():
    items = parse_scale_plan_items(
        "not json\n"
        '{"action":"start_service","status":"ok","instance":"worker-02","message":"started"}\n'
    )

    assert items[0]["action"] == "skip"
    assert items[0]["status"] == "warning"
    assert "not json" in items[0]["message"]
    assert items[1]["action"] == "start_service"
    assert items[1]["status"] == "ok"


def test_remote_worker_service_action_is_whitelisted(tmp_path):
    executor = FakeRemoteWorkerExecutor()
    client = make_client(tmp_path, executor)

    response = client.post(
        "/api/remote-workers/service",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "action": "restart",
        },
    )

    assert response.status_code == 200
    assert response.json()["action"] == "restart"
    assert executor.calls[0][1].action == "restart"

    rejected = client.post(
        "/api/remote-workers/service",
        json={
            "host": "ocr-prod-40-3",
            "ssh_user": "ocr_user",
            "action": "rm -rf /",
        },
    )
    assert rejected.status_code == 422
