from tools import run_mock_e2e


def test_supported_interpreter_with_platform_extra_reports_no_problems():
    assert (
        run_mock_e2e.environment_problems(
            version_info=(3, 12, 1),
            missing_platform=(),
        )
        == []
    )


def test_old_python_fails_fast_with_the_exact_interpreter_and_install_commands():
    problems = run_mock_e2e.environment_problems(
        version_info=(3, 9, 18),
        missing_platform=(),
    )

    assert len(problems) == 1
    assert "Python 3.10+ is required" in problems[0]
    assert "this interpreter is 3.9.18" in problems[0]
    assert "python3.10 -m venv .venv" in problems[0]
    assert "pip install -e '.[dev]'" in problems[0]


def test_missing_platform_dependencies_report_the_exact_install_command():
    problems = run_mock_e2e.environment_problems(
        version_info=(3, 12, 1),
        missing_platform=("fastapi", "uvicorn"),
    )

    assert len(problems) == 1
    assert "pip install 'ocrparser-platform[platform]'" in problems[0]
    assert "fastapi, uvicorn" in problems[0]


def test_both_problems_are_reported_together_before_any_service_starts():
    problems = run_mock_e2e.environment_problems(
        version_info=(3, 9, 18),
        missing_platform=("fastapi",),
    )

    assert len(problems) == 2
    assert "Python 3.10+ is required" in problems[0]
    assert "ocrparser-platform[platform]" in problems[1]


def test_main_exits_before_starting_services_when_the_environment_is_unsupported(
    monkeypatch,
    capsys,
):
    started: list[object] = []
    monkeypatch.setattr(
        run_mock_e2e,
        "environment_problems",
        lambda: ["Python 3.10+ is required; this interpreter is 3.9.18."],
    )
    monkeypatch.setattr(
        run_mock_e2e,
        "run",
        lambda *args, **kwargs: started.append(args) or 0,
    )
    monkeypatch.setattr("sys.argv", ["run_mock_e2e.py"])

    exit_code = run_mock_e2e.main()

    assert exit_code == 2
    assert started == []
    assert "Python 3.10+ is required" in capsys.readouterr().err


def test_main_runs_the_walkthrough_when_the_environment_is_supported(
    monkeypatch,
):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(run_mock_e2e, "environment_problems", lambda: [])
    monkeypatch.setattr(
        run_mock_e2e,
        "run",
        lambda root, *, parser_python: calls.append(
            {"root": root, "parser_python": parser_python}
        )
        or 0,
    )
    monkeypatch.setattr("sys.argv", ["run_mock_e2e.py"])

    assert run_mock_e2e.main() == 0
    assert calls[0]["root"] == run_mock_e2e.REPO_ROOT
