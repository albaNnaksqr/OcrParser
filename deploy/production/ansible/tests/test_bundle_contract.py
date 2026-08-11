"""Static contract tests for the deploy/production/ansible bundle.

Every check here only reads and parses local YAML/text files: no SSH, no
systemd, no network, and no ansible-playbook invocation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BUNDLE_ROOT.parents[2]

PLAYBOOKS_DIR = BUNDLE_ROOT / "playbooks"
ROLES_DIR = BUNDLE_ROOT / "roles"

EXPECTED_PLAYBOOKS = (
    "privilege-check.yml",
    "preflight.yml",
    "control.yml",
    "workers.yml",
    "verify.yml",
    "canary.yml",
    "rollback.yml",
)
EXPECTED_ROLES = (
    "privilege_check",
    "release_identity",
    "deployment_defaults",
    "common",
    "control",
    "worker_identity",
    "worker",
    "verify",
    "canary",
)

SECRET_NAME_FRAGMENTS = ("token", "secret", "password", "database_url", "api_key")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_yaml_document(path: Path) -> Any:
    return yaml.safe_load(_read(path))


def _iter_tasks(node: Any) -> Iterator[dict]:
    """Yield every task dict, descending into block/rescue/always and plays."""

    if isinstance(node, list):
        for item in node:
            yield from _iter_tasks(item)
        return
    if not isinstance(node, dict):
        return
    if "tasks" in node or "pre_tasks" in node or "post_tasks" in node or "roles" in node:
        for key in ("pre_tasks", "tasks", "post_tasks"):
            if key in node:
                yield from _iter_tasks(node[key])
        return
    for key in ("block", "rescue", "always"):
        if key in node:
            yield from _iter_tasks(node[key])
    if "name" in node and ("block" in node or any(k not in ("block", "rescue", "always") and "." in k for k in node)):
        yield node


# ---- structural existence ---------------------------------------------------


def test_all_expected_playbooks_exist():
    found = {p.name for p in PLAYBOOKS_DIR.glob("*.yml")}
    assert set(EXPECTED_PLAYBOOKS) <= found


def test_every_expected_role_has_a_tasks_entrypoint():
    for role in EXPECTED_ROLES:
        tasks_file = ROLES_DIR / role / "tasks" / "main.yml"
        assert tasks_file.is_file(), f"missing {tasks_file}"


def test_every_playbook_is_valid_yaml_and_non_empty():
    for name in EXPECTED_PLAYBOOKS:
        document = _load_yaml_document(PLAYBOOKS_DIR / name)
        assert isinstance(document, list) and document


def test_every_role_tasks_file_is_valid_yaml_and_non_empty():
    for role in EXPECTED_ROLES:
        document = _load_yaml_document(ROLES_DIR / role / "tasks" / "main.yml")
        assert isinstance(document, list) and document


# ---- security contract: no secret facts, hostvars, argv, debug, or report --


def test_no_role_ever_assigns_a_secret_shaped_value_via_set_fact():
    for role in EXPECTED_ROLES:
        tasks = _load_yaml_document(ROLES_DIR / role / "tasks" / "main.yml")
        for task in _iter_tasks(tasks):
            for action in ("ansible.builtin.set_fact", "set_fact"):
                if action not in task:
                    continue
                rendered = json.dumps(task[action]).lower()
                assert "lookup(" not in rendered, (
                    f"{role}: set_fact task {task.get('name')!r} resolves a lookup() "
                    "into a fact"
                )
                for fragment in SECRET_NAME_FRAGMENTS:
                    assert fragment not in rendered, (
                        f"{role}: set_fact task {task.get('name')!r} assigns a "
                        f"secret-shaped key ({fragment})"
                    )


def test_every_lookup_env_of_a_secret_ref_is_task_local_and_no_log():
    for role in EXPECTED_ROLES:
        tasks = _load_yaml_document(ROLES_DIR / role / "tasks" / "main.yml")
        for task in _iter_tasks(tasks):
            task_vars = task.get("vars") or {}
            uses_secret_lookup = any(
                isinstance(value, str) and "lookup('env'" in value and (
                    "secret_var" in value or "env_var" in value
                )
                for value in task_vars.values()
            )
            if uses_secret_lookup:
                assert task.get("no_log") is True, (
                    f"{role}: task {task.get('name')!r} resolves a secret env lookup "
                    "without no_log: true"
                )


def test_no_task_argv_or_command_line_ever_embeds_a_lookup_env_call():
    for role in EXPECTED_ROLES:
        tasks = _load_yaml_document(ROLES_DIR / role / "tasks" / "main.yml")
        for task in _iter_tasks(tasks):
            for action in ("ansible.builtin.command", "command", "ansible.builtin.shell", "shell"):
                if action not in task:
                    continue
                rendered = json.dumps(task[action])
                assert "lookup(" not in rendered, (
                    f"{role}: {action} task {task.get('name')!r} embeds a lookup() "
                    "call directly in argv/cmdline"
                )


def test_no_debug_task_ever_references_a_secret_shaped_variable():
    for role in EXPECTED_ROLES:
        tasks = _load_yaml_document(ROLES_DIR / role / "tasks" / "main.yml")
        for task in _iter_tasks(tasks):
            for action in ("ansible.builtin.debug", "debug"):
                if action not in task:
                    continue
                rendered = json.dumps(task[action]).lower()
                for fragment in SECRET_NAME_FRAGMENTS:
                    assert fragment not in rendered, (
                        f"{role}: debug task {task.get('name')!r} references a "
                        f"secret-shaped value ({fragment})"
                    )


def test_verify_role_report_fact_never_includes_hostnames_ids_paths_or_secrets():
    tasks = _load_yaml_document(ROLES_DIR / "verify" / "tasks" / "main.yml")
    report_task = next(
        t for t in _iter_tasks(tasks)
        if t.get("name") == "Build the release/control/migrations/workers report matching the renderer's input schema"
    )
    report = report_task["ansible.builtin.set_fact"]["ocr_verify_report"]
    assert set(report) == {"release_tag", "release_commit", "wheel_sha256", "control", "migrations", "workers"}
    assert set(report["control"]) == {"healthy", "ready", "source_revision_matches"}
    assert set(report["migrations"]) == {"verified"}
    assert set(report["workers"]) == {
        "total", "online", "unique_server_ids", "shared_root_ok", "spool_quarantine", "version_consistent",
    }
    # Every leaf value must be a scalar count/boolean-shaped expression, never a
    # raw hostname/id/path/secret pulled straight from inventory or hostvars.
    for submap_key in ("control", "migrations", "workers"):
        for value in report[submap_key].values():
            assert isinstance(value, str), f"verify report field {submap_key} has a non-expression leaf"
    rendered = json.dumps(report).lower()
    for forbidden in ("ocr_control_url", "inventory_hostname", "token", "database_url"):
        assert forbidden not in rendered, f"verify report fact references forbidden field: {forbidden}"


def test_verify_role_invokes_the_renderer_with_verify_and_output_dir_flags():
    tasks_text = _read(ROLES_DIR / "verify" / "tasks" / "main.yml")
    assert '"--verify"' in tasks_text
    assert '"--output-dir"' in tasks_text

    defaults_text = _read(ROLES_DIR / "verify" / "defaults" / "main.yml")
    assert "render_deployment_report.py" in defaults_text


def test_verify_accepts_all_non_stale_ready_worker_statuses():
    tasks_text = _read(ROLES_DIR / "verify" / "tasks" / "main.yml")
    assert "['idle', 'online', 'busy']" in tasks_text
    assert "selectattr('is_stale', 'equalto', false)" in tasks_text
    assert "difference(ocr_verify_ready_server_ids)" in tasks_text


def test_verify_excludes_the_internal_server_pool_from_fleet_counts():
    tasks_text = _read(ROLES_DIR / "verify" / "tasks" / "main.yml")
    assert tasks_text.count("rejectattr('id', 'equalto', '__server_pool__')") >= 2


def test_cross_host_worker_ids_use_the_normalized_effective_list():
    identity_text = _read(ROLES_DIR / "worker_identity" / "tasks" / "main.yml")
    assert "hostvars[item]['ocr_worker_server_id'] | default(item, true)" in identity_text

    for role in ("worker", "verify", "canary"):
        tasks_text = _read(ROLES_DIR / role / "tasks" / "main.yml")
        assert "ocr_worker_effective_server_ids" in tasks_text
        assert "map('extract', hostvars, 'ocr_worker_server_id')" not in tasks_text


def test_verify_and_canary_load_shared_deployment_path_defaults():
    defaults = _load_yaml_document(
        ROLES_DIR / "deployment_defaults" / "defaults" / "main.yml"
    )
    common_defaults = _load_yaml_document(ROLES_DIR / "common" / "defaults" / "main.yml")
    for key in (
        "ocr_service_user",
        "ocr_service_group",
        "ocr_install_root",
        "ocr_source_dir",
        "ocr_venv_dir",
        "ocr_wheel_dir",
    ):
        assert defaults[key] == common_defaults[key]

    for playbook in ("verify.yml", "canary.yml"):
        play = _load_yaml_document(PLAYBOOKS_DIR / playbook)[0]
        assert play["roles"].index("deployment_defaults") < play["roles"].index("release_identity")

    worker_defaults = _load_yaml_document(
        ROLES_DIR / "worker_identity" / "defaults" / "main.yml"
    )
    assert "ocr_worker_server_id" in worker_defaults
    assert "ocr_worker_spool_dir" in worker_defaults


# ---- check-mode guarding -----------------------------------------------------


def test_every_role_warns_when_running_under_check_mode():
    for role in ("common", "control", "worker", "verify", "canary"):
        text = _read(ROLES_DIR / role / "tasks" / "main.yml")
        assert "ansible_check_mode" in text, f"{role} never branches on ansible_check_mode"


def test_common_role_guards_the_post_download_wheel_digest_assertion():
    tasks = _load_yaml_document(ROLES_DIR / "common" / "tasks" / "main.yml")
    task = next(
        t for t in _iter_tasks(tasks)
        if t.get("name") == "Assert the on-disk wheel digest matches the inventory"
    )
    assert task.get("when") == "not ansible_check_mode"


def test_common_role_refuses_unconstrained_release_installation():
    defaults = _load_yaml_document(ROLES_DIR / "common" / "defaults" / "main.yml")
    assert defaults["ocr_platform_constraints_path"].endswith(
        "/deploy/production/ansible/constraints/platform.txt"
    )

    constraints = BUNDLE_ROOT / "constraints" / "platform.txt"
    assert constraints.is_file()
    pins = {
        line.strip()
        for line in _read(constraints).splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert "fastapi==0.141.1" in pins
    assert "sqlalchemy==2.0.51" in pins
    assert "psycopg==3.3.4" in pins
    assert all("==" in pin for pin in pins)

    tasks = _load_yaml_document(ROLES_DIR / "common" / "tasks" / "main.yml")
    refusal = next(
        task
        for task in _iter_tasks(tasks)
        if task.get("name") == "Refuse an unconstrained production installation"
    )
    assert refusal.get("when") == "not ansible_check_mode"
    assert refusal["ansible.builtin.assert"]["that"] == [
        "ocr_platform_constraints_stat.stat.exists",
        "ocr_platform_constraints_stat.stat.isreg",
        "ocr_platform_constraints_stat.stat.size | int > 0",
    ]

    requirement = next(
        task
        for task in _iter_tasks(tasks)
        if task.get("name") == "Write the constrained release install requirement"
    )
    content = requirement["ansible.builtin.copy"]["content"]
    assert "--constraint {{ ocr_platform_constraints_path }}" in content
    assert "{{ ocr_wheel_path }}[platform]" in content


def test_assert_conditions_do_not_use_string_valued_regex_search():
    """Ansible 2.19 requires assert conditions to evaluate to real bools."""

    yaml_files = list(PLAYBOOKS_DIR.glob("*.yml"))
    yaml_files.extend(ROLES_DIR.glob("*/tasks/main.yml"))
    for path in yaml_files:
        assert "| regex_search(" not in _read(path), (
            f"{path} uses regex_search as a condition; use the boolean "
            "`is match(...)` test under ansible-core 2.19"
        )


def test_multiline_python_provenance_probes_preserve_newlines():
    tasks = _load_yaml_document(ROLES_DIR / "common" / "tasks" / "main.yml")
    names = {
        "Read the currently installed build provenance",
        "Read the installed build provenance",
    }
    probes = [task for task in _iter_tasks(tasks) if task.get("name") in names]
    assert len(probes) == 2
    for task in probes:
        argv = task["ansible.builtin.command"]["argv"]
        script = argv[2]
        assert "import json\ntry:" in script
        compile(script, f"<{task['name']}>", "exec")


def test_common_preflight_checks_native_parser_libraries():
    tasks = _load_yaml_document(ROLES_DIR / "common" / "tasks" / "main.yml")
    probe = next(
        task for task in _iter_tasks(tasks)
        if task.get("name") == "Probe native libraries required by the parser runtime"
    )
    script = probe["ansible.builtin.command"]["argv"][2]
    assert "import cv2" in script
    assert "pyzbar.pyzbar" in script
    assert probe.get("failed_when") is False

    guard = next(
        task for task in _iter_tasks(tasks)
        if task.get("name") == "Assert the parser's native runtime libraries are available"
    )
    assert "ocr_native_runtime_probe.rc == 0" in guard["ansible.builtin.assert"]["that"]


# ---- migration ordering, canary opt-in, rollback guard ---------------------


def test_workers_playbook_documents_running_after_preflight_and_control():
    text = _read(PLAYBOOKS_DIR / "workers.yml")
    assert "preflight.yml" in text
    assert "control.yml" in text


def test_canary_playbook_refuses_to_run_unless_explicitly_enabled():
    document = _load_yaml_document(PLAYBOOKS_DIR / "canary.yml")
    pre_tasks = document[0]["pre_tasks"]
    guard = next(t for t in pre_tasks if "ocr_canary_enabled" in json.dumps(t))
    assert "ocr_canary_enabled" in json.dumps(guard["ansible.builtin.assert"]["that"])


def test_canary_walkthrough_runs_as_the_service_user_with_owned_tree():
    tasks = _load_yaml_document(ROLES_DIR / "canary" / "tasks" / "main.yml")
    create_task = next(
        task for task in _iter_tasks(tasks)
        if task.get("name") == "Create the isolated canary subtree"
    )
    assert create_task["ansible.builtin.file"]["owner"] == "{{ ocr_service_user }}"
    assert create_task["ansible.builtin.file"]["recurse"] is True

    run_task = next(
        task for task in _iter_tasks(tasks)
        if task.get("name") == "Run the synthetic-document walkthrough against the deployed release"
    )
    assert run_task["become"] is True
    assert run_task["become_user"] == "{{ ocr_service_user }}"


def test_canary_writes_an_allow_listed_result_and_combines_it_with_verify():
    tasks = _load_yaml_document(ROLES_DIR / "canary" / "tasks" / "main.yml")
    report_task = next(
        task for task in _iter_tasks(tasks)
        if task.get("name") == "Build the allow-listed canary report"
    )
    report = report_task["ansible.builtin.set_fact"]["ocr_canary_report"]
    assert set(report) == {"enabled", "status", "artifacts_present"}
    rendered = json.dumps(report).lower()
    for forbidden in ("host", "url", "path", "token", "database", "stdout"):
        assert forbidden not in rendered

    tasks_text = _read(ROLES_DIR / "canary" / "tasks" / "main.yml")
    assert '"--verify"' in tasks_text
    assert '"--canary"' in tasks_text
    assert "verify-{{ ocr_release_commit }}.json" in _read(
        ROLES_DIR / "canary" / "defaults" / "main.yml"
    )


def test_rollback_playbook_requires_explicit_release_identity_and_one_compat_path():
    document = _load_yaml_document(PLAYBOOKS_DIR / "rollback.yml")
    localhost_play = document[0]
    assert localhost_play["hosts"] == "localhost"
    tasks = localhost_play["tasks"]

    identity_task = next(
        t for t in tasks if t["name"] == "Assert the rollback release identity is complete"
    )
    identity_conditions = " ".join(identity_task["ansible.builtin.assert"]["that"])
    for required in (
        "ocr_rollback_release_tag",
        "ocr_rollback_release_commit",
        "ocr_rollback_wheel_url",
        "ocr_rollback_wheel_sha256",
    ):
        assert required in identity_conditions

    compat_task = next(
        t for t in tasks if t["name"] == "Assert exactly one migration-compatibility path is asserted"
    )
    compat_conditions = " ".join(compat_task["ansible.builtin.assert"]["that"])
    assert "ocr_rollback_migration_compatible" in compat_conditions
    assert "ocr_rollback_restore_plan_reference" in compat_conditions


def test_rollback_playbook_rolls_control_back_before_workers():
    document = _load_yaml_document(PLAYBOOKS_DIR / "rollback.yml")
    play_names = [play["name"] for play in document]
    assert play_names.index("Roll Control back to the prior release") < play_names.index(
        "Roll workers back to the prior release"
    )


def test_control_and_rollback_control_plays_use_serial_one_and_zero_fail_percentage():
    control_play = _load_yaml_document(PLAYBOOKS_DIR / "control.yml")[0]
    assert control_play["serial"] == 1
    assert control_play["max_fail_percentage"] == 0

    rollback_control_play = next(
        play for play in _load_yaml_document(PLAYBOOKS_DIR / "rollback.yml")
        if play.get("name") == "Roll Control back to the prior release"
    )
    assert rollback_control_play["serial"] == 1
    assert rollback_control_play["max_fail_percentage"] == 0


def test_workers_and_rollback_worker_plays_default_serial_and_max_fail_safely():
    for name, play_name in (
        ("workers.yml", "Roll out workers"),
        ("rollback.yml", "Roll workers back to the prior release"),
    ):
        document = _load_yaml_document(PLAYBOOKS_DIR / name)
        play = next(p for p in document if p.get("name") == play_name)
        assert "default(1)" in play["serial"]
        assert "default(0)" in play["max_fail_percentage"]


# ---- example inventory / group_vars are a safe, self-consistent contract --


def test_example_group_vars_has_no_rollout_vars_so_playbook_defaults_are_exercised():
    group_vars = _load_yaml_document(BUNDLE_ROOT / "group_vars" / "all.example.yml")
    assert "ocr_rollout_serial" not in group_vars
    assert "ocr_rollout_max_fail_percentage" not in group_vars


def test_example_inventory_uses_the_safe_rollout_defaults():
    inventory = _load_yaml_document(BUNDLE_ROOT / "inventory.example.yml")
    all_vars = inventory["all"]["vars"]
    assert "ocr_rollout_serial" not in all_vars
    assert "ocr_rollout_max_fail_percentage" not in all_vars
    workers_text = _read(PLAYBOOKS_DIR / "workers.yml")
    assert "ocr_rollout_serial | default(1)" in workers_text
    assert "ocr_rollout_max_fail_percentage | default(0)" in workers_text


def test_mutating_playbooks_check_privilege_before_any_role_mutates_a_host():
    for name in ("preflight.yml", "control.yml", "workers.yml", "canary.yml", "rollback.yml"):
        document = _load_yaml_document(PLAYBOOKS_DIR / name)
        mutating_plays = [play for play in document if play.get("become") is True]
        assert mutating_plays, name
        for play in mutating_plays:
            rendered = json.dumps(play.get("pre_tasks", []))
            assert "privilege_check" in rendered, f"{name}: {play.get('name')}"
            assert play.get("gather_facts") is False


def test_privilege_role_is_read_only_and_never_uses_become():
    tasks = _load_yaml_document(ROLES_DIR / "privilege_check" / "tasks" / "main.yml")
    rendered = json.dumps(tasks)
    assert "ocr_canary_enabled" in rendered
    assert "ocr_canary_subdir" in rendered
    assert "ansible_become_password | string | length > 0" in rendered
    forbidden_actions = {
        "ansible.builtin.file",
        "ansible.builtin.copy",
        "ansible.builtin.template",
        "ansible.builtin.systemd",
        "ansible.builtin.user",
        "ansible.builtin.group",
        "ansible.builtin.package",
    }
    for task in _iter_tasks(tasks):
        assert task.get("become") is False
        assert forbidden_actions.isdisjoint(task)


def test_release_identity_role_precedes_runtime_roles_everywhere():
    expected_runtime_role = {
        "preflight.yml": "common",
        "control.yml": "common",
        "workers.yml": "common",
        "verify.yml": "verify",
        "canary.yml": "canary",
    }
    for name, runtime_role in expected_runtime_role.items():
        document = _load_yaml_document(PLAYBOOKS_DIR / name)
        play = document[-1]
        assert play["roles"].index("release_identity") < play["roles"].index(runtime_role)


# ---- documentation links ----------------------------------------------------


def test_bundle_readmes_exist_in_english_and_chinese():
    assert (BUNDLE_ROOT / "README.md").is_file()
    assert (BUNDLE_ROOT / "README.zh-CN.md").is_file()


def test_root_readmes_link_to_the_production_deployment_bundle():
    for readme in ("README.md", "README.zh-CN.md"):
        text = _read(REPO_ROOT / readme)
        assert "deploy/production" in text
