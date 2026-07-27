from __future__ import annotations

import ast
import copy
import json
import re
import shutil
from pathlib import Path

import pytest

from tools import control_architecture_debt


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "control_architecture_debt.json"
)


def _baseline() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _copy_domains(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    target = root / "ocr_platform" / "control" / "domains"
    target.parent.mkdir(parents=True)
    shutil.copytree(
        ROOT / "ocr_platform" / "control" / "domains",
        target,
    )
    return root


def _replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert source.count(old) >= 1
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def test_control_runtime_composition_has_no_database_registry_or_leaf_transaction() -> None:
    database_path = ROOT / "ocr_platform" / "control" / "database.py"
    bootstrap_path = ROOT / "ocr_platform" / "control" / "bootstrap.py"
    readiness_path = ROOT / "ocr_platform" / "control" / "readiness.py"
    database_tree = ast.parse(database_path.read_text(encoding="utf-8"))
    bootstrap_tree = ast.parse(bootstrap_path.read_text(encoding="utf-8"))

    module_assignments = {
        target.id
        for statement in database_tree.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        for target in (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if isinstance(target, ast.Name)
    }
    function_names = {
        node.name
        for node in database_tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert {
        "SessionLocal",
        "engine",
        "_configured_database_url",
        "_configured_database_source",
    }.isdisjoint(module_assignments)
    assert {
        "configure_database",
        "_get_configured_database",
        "get_session",
    }.isdisjoint(function_names)

    functions = {
        node.name: node
        for node in bootstrap_tree.body
        if isinstance(node, ast.FunctionDef)
    }

    def session_methods(function_name: str) -> list[str]:
        return [
            node.func.attr
            for node in ast.walk(functions[function_name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"begin", "commit", "rollback"}
        ]

    assert session_methods("seed_default_model_profiles") == []
    assert session_methods("bootstrap_control_database") == ["begin"]
    assert "database.engine" not in readiness_path.read_text(encoding="utf-8")


def test_architecture_debt_fixture_matches_generated_sites() -> None:
    expected = _baseline()
    actual = control_architecture_debt.build_architecture_debt()

    control_architecture_debt.validate_fixture_shape(expected)
    control_architecture_debt.validate_decreasing(actual, expected)


def test_architecture_debt_counts_match_independent_audit() -> None:
    debt = _baseline()
    imports = debt["cross_domain_imports"]
    transactions = debt["transactions"]
    query = debt["query_mutations"]
    writes = debt["policy_external_status_writes"]

    assert {
        "statements": imports["statement_count"],
        "symbols": imports["symbol_count"],
        "private": imports["private_symbol_count"],
        "lazy": imports["lazy_wrapper_count"],
    } == {
        "statements": 42,
        "symbols": 44,
        "private": 17,
        "lazy": 37,
    }
    assert imports["edges"] == [
        {
            "source": "diagnostics",
            "target": "workers",
            "statement_count": 1,
            "symbol_count": 3,
        },
        {
            "source": "jobs",
            "target": "manifests",
            "statement_count": 9,
            "symbol_count": 9,
        },
        {
            "source": "jobs",
            "target": "model_profiles",
            "statement_count": 2,
            "symbol_count": 2,
        },
        {
            "source": "jobs",
            "target": "workers",
            "statement_count": 9,
            "symbol_count": 9,
        },
        {
            "source": "manifests",
            "target": "jobs",
            "statement_count": 6,
            "symbol_count": 6,
        },
        {
            "source": "manifests",
            "target": "workers",
            "statement_count": 10,
            "symbol_count": 10,
        },
        {
            "source": "workers",
            "target": "manifests",
            "statement_count": 3,
            "symbol_count": 3,
        },
        {
            "source": "workers",
            "target": "model_profiles",
            "statement_count": 2,
            "symbol_count": 2,
        },
    ]
    assert imports["strongly_connected_components"] == [
        ["jobs", "manifests", "workers"]
    ]
    assert transactions["total"] == 49
    assert transactions["operations"] == {
        "commit": 28,
        "flush": 14,
        "rollback": 7,
    }
    assert query["direct_dml_count"] == 0
    assert query["semantic_allowlist_count"] == 3
    assert {
        site["symbol"] for site in query["semantic_allowlist_sites"]
    } == {
        "get_job_summary",
        "list_job_summaries",
        "list_job_summaries_page",
    }
    assert writes["count"] == 40
    assert sum(
        site["write_kind"] == "attribute_assignment"
        for site in writes["sites"]
    ) == 25
    assert sum(
        site["write_kind"] == "sql_values"
        for site in writes["sites"]
    ) == 15


def test_stable_ids_exclude_line_numbers_and_fingerprints_are_ast_hashes() -> None:
    debt = _baseline()
    site_groups = [
        debt["cross_domain_imports"]["sites"],
        debt["cross_domain_imports"]["statements"],
        debt["transactions"]["sites"],
        debt["query_mutations"]["semantic_allowlist_sites"],
        debt["policy_external_status_writes"]["sites"],
    ]
    for site in (item for group in site_groups for item in group):
        assert re.fullmatch(r"[^:]+(?:\.[^:]+)*:[^:]+:[^:]+:[1-9]\d*", site["id"])
        assert not re.search(r":\d+:", site["id"])
        assert re.fullmatch(r"[0-9a-f]{64}", site["fingerprint"])
        assert re.fullmatch(r".+\.py:\d+", site["source"])


def test_new_cross_domain_import_and_edge_fail_decreasing_gate(
    tmp_path: Path,
) -> None:
    root = _copy_domains(tmp_path)
    jobs_core = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    jobs_core.write_text(
        jobs_core.read_text(encoding="utf-8")
        + "\n\ndef architecture_debt_mutation():\n"
        + "    from ..workers import core\n"
        + "    return core\n",
        encoding="utf-8",
    )
    actual = control_architecture_debt.build_architecture_debt(root)

    with pytest.raises(ValueError, match="new or replaced sites"):
        control_architecture_debt.validate_decreasing(actual, _baseline())


def test_replaced_transaction_and_policy_write_fail(
    tmp_path: Path,
) -> None:
    baseline = _baseline()

    transaction_root = _copy_domains(tmp_path / "transaction")
    manifest_core = (
        transaction_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    )
    _replace_once(manifest_core, "session.commit()", "session.flush()")
    transaction_actual = (
        control_architecture_debt.build_architecture_debt(transaction_root)
    )
    with pytest.raises(ValueError, match="transactions.sites"):
        control_architecture_debt.validate_decreasing(
            transaction_actual,
            baseline,
        )

    policy_root = _copy_domains(tmp_path / "policy")
    worker_core = (
        policy_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "workers"
        / "core.py"
    )
    _replace_once(
        worker_core,
        'server.status = "online"',
        'server.status = "offline"',
    )
    policy_actual = control_architecture_debt.build_architecture_debt(
        policy_root
    )
    with pytest.raises(
        ValueError,
        match="policy_external_status_writes.sites",
    ):
        control_architecture_debt.validate_decreasing(
            policy_actual,
            baseline,
        )


def test_new_direct_and_semantic_query_mutations_fail(
    tmp_path: Path,
) -> None:
    baseline = _baseline()

    direct_root = _copy_domains(tmp_path / "direct")
    jobs_queries = (
        direct_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    jobs_queries.write_text(
        jobs_queries.read_text(encoding="utf-8")
        + "\n\ndef bad_query(session):\n"
        + "    stmt = update(Job).values(label='changed')\n"
        + "    session.execute(stmt)\n",
        encoding="utf-8",
    )
    direct_actual = control_architecture_debt.build_architecture_debt(
        direct_root
    )
    with pytest.raises(ValueError, match="direct_dml_sites"):
        control_architecture_debt.validate_decreasing(
            direct_actual,
            baseline,
        )
    assert direct_actual["query_mutations"]["direct_dml_count"] == 1

    semantic_root = _copy_domains(tmp_path / "semantic")
    semantic_queries = (
        semantic_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    semantic_queries.write_text(
        semantic_queries.read_text(encoding="utf-8")
        + "\nfrom .core import request_stop\n",
        encoding="utf-8",
    )
    semantic_actual = control_architecture_debt.build_architecture_debt(
        semantic_root
    )
    assert semantic_actual["query_mutations"][
        "semantic_allowlist_count"
        ] == 4
    with pytest.raises(
        ValueError,
        match="semantic_allowlist_sites",
    ):
        control_architecture_debt.validate_decreasing(
            semantic_actual,
            baseline,
        )


def test_deleting_debt_site_is_allowed(tmp_path: Path) -> None:
    baseline = _baseline()
    root = _copy_domains(tmp_path)
    jobs_router = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "router.py"
    )
    _replace_once(
        jobs_router,
        "from ..workers.core import preflight_job\n",
        "",
    )
    actual = control_architecture_debt.build_architecture_debt(root)

    control_architecture_debt.validate_decreasing(actual, baseline)
    assert actual["cross_domain_imports"]["symbol_count"] == 43

    transaction_root = _copy_domains(tmp_path / "transaction")
    manifest_core = (
        transaction_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    )
    _replace_once(manifest_core, "session.commit()", "pass")
    transaction_actual = (
        control_architecture_debt.build_architecture_debt(transaction_root)
    )
    control_architecture_debt.validate_decreasing(
        transaction_actual,
        baseline,
    )
    assert transaction_actual["transactions"]["total"] == 48

    policy_root = _copy_domains(tmp_path / "policy")
    worker_core = (
        policy_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "workers"
        / "core.py"
    )
    _replace_once(worker_core, 'server.status = "online"', "pass")
    policy_actual = control_architecture_debt.build_architecture_debt(
        policy_root
    )
    control_architecture_debt.validate_decreasing(policy_actual, baseline)
    assert policy_actual["policy_external_status_writes"]["count"] == 39

    duplicate_root = _copy_domains(tmp_path / "duplicate")
    duplicate_core = (
        duplicate_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    )
    duplicate_source = duplicate_core.read_text(encoding="utf-8")
    first_bulk_write = """    session.execute(
        update(WorkShard)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )
"""
    assert first_bulk_write in duplicate_source
    duplicate_core.write_text(
        duplicate_source.replace(first_bulk_write, "", 1),
        encoding="utf-8",
    )
    duplicate_actual = control_architecture_debt.build_architecture_debt(
        duplicate_root
    )
    control_architecture_debt.validate_decreasing(
        duplicate_actual,
        baseline,
    )
    assert duplicate_actual["policy_external_status_writes"]["count"] == 39


def test_line_number_changes_are_evidence_only(tmp_path: Path) -> None:
    root = _copy_domains(tmp_path)
    jobs_router = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "router.py"
    )
    jobs_router.write_text(
        "\n\n" + jobs_router.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    actual = control_architecture_debt.build_architecture_debt(root)

    control_architecture_debt.validate_decreasing(actual, _baseline())


def test_new_edge_and_scc_metadata_are_independently_rejected() -> None:
    baseline = _baseline()

    edge_mutation = copy.deepcopy(baseline)
    edge_mutation["cross_domain_imports"]["edges"].append(
        {
            "source": "model_profiles",
            "target": "jobs",
            "statement_count": 1,
            "symbol_count": 1,
        }
    )
    with pytest.raises(ValueError, match="edge metadata is inconsistent"):
        control_architecture_debt.validate_fixture_shape(edge_mutation)

    baseline_scc = [["jobs", "manifests", "workers"]]
    control_architecture_debt.validate_scc_decreasing(
        [["jobs", "manifests"]],
        baseline_scc,
    )
    with pytest.raises(ValueError, match="new cross-domain SCC"):
        control_architecture_debt.validate_scc_decreasing(
            [["jobs", "manifests", "model_profiles", "workers"]],
            baseline_scc,
        )


def test_deleting_edges_may_split_an_existing_cycle(tmp_path: Path) -> None:
    root = tmp_path / "cycle"
    domain_root = root / "ocr_platform" / "control" / "domains"
    sources = {
        "jobs": (
            "from ..manifests.core import manifest_value\n"
            "from ..workers.core import worker_value\n"
        ),
        "manifests": (
            "from ..jobs.core import job_value\n"
            "from ..workers.core import worker_value\n"
        ),
        "workers": "from ..manifests.core import manifest_value\n",
    }
    for domain, source in sources.items():
        path = domain_root / domain
        path.mkdir(parents=True)
        (path / "core.py").write_text(source, encoding="utf-8")
    baseline = control_architecture_debt.build_architecture_debt(root)
    assert baseline["cross_domain_imports"][
        "strongly_connected_components"
    ] == [["jobs", "manifests", "workers"]]

    (domain_root / "jobs" / "core.py").write_text(
        "from ..manifests.core import manifest_value\n",
        encoding="utf-8",
    )
    (domain_root / "manifests" / "core.py").write_text(
        "from ..jobs.core import job_value\n",
        encoding="utf-8",
    )
    (domain_root / "workers" / "core.py").write_text("", encoding="utf-8")
    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["cross_domain_imports"][
        "strongly_connected_components"
    ] == [["jobs", "manifests"]]
    control_architecture_debt.validate_decreasing(actual, baseline)


def test_query_equivalent_dml_and_module_import_forms_are_blocked(
    tmp_path: Path,
) -> None:
    baseline = _baseline()

    bulk_root = _copy_domains(tmp_path / "bulk")
    bulk_queries = (
        bulk_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    bulk_queries.write_text(
        bulk_queries.read_text(encoding="utf-8")
        + "\n\ndef bulk_mutation(session):\n"
        + "    session.query(Job).update({'status': 'stopped'})\n",
        encoding="utf-8",
    )
    bulk_actual = control_architecture_debt.build_architecture_debt(
        bulk_root
    )
    assert bulk_actual["query_mutations"]["direct_dml_count"] == 1
    assert bulk_actual["policy_external_status_writes"]["count"] == 41
    with pytest.raises(ValueError, match="direct_dml_sites"):
        control_architecture_debt.validate_decreasing(
            bulk_actual,
            baseline,
        )

    module_root = _copy_domains(tmp_path / "module")
    module_queries = (
        module_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    module_queries.write_text(
        module_queries.read_text(encoding="utf-8")
        + "\nfrom . import core as core_module\n"
        + "\ndef semantic_mutation(session):\n"
        + "    return core_module.request_stop(session, 'job')\n",
        encoding="utf-8",
    )
    module_actual = control_architecture_debt.build_architecture_debt(
        module_root
    )
    assert module_actual["query_mutations"][
        "semantic_allowlist_count"
        ] == 4
    with pytest.raises(
        ValueError,
        match="semantic_allowlist_sites",
    ):
        control_architecture_debt.validate_decreasing(
            module_actual,
            baseline,
        )

    core_root = _copy_domains(tmp_path / "core-bulk")
    core_path = (
        core_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    core_path.write_text(
        core_path.read_text(encoding="utf-8")
        + "\n\ndef core_bulk_mutation(session):\n"
        + "    session.query(Job).update({'status': 'stopped'})\n",
        encoding="utf-8",
    )
    core_actual = control_architecture_debt.build_architecture_debt(
        core_root
    )
    assert core_actual["query_mutations"]["direct_dml_count"] == 0
    assert core_actual["policy_external_status_writes"]["count"] == 41


def test_additional_escape_hatches_are_blocked(tmp_path: Path) -> None:
    baseline = _baseline()

    domain_root = _copy_domains(tmp_path / "domain")
    new_domain = (
        domain_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "new_domain"
    )
    new_domain.mkdir()
    (new_domain / "core.py").write_text(
        "from ..jobs.core import get_job_or_raise\n",
        encoding="utf-8",
    )
    domain_actual = control_architecture_debt.build_architecture_debt(
        domain_root
    )
    with pytest.raises(ValueError, match="new or replaced sites"):
        control_architecture_debt.validate_decreasing(
            domain_actual,
            baseline,
        )

    alias_root = _copy_domains(tmp_path / "alias")
    alias_core = (
        alias_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    alias_core.write_text(
        alias_core.read_text(encoding="utf-8")
        + "\n\ndef aliased_transaction(session: Session):\n"
        + "    db = session\n"
        + "    db.commit()\n",
        encoding="utf-8",
    )
    alias_actual = control_architecture_debt.build_architecture_debt(
        alias_root
    )
    assert alias_actual["transactions"]["total"] == 50
    with pytest.raises(ValueError, match="transactions.sites"):
        control_architecture_debt.validate_decreasing(
            alias_actual,
            baseline,
        )

    status_root = _copy_domains(tmp_path / "status")
    status_core = (
        status_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    status_core.write_text(
        status_core.read_text(encoding="utf-8")
        + "\n\ndef escaped_status_writes(session, job):\n"
        + "    setattr(job, 'status', 'stopped')\n"
        + "    session.execute(update(Job).values({'status': 'stopped'}))\n",
        encoding="utf-8",
    )
    status_actual = control_architecture_debt.build_architecture_debt(
        status_root
    )
    assert status_actual["policy_external_status_writes"]["count"] == 42
    with pytest.raises(
        ValueError,
        match="policy_external_status_writes.sites",
    ):
        control_architecture_debt.validate_decreasing(
            status_actual,
            baseline,
        )

    raw_root = _copy_domains(tmp_path / "raw")
    raw_queries = (
        raw_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    raw_queries.write_text(
        raw_queries.read_text(encoding="utf-8")
        + "\n\ndef raw_query_mutation(session):\n"
        + "    statement = text('UPDATE jobs SET status = 1')\n"
        + "    session.execute(statement)\n",
        encoding="utf-8",
    )
    raw_actual = control_architecture_debt.build_architecture_debt(raw_root)
    assert raw_actual["query_mutations"]["direct_dml_count"] == 1
    with pytest.raises(ValueError, match="direct_dml_sites"):
        control_architecture_debt.validate_decreasing(
            raw_actual,
            baseline,
        )

    query_dir_root = _copy_domains(tmp_path / "query-dir")
    query_dir = (
        query_dir_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries"
    )
    query_dir.mkdir()
    (query_dir / "hidden.py").write_text(
        "from .. import core\n"
        "\n"
        "def mutate(session):\n"
        "    session.query(Job).delete()\n"
        "    return core.request_stop(session, 'job')\n",
        encoding="utf-8",
    )
    query_dir_actual = control_architecture_debt.build_architecture_debt(
        query_dir_root
    )
    assert query_dir_actual["query_mutations"]["direct_dml_count"] == 1
    assert query_dir_actual["query_mutations"][
        "semantic_allowlist_count"
    ] == 4
    with pytest.raises(ValueError, match="direct_dml_sites"):
        control_architecture_debt.validate_decreasing(
            query_dir_actual,
            baseline,
        )


def test_semantic_query_analysis_uses_the_shared_mutation_sinks(
    tmp_path: Path,
) -> None:
    baseline = _baseline()
    root = _copy_domains(tmp_path)
    jobs_core = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    jobs_core.write_text(
        jobs_core.read_text(encoding="utf-8")
        + "\n\ndef equivalent_mutator(session, job, payload):\n"
        + "    db = session\n"
        + "    db.query(Job).update({'status': 'stopped'})\n"
        + "    setattr(job, 'status', 'stopped')\n"
        + "    db.execute(update(Job).values(payload))\n"
        + "    db.execute(text('UPDATE jobs SET status = 1'))\n",
        encoding="utf-8",
    )
    jobs_queries = jobs_core.with_name("queries.py")
    jobs_queries.write_text(
        jobs_queries.read_text(encoding="utf-8")
        + "\nfrom .core import equivalent_mutator\n",
        encoding="utf-8",
    )

    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["query_mutations"]["semantic_allowlist_count"] == 4
    assert actual["policy_external_status_writes"]["count"] == 44
    with pytest.raises(
        ValueError,
        match="semantic_allowlist_sites",
    ):
        control_architecture_debt.validate_decreasing(actual, baseline)


def test_alias_dynamic_import_and_closure_escape_hatches_are_blocked(
    tmp_path: Path,
) -> None:
    baseline = _baseline()

    sql_alias_root = _copy_domains(tmp_path / "sql-alias")
    alias_queries = (
        sql_alias_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    alias_queries.write_text(
        alias_queries.read_text(encoding="utf-8")
        + "\nfrom sqlalchemy import update as sql_update\n"
        + "\ndef alias_dml(session):\n"
        + "    session.execute(sql_update(Job).values(label='changed'))\n",
        encoding="utf-8",
    )
    sql_alias_actual = control_architecture_debt.build_architecture_debt(
        sql_alias_root
    )
    assert sql_alias_actual["query_mutations"]["direct_dml_count"] == 1

    import_root = _copy_domains(tmp_path / "dynamic-import")
    import_core = (
        import_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    import_core.write_text(
        import_core.read_text(encoding="utf-8")
        + "\n\ndef dynamic_core_import():\n"
        + "    return importlib.import_module("
        + "'ocr_platform.control.domains.workers.core')\n",
        encoding="utf-8",
    )
    import_actual = control_architecture_debt.build_architecture_debt(
        import_root
    )
    assert import_actual["cross_domain_imports"]["symbol_count"] == 45

    closure_root = _copy_domains(tmp_path / "closure")
    closure_core = (
        closure_root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "core.py"
    )
    closure_core.write_text(
        closure_core.read_text(encoding="utf-8")
        + "\n\ndef closure_transaction(db: Session | None):\n"
        + "    alias = db\n"
        + "    def inner():\n"
        + "        alias.commit()\n"
        + "    return inner\n",
        encoding="utf-8",
    )
    closure_actual = control_architecture_debt.build_architecture_debt(
        closure_root
    )
    assert closure_actual["transactions"]["total"] == 50

    for actual in (
        sql_alias_actual,
        import_actual,
        closure_actual,
    ):
        with pytest.raises(ValueError, match="new or replaced sites"):
            control_architecture_debt.validate_decreasing(actual, baseline)


def test_false_positive_exclusions_for_constructor_and_unrelated_flush(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    jobs = root / "ocr_platform" / "control" / "domains" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "core.py").write_text(
        "def examples(file_handle, response):\n"
        "    Manifest(status='ready')\n"
        "    response.values(status='ready')\n"
        "    file_handle.flush()\n",
        encoding="utf-8",
    )

    debt = control_architecture_debt.build_architecture_debt(root)

    assert debt["transactions"]["total"] == 0
    assert debt["policy_external_status_writes"]["count"] == 0


@pytest.mark.parametrize(
    ("name", "source", "expected_policy_count"),
    [
        (
            "bulk-update",
            """
def hidden_bulk_update(session):
    session.bulk_update_mappings(Job, [{"status": "failed"}])
""",
            41,
        ),
        (
            "bulk-save",
            """
def hidden_bulk_save(session):
    session.bulk_save_objects([Job(status="failed")])
""",
            41,
        ),
        (
            "scalar-returning",
            """
def hidden_scalar(session):
    from sqlalchemy import update as local_update
    session.scalars(
        local_update(Job).values(label="changed").returning(Job)
    )
""",
            40,
        ),
        (
            "module-alias-returning",
            """
def hidden_module_alias(session):
    import sqlalchemy as sa
    session.scalar(sa.update(Job).values(label="changed").returning(Job))
""",
            40,
        ),
    ],
)
def test_query_bulk_scalar_and_function_local_alias_sinks_are_blocked(
    tmp_path: Path,
    name: str,
    source: str,
    expected_policy_count: int,
) -> None:
    root = _copy_domains(tmp_path / name)
    jobs_queries = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    jobs_queries.write_text(
        jobs_queries.read_text(encoding="utf-8") + source,
        encoding="utf-8",
    )

    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["query_mutations"]["direct_dml_count"] == 1
    assert (
        actual["policy_external_status_writes"]["count"]
        == expected_policy_count
    )
    with pytest.raises(ValueError, match="direct_dml_sites"):
        control_architecture_debt.validate_decreasing(
            actual,
            _baseline(),
        )


def test_any_query_orm_attribute_store_is_direct_dml(tmp_path: Path) -> None:
    root = _copy_domains(tmp_path)
    jobs_queries = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    jobs_queries.write_text(
        jobs_queries.read_text(encoding="utf-8")
        + """

def hidden_attribute_mutation(job, now):
    job.name = "changed"
    job.updated_at = now
""",
        encoding="utf-8",
    )

    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["query_mutations"]["direct_dml_count"] == 2
    assert actual["policy_external_status_writes"]["count"] == 40
    with pytest.raises(ValueError, match="direct_dml_sites"):
        control_architecture_debt.validate_decreasing(
            actual,
            _baseline(),
        )


@pytest.mark.parametrize(
    ("name", "query_source"),
    [
        (
            "lazy-direct",
            """
def lazy_direct(session):
    from .core import request_stop
    return request_stop(session, "job")
""",
        ),
        (
            "lazy-module",
            """
def lazy_module(session):
    from . import core
    return core.request_stop(session, "job")
""",
        ),
    ],
)
def test_semantic_query_resolves_function_local_imports(
    tmp_path: Path,
    name: str,
    query_source: str,
) -> None:
    root = _copy_domains(tmp_path / name)
    jobs_queries = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
        / "queries.py"
    )
    jobs_queries.write_text(
        jobs_queries.read_text(encoding="utf-8") + query_source,
        encoding="utf-8",
    )

    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["query_mutations"]["semantic_allowlist_count"] == 4
    with pytest.raises(ValueError, match="semantic_allowlist_sites"):
        control_architecture_debt.validate_decreasing(
            actual,
            _baseline(),
        )


def test_semantic_query_reachability_covers_commands_module(
    tmp_path: Path,
) -> None:
    root = _copy_domains(tmp_path)
    jobs = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
    )
    commands = jobs / "commands.py"
    commands.write_text(
        commands.read_text(encoding="utf-8")
        + """

def wrapped_stop(session):
    return request_stop(session, "job")
""",
        encoding="utf-8",
    )
    queries = jobs / "queries.py"
    queries.write_text(
        queries.read_text(encoding="utf-8")
        + """

def hidden_command_call(session):
    from . import commands
    return commands.wrapped_stop(session)

def hidden_command_reexport(session):
    from .commands import request_stop as command_stop
    return command_stop(session, "job")
""",
        encoding="utf-8",
    )

    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["query_mutations"]["semantic_allowlist_count"] == 5
    command_sites = [
        site
        for site in actual["query_mutations"]["semantic_allowlist_sites"]
        if site["core_module"].endswith(".jobs.commands")
    ]
    assert {site["symbol"] for site in command_sites} == {
        "request_stop",
        "wrapped_stop",
    }
    with pytest.raises(ValueError, match="semantic_allowlist_sites"):
        control_architecture_debt.validate_decreasing(
            actual,
            _baseline(),
        )


def test_shared_semantic_sink_tracks_local_dml_statement_taint(
    tmp_path: Path,
) -> None:
    root = _copy_domains(tmp_path)
    jobs = (
        root
        / "ocr_platform"
        / "control"
        / "domains"
        / "jobs"
    )
    core = jobs / "core.py"
    core.write_text(
        core.read_text(encoding="utf-8")
        + """

def local_statement_mutator(session):
    from sqlalchemy import update as local_update
    statement = local_update(Job).values(label="changed").returning(Job)
    return session.scalars(statement)
""",
        encoding="utf-8",
    )
    queries = jobs / "queries.py"
    queries.write_text(
        queries.read_text(encoding="utf-8")
        + "\nfrom .core import local_statement_mutator\n",
        encoding="utf-8",
    )

    actual = control_architecture_debt.build_architecture_debt(root)

    assert actual["query_mutations"]["semantic_allowlist_count"] == 4
    with pytest.raises(ValueError, match="semantic_allowlist_sites"):
        control_architecture_debt.validate_decreasing(
            actual,
            _baseline(),
        )
