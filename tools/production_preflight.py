#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_REPO_DIR = "/opt/ocr-platform/ocrparser"


@dataclass(frozen=True)
class CheckResult:
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "warn", "skip"}

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class HostReport:
    host: str
    returncode: int
    facts: dict[str, str]
    checks: dict[str, CheckResult]
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and all(item.ok for item in self.checks.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "ok": self.ok,
            "returncode": self.returncode,
            "facts": dict(sorted(self.facts.items())),
            "checks": {key: value.to_dict() for key, value in sorted(self.checks.items())},
            "stderr": self.stderr,
        }


@dataclass(frozen=True)
class FleetReport:
    hosts: list[HostReport]

    @property
    def ok(self) -> bool:
        return all(host.ok for host in self.hosts)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "hosts": [host.to_dict() for host in self.hosts],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run read-only OCR production deployment preflight checks over SSH.",
    )
    parser.add_argument(
        "--host",
        dest="hosts",
        action="append",
        required=True,
        help="Host or SSH alias to inspect. Pass once per host.",
    )
    parser.add_argument("--user", default=None, help="SSH user. Omit when host is an SSH alias with User configured.")
    parser.add_argument("--identity-file", default=None, help="Optional SSH private key path.")
    parser.add_argument(
        "--ssh-option",
        dest="ssh_options",
        action="append",
        default=[],
        help="Extra SSH -o option, for example StrictHostKeyChecking=accept-new.",
    )
    parser.add_argument("--timeout", type=int, default=15, help="Per-host SSH command timeout in seconds.")
    parser.add_argument(
        "--shared-root",
        default="/shared/ocr-data",
        help="Real shared filesystem mount point visible to all workers.",
    )
    parser.add_argument(
        "--platform-root",
        default=None,
        help="Platform-owned subtree for manifests/jobs. Defaults to <shared-root>/ocr-platform.",
    )
    parser.add_argument(
        "--control-host",
        default=None,
        help="Host that should run PostgreSQL and the OCR control API. Defaults to the first --host.",
    )
    parser.add_argument(
        "--control-url",
        default=None,
        help="Control API URL reachable from every host. Defaults to http://<control-host>:8080.",
    )
    parser.add_argument("--repo-dir", default=DEFAULT_REPO_DIR, help="OCR parser checkout path on each host.")
    parser.add_argument("--expected-git-ref", default=None, help="Expected git commit/ref prefix on each host.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def remote_probe_script() -> str:
    return r"""set +e
SHARED_ROOT="$1"
PLATFORM_ROOT="$2"
CONTROL_URL="$3"
REPO_DIR="$4"
EXPECTED_GIT_REF="$5"
IS_CONTROL="$6"

emit_fact() {
  printf 'FACT\t%s\t%s\n' "$1" "$2"
}

emit_check() {
  printf 'CHECK\t%s\t%s\t%s\n' "$1" "$2" "$3"
}

check_rc() {
  name="$1"
  rc="$2"
  ok_detail="$3"
  fail_detail="$4"
  if [ "$rc" -eq 0 ]; then
    emit_check "$name" ok "$ok_detail"
  else
    emit_check "$name" fail "$fail_detail"
  fi
}

emit_fact hostname "$(hostname 2>/dev/null || true)"
emit_fact user "$(whoami 2>/dev/null || true)"
emit_fact date "$(date -Iseconds 2>/dev/null || true)"

mount_line="$(findmnt -n --target "$SHARED_ROOT" -o TARGET,FSTYPE,SOURCE 2>/dev/null | head -n 1)"
emit_fact shared_mount "$mount_line"
mount_target="$(printf '%s\n' "$mount_line" | awk '{print $1}')"
if [ -n "$mount_line" ] && [ "$mount_target" = "$SHARED_ROOT" ]; then
  emit_check shared_root_mounted ok "$mount_line"
else
  emit_check shared_root_mounted fail "$SHARED_ROOT resolves to ${mount_target:-<none>}"
fi

if [ -d "$PLATFORM_ROOT" ]; then
  emit_check platform_root_exists ok "$PLATFORM_ROOT exists"
else
  emit_check platform_root_exists fail "$PLATFORM_ROOT is missing"
fi

sudo -n -u ocr-agent test -r "$PLATFORM_ROOT" 2>/dev/null
check_rc platform_root_readable_by_ocr_agent "$?" readable "ocr-agent cannot read $PLATFORM_ROOT"
sudo -n -u ocr-agent test -w "$PLATFORM_ROOT" 2>/dev/null
check_rc platform_root_writable_by_ocr_agent "$?" writable "ocr-agent cannot write $PLATFORM_ROOT"
sudo -n -u ocr-platform test -r "$PLATFORM_ROOT" 2>/dev/null
check_rc platform_root_readable_by_ocr_platform "$?" readable "ocr-platform cannot read $PLATFORM_ROOT"
sudo -n -u ocr-platform test -w "$PLATFORM_ROOT" 2>/dev/null
check_rc platform_root_writable_by_ocr_platform "$?" writable "ocr-platform cannot write $PLATFORM_ROOT"

agent_processes="$(pgrep -af 'ocr_platform.agent' 2>/dev/null | head -n 5)"
emit_fact agent_processes "$agent_processes"
agent_git_ref="$(printf '%s\n' "$agent_processes" | sed -n 's/.*--git_ref \([^ ]*\).*/\1/p' | head -n 1)"
emit_fact agent_git_ref "$agent_git_ref"
if [ -n "$agent_processes" ]; then
  emit_check agent_process_running ok "ocr_platform.agent process found"
  case "$agent_processes" in
    *"--shared_root $SHARED_ROOT"*|*"--shared_root=$SHARED_ROOT"*|*"OCR_AGENT_SHARED_ROOTS=$SHARED_ROOT"*)
      emit_check agent_shared_root_matches ok "$SHARED_ROOT"
      ;;
    *)
      emit_check agent_shared_root_matches warn "agent process found, but $SHARED_ROOT was not visible in command"
      ;;
  esac
else
  emit_check agent_process_running fail "no ocr_platform.agent process found"
fi

if command -v git >/dev/null 2>&1 && [ -d "$REPO_DIR/.git" ]; then
  git_ref="$(git -c safe.directory="$REPO_DIR" -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
else
  git_ref=""
fi
if [ -z "$git_ref" ] && [ -n "$agent_git_ref" ]; then
  git_ref="$agent_git_ref"
fi
emit_fact git_ref "$git_ref"
if [ -n "$EXPECTED_GIT_REF" ]; then
  case "$git_ref" in
    "$EXPECTED_GIT_REF"*) emit_check git_ref_matches ok "$git_ref" ;;
    *) emit_check git_ref_matches fail "expected $EXPECTED_GIT_REF got ${git_ref:-<none>}" ;;
  esac
elif [ -n "$git_ref" ]; then
  emit_check git_ref_available ok "$git_ref"
else
  emit_check git_ref_available fail "$REPO_DIR is not a readable git checkout and agent --git_ref is absent"
fi

if [ -n "$CONTROL_URL" ] && command -v curl >/dev/null 2>&1; then
  http_code="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "$CONTROL_URL/api/system/database" 2>/dev/null || true)"
  emit_fact control_api_http_code "$http_code"
  case "$http_code" in
    2*|401|403) emit_check control_api_reachable ok "HTTP $http_code" ;;
    404) emit_check control_api_reachable warn "HTTP 404; control is reachable but /api/system/database is unavailable in this version" ;;
    *) emit_check control_api_reachable fail "HTTP ${http_code:-connection failed}" ;;
  esac
else
  emit_check control_api_reachable skip "curl or control URL unavailable"
fi

if [ "$IS_CONTROL" = "1" ]; then
  control_state="$(systemctl is-active ocr-platform-control 2>/dev/null || true)"
  emit_fact control_service_state "$control_state"
  if [ "$control_state" = "active" ]; then
    emit_check control_service_active ok active
  else
    emit_check control_service_active fail "${control_state:-unknown}"
  fi

  if command -v pg_isready >/dev/null 2>&1; then
    pg_ready="$(pg_isready -h 127.0.0.1 -p 5432 2>&1)"
    check_rc postgres_ready "$?" "$pg_ready" "$pg_ready"
  else
    emit_check postgres_ready fail "pg_isready not found"
  fi
else
  emit_check control_service_active skip "not control host"
  emit_check postgres_ready skip "not control host"
fi
"""


def build_ssh_command(
    *,
    host: str,
    user: str | None,
    identity_file: str | None,
    shared_root: str,
    platform_root: str,
    control_url: str,
    repo_dir: str,
    expected_git_ref: str | None,
    control_host: str,
    timeout: int,
    ssh_options: Sequence[str],
) -> list[str]:
    destination = f"{user}@{host}" if user else host
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
    ]
    for option in ssh_options:
        command.extend(["-o", option])
    if identity_file:
        command.extend(["-i", str(Path(identity_file).expanduser())])
    command.extend(
        [
            destination,
            "bash",
            "-s",
            "--",
            shared_root,
            platform_root,
            control_url,
            repo_dir,
            expected_git_ref or "",
            "1" if host == control_host else "0",
        ]
    )
    return command


def parse_probe_output(*, host: str, returncode: int, stdout: str, stderr: str) -> HostReport:
    facts: dict[str, str] = {}
    checks: dict[str, CheckResult] = {}
    if returncode == 0:
        checks["ssh_probe"] = CheckResult("ok", "connected")
    else:
        checks["ssh_probe"] = CheckResult("fail", f"ssh exited {returncode}")

    for raw_line in stdout.splitlines():
        parts = raw_line.split("\t", 3)
        if len(parts) >= 3 and parts[0] == "FACT":
            facts[parts[1]] = parts[2]
        elif len(parts) >= 4 and parts[0] == "CHECK":
            checks[parts[1]] = CheckResult(parts[2], parts[3])

    return HostReport(
        host=host,
        returncode=returncode,
        facts=facts,
        checks=checks,
        stderr=stderr.strip(),
    )


def run_host_probe(
    *,
    host: str,
    user: str | None,
    identity_file: str | None,
    shared_root: str,
    platform_root: str,
    control_url: str,
    repo_dir: str,
    expected_git_ref: str | None,
    control_host: str,
    timeout: int,
    ssh_options: Sequence[str],
) -> HostReport:
    command = build_ssh_command(
        host=host,
        user=user,
        identity_file=identity_file,
        shared_root=shared_root,
        platform_root=platform_root,
        control_url=control_url,
        repo_dir=repo_dir,
        expected_git_ref=expected_git_ref,
        control_host=control_host,
        timeout=timeout,
        ssh_options=ssh_options,
    )
    try:
        completed = subprocess.run(
            command,
            input=remote_probe_script(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return HostReport(
            host=host,
            returncode=124,
            facts={},
            checks={"ssh_probe": CheckResult("fail", f"timeout after {timeout}s")},
            stderr=str(exc),
        )

    return parse_probe_output(
        host=host,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def build_report(hosts: list[HostReport]) -> FleetReport:
    return FleetReport(hosts=hosts)


def print_human_report(report: FleetReport) -> None:
    print(f"overall: {'ok' if report.ok else 'failed'}")
    for host in report.hosts:
        print(f"\n{host.host}: {'ok' if host.ok else 'failed'}")
        for key, value in sorted(host.facts.items()):
            if value:
                print(f"  fact {key}: {value}")
        for key, check in sorted(host.checks.items()):
            print(f"  [{check.status}] {key}: {check.detail}")
        if host.stderr:
            print(f"  stderr: {host.stderr}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    control_host = args.control_host or args.hosts[0]
    control_url = args.control_url or f"http://{control_host}:8080"
    shared_root = args.shared_root.rstrip("/") or "/"
    platform_root = args.platform_root or f"{shared_root}/ocr-platform"
    reports = [
        run_host_probe(
            host=host,
            user=args.user,
            identity_file=args.identity_file,
            shared_root=shared_root,
            platform_root=platform_root,
            control_url=control_url,
            repo_dir=args.repo_dir,
            expected_git_ref=args.expected_git_ref,
            control_host=control_host,
            timeout=args.timeout,
            ssh_options=args.ssh_options,
        )
        for host in args.hosts
    ]
    report = build_report(reports)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
