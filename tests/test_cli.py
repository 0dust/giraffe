import json
import io
from pathlib import Path

import pytest

from giraffe.cli import configuration, main, parser
from giraffe.models import RunReport


def test_cli_one_command_has_builtin_suite():
    config = configuration(parser().parse_args([
        "run", "--url", "http://localhost:11434/v1", "--model", "tiny",
        "--max-requests", "50", "--json", "--latency-ms", "5000",
    ]))
    assert len(config.checks) == 11
    assert "json" in config.checks and "gpu" not in config.checks
    assert config.max_requests == 50
    assert config.structured_json
    assert config.limits.latency_ms == 5000


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_default_cli_budget_covers_default_preview(output_format, tmp_path, monkeypatch, capsys):
    config = configuration(parser().parse_args([
        "run", "--url", "http://127.0.0.1:11434/v1", "--model", "llama3.2:3b",
    ]))
    assert config.max_requests == 300
    assert (config.max_duration_seconds, config.request_timeout_seconds, config.max_output_tokens) == (300, 30, 128)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"targets": [target.model_dump(mode="json") for target in config.targets]}))
    monkeypatch.setattr("giraffe.preflight.check_target", lambda *a: pytest.fail("Preview probed"))
    monkeypatch.setattr("giraffe.cli._run", lambda *a: pytest.fail("Preview sent traffic"))
    assert main(["preview", "--config", str(path), "--output-format", output_format]) == 0
    captured = capsys.readouterr()
    if output_format == "json":
        result = json.loads(captured.out)
        assert result["config"]["checks"] == config.checks
        assert result["config"]["max_requests"] == result["request_plan"]["request_limit"] == 300
        warnings = result["request_plan"]["warnings"]
    else:
        assert "max_requests: 300" in captured.out
        warnings = captured.out.splitlines()
    assert not any("share of the run limit" in warning for warning in warnings)
    assert not captured.err


def test_cli_optional_flags_extend_saved_selection_but_explicit_checks_win(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "targets": [{"name": "test", "url": "http://localhost", "model": "tiny"}],
        "checks": ["correctness"],
    }))
    args = ["run", "--config", str(path), "--json", "--metrics", "--tool-calling"]
    selected = configuration(parser().parse_args(args))
    assert selected.checks == ["correctness", "json", "gpu", "tools"]
    explicit = configuration(parser().parse_args(args + ["--checks", "correctness"]))
    assert explicit.checks == ["correctness"]
    assert not explicit.structured_json and not explicit.metrics and not explicit.tool_calling


def test_config_cannot_restart_without_explicit_command(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [{"name": "test", "url": "http://localhost",
                                             "model": "tiny"}], "restart_target": "test"}))
    assert main(["run", "--config", str(config)]) == 4
    assert "cold-start --restart" in capsys.readouterr().err
    parsed = configuration(parser().parse_args([
        "cold-start", "--config", str(config), "--restart", "test",
    ]))
    assert parsed.restart_target == "test"


def test_baseline_save_never_replaces_implicitly(tmp_path):
    source, destination = tmp_path / "run.json", tmp_path / "baseline.json"
    report = RunReport(run_id="first", started_at="now", finished_at="now", overall="pass",
                       manifest={})
    source.write_text(report.model_dump_json())
    assert main(["baseline", "save", str(source), str(destination)]) == 0
    report.run_id = "second"
    source.write_text(report.model_dump_json())
    assert main(["baseline", "save", str(source), str(destination)]) == 4
    assert json.loads(destination.read_text())["run_id"] == "first"
    assert main(["baseline", "save", str(source), str(destination), "--replace"]) == 0
    assert json.loads(destination.read_text())["run_id"] == "second"


def test_existing_report_directory_rejected_before_network(tmp_path, capsys):
    (tmp_path / "report.json").write_text("existing")
    assert main(["run", "--url", "http://localhost", "--model", "tiny",
                 "--output", str(tmp_path)]) == 4
    assert "not empty" in capsys.readouterr().err


def test_cli_errors_do_not_print_raw_config(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"targets": [{"name": "test", "url": "https://user:secret@x",
                                             "model": "tiny"}]}))
    assert main(["run", "--config", str(config)]) == 4
    assert "secret" not in capsys.readouterr().err


def test_run_duration_must_be_finite(capsys):
    assert main(["run", "--url", "http://localhost", "--model", "tiny",
                 "--max-duration", "inf"]) == 4
    assert "finite" in capsys.readouterr().err


def test_ui_rejects_invalid_port_before_starting_server(capsys):
    assert main(["ui", "--port", "0"]) == 4
    assert "Port must be" in capsys.readouterr().err


def test_ui_launch_stays_local_and_clears_implicit_restart(tmp_path, monkeypatch, capsys):
    import uvicorn
    import giraffe.web

    config = tmp_path / "ui.yaml"
    config.write_text(json.dumps({"targets": [{"name": "local", "url": "http://localhost",
                                             "model": "tiny", "restart_command": ["false"]}],
                                  "restart_target": "local"}))
    captured = {}

    def create_app(**kwargs):
        captured.update(kwargs)
        return "app"

    monkeypatch.setattr(giraffe.web, "create_app", create_app)
    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            assert "Giraffe UI:" not in capsys.readouterr().out
            captured["bound"] = address

        def listen(self, backlog):
            assert captured["bound"] == ("127.0.0.1", 8765)

    monkeypatch.setattr("giraffe.cli.socket.socket", lambda *a: Listener())
    monkeypatch.setattr(uvicorn.Server, "run", lambda server, **kwargs: captured.update(
        app=server.config.app, host=server.config.host, port=server.config.port, **kwargs))
    assert main(["ui", "--config", str(config), "--runs-dir", str(tmp_path)]) == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == 8765
    assert "open in your browser" in capsys.readouterr().out
    assert captured["defaults"]["restart_target"] is None
    assert captured["defaults"]["targets"][0]["restart_command"] == ["false"]


def test_describe_has_full_authoritative_schema_without_traffic(monkeypatch, capsys):
    from giraffe.capabilities import capabilities

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery must not run inference")

    monkeypatch.setattr("giraffe.cli._run", forbidden)
    assert main(["describe"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == capabilities()
    assert result["config_schema"]["required"] == ["targets"]
    assert result["defaults"]["traffic"]["rates"] == [1, 2, 4]
    assert len(result["checks"]) == 17
    assert "runtime availability" in result["validation_note"]


def test_validate_stdin_normalizes_config_without_side_effects(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("""
targets:
  - name: local
    url: http://localhost
    model: tiny
checks: [correctness]
structured_json: true
test_options:
  correctness:
    samples: 8
"""))
    monkeypatch.setattr("giraffe.cli._run", lambda *a, **k: pytest.fail("Validation sent traffic"))
    assert main(["validate", "--config", "-", "--output-format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "validate" and result["outcome"] == "success"
    assert result["config"]["checks"] == ["correctness"]
    assert result["config"]["test_options"] == {"correctness": {"samples": 8}}
    assert not result["config"]["structured_json"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("document", [
    '{"targets": [{"name": "local", "url": "https://user:PRIVATE_SECRET@host", "model": "m"}]}',
    'targets: [PRIVATE_SECRET\n',
])
def test_validate_json_errors_never_echo_input(document, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(document))
    assert main(["validate", "--config", "-", "--output-format=json"]) == 4
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["outcome"] == "error" and result["exit_code"] == 4
    assert result["errors"] and "PRIVATE_SECRET" not in captured.out + captured.err


def test_argument_errors_use_json_when_requested(capsys):
    assert main(["run", "--samples", "wrong", "--output-format", "json"]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "run" and result["outcome"] == "error"
    assert result["errors"][0]["type"] == "UsageError"


@pytest.mark.parametrize("outcome,exit_code", [("pass", 0), ("fail", 1), ("inconclusive", 2), ("blocked", 3), ("skipped", 2)])
@pytest.mark.parametrize("command", ["run", "cold-start"])
def test_run_json_outcome_has_saved_report_paths_and_preserves_exit_code(
    outcome, exit_code, command, monkeypatch, capsys, tmp_path,
):
    configuration = {
        "targets": [{"name": "local", "url": "http://localhost", "model": "tiny",
                     "restart_command": ["unused-command"]}],
        "checks": ["correctness"],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(configuration)))
    captured_config = []

    async def run(config, **kwargs):
        captured_config.append(config)
        return RunReport(run_id="machine-run", started_at="now", finished_at="now", overall=outcome,
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    arguments = [command, "--config", "-", "--output", str(tmp_path), "--output-format", "json", "--skip-preflight"]
    if command == "cold-start":
        arguments += ["--restart", "local"]
    assert main(arguments) == exit_code
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert len(captured.out.splitlines()) == 1
    assert result["kind"] == command and result["outcome"] == outcome and result["exit_code"] == exit_code
    assert result["run_id"] == "machine-run"
    assert json.loads(Path(result["report_paths"]["json"]).read_text())["run_id"] == "machine-run"
    assert Path(result["report_paths"]["html"]).is_file()
    assert "Giraffe:" in captured.err
    assert captured_config[0].restart_target == ("local" if command == "cold-start" else None)


def test_baseline_save_json_preserves_explicit_replace_boundary(tmp_path, capsys):
    source, destination = tmp_path / "run.json", tmp_path / "saved.json"
    source.write_text(RunReport(run_id="chosen", started_at="now", finished_at="now",
                               overall="pass", manifest={}).model_dump_json())
    arguments = ["baseline", "save", str(source), str(destination), "--output-format", "json"]
    assert main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "baseline.save" and result["destination"] == str(destination.resolve())
    assert main(arguments) == 4
    assert json.loads(capsys.readouterr().out)["outcome"] == "error"


def operator_pack():
    return {"version": 1, "cases": [{"fixture_id": "hello", "messages": [
        {"role": "user", "content": "Reply hello"}], "scorer": "exact", "expected": "hello"}]}


@pytest.mark.parametrize("command", ["run", "cold-start"])
def test_request_pack_file_embeds_portable_configuration(tmp_path, command):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(operator_pack()))
    args = [command, "--url", "http://localhost", "--model", "tiny", "--request-pack", str(path)]
    if command == "cold-start":
        args += ["--restart", "local"]
    result = configuration(parser().parse_args(args))
    path.unlink()
    exported = tmp_path / "exported.json"
    exported.write_text(result.model_dump_json())
    rerun_args = [command, "--config", str(exported)]
    if command == "cold-start":
        rerun_args += ["--restart", "local"]
    assert configuration(parser().parse_args(rerun_args)) == result
    assert result.request_source == "request_pack"
    assert result.request_pack.cases[0].expected == "hello"


@pytest.mark.parametrize("conflict", ["inline", "fixtures", "url"])
def test_request_pack_flag_conflicts_fail_before_reading_pack(conflict, monkeypatch, capsys, tmp_path):
    values = {"targets": [{"name": "local", "url": "http://localhost", "model": "tiny"}]}
    if conflict == "inline":
        values["request_pack"] = operator_pack()
    if conflict == "fixtures":
        values["custom_fixtures"] = "missing-legacy.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
    monkeypatch.setattr("giraffe.request_pack.load_request_pack", lambda *a: pytest.fail("read pack"))
    output = tmp_path / "run"
    path = "https://example.com/pack.json" if conflict == "url" else "missing-pack.json"
    assert main(["run", "--config", "-", "--request-pack", path, "--output", str(output),
                 "--output-format", "json"]) == 4
    assert not output.exists()
    message = json.loads(capsys.readouterr().out)["errors"][0]["message"]
    assert ("inline" if conflict == "inline" else "custom_fixtures" if conflict == "fixtures" else "local") in message


def test_preview_stdin_allows_blank_model_without_side_effects(monkeypatch, capsys, tmp_path):
    from giraffe.models import ConfigDraft
    from giraffe.request_pack import pack_preview
    from giraffe.planning import request_plan

    values = {"targets": [{"name": "local", "url": "http://localhost", "model": ""}],
              "checks": ["capacity"], "request_source": "request_pack", "request_pack": operator_pack()}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
    monkeypatch.setattr("giraffe.cli._run", lambda *a: pytest.fail("Preview sent traffic"))
    assert main(["preview", "--config", "-"]) == 0
    draft = ConfigDraft.model_validate(values)
    assert json.loads(capsys.readouterr().out) == {
        "config": draft.model_dump(mode="json"), "request_pack_preview": pack_preview(draft),
        "request_plan": request_plan(draft)}
    assert not list(tmp_path.iterdir())


def test_describe_exposes_pack_contract(capsys):
    assert main(["describe"]) == 0
    pack = json.loads(capsys.readouterr().out)["request_pack"]
    assert pack["version"] == 1
    assert pack["max_bytes"] == 1048576 and pack["max_cases"] == 1000
    assert set(pack["scorers"]) == {"exact", "contains", "json", "none", "judge"}
    assert pack["schema"]["properties"]["version"] == {"const": 1, "title": "Version", "type": "integer"}


@pytest.mark.parametrize("source", ["inline", "file"])
def test_invalid_pack_errors_have_case_locations_and_create_no_run(source, tmp_path, monkeypatch, capsys):
    pack = operator_pack()
    pack["cases"][0]["weight"] = "PRIVATE_SECRET"
    values = {"targets": [{"name": "local", "url": "http://localhost", "model": "tiny"}],
              "request_source": "request_pack"}
    args = ["run", "--config", "-", "--output", str(tmp_path / "output"), "--output-format", "json"]
    if source == "inline":
        values["request_pack"] = pack
    else:
        path = tmp_path / "pack.json"
        path.write_text(json.dumps(pack))
        args += ["--request-pack", str(path)]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
    monkeypatch.setattr("giraffe.cli._run", lambda *a: pytest.fail("Invalid pack sent traffic"))
    assert main(args) == 4
    output = capsys.readouterr()
    assert "PRIVATE_SECRET" not in output.out + output.err
    error = json.loads(output.out)["errors"][0]
    assert error["loc"][-3:] == ["cases", 0, "weight"]
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("flags", [[], ["--output-format", "json"], ["--output-format=json"],
                                   ["--output-format", "text"], ["--output-format=text"]])
def test_preview_includes_plan_warnings_without_traffic(flags, tmp_path, monkeypatch, capsys):
    import httpx

    def no_traffic(*args, **kwargs):
        pytest.fail("Preview sent traffic")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_traffic)
    monkeypatch.chdir(tmp_path)
    values = {"targets": [{"name": "local", "url": "http://localhost", "model": ""}],
              "checks": ["correctness"], "max_requests": 1}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(values)))
    assert main(["preview", "--config", "-", *flags]) == 0
    captured = capsys.readouterr()
    warning = "local needs at least 6 requests, but its share of the run limit is 1. Some tests may be incomplete."
    if any("text" in value for value in flags):
        assert "No traffic was sent." in captured.out
        assert f"Warning: {warning}\n" in captured.out
        assert "Known requests per target: 6" in captured.out
    else:
        result = json.loads(captured.out)
        assert result["request_plan"]["warnings"] == [warning]
        assert result["request_plan"]["known_per_target"] == 6
    assert not captured.err and not list(tmp_path.iterdir())


@pytest.mark.parametrize("flags", [["--output-format", "text"], ["--output-format=text"]])
def test_preview_text_errors_remain_text(flags, tmp_path, capsys):
    assert main(["preview", "--config", str(tmp_path / "missing.yaml"), *flags]) == 4
    captured = capsys.readouterr()
    assert not captured.out and "config file not found:" in captured.err


def test_cli_endpoint_unresponsiveness_defaults_and_overrides():
    args = ["run", "--url", "http://localhost", "--model", "test"]
    defaults = configuration(parser().parse_args(args))
    assert defaults.unresponsive_after_failures == 5 and defaults.unresponsive_for_seconds == 45
    changed = configuration(parser().parse_args(args + [
        "--unresponsive-after-failures", "3", "--unresponsive-for", "12.5"]))
    assert changed.unresponsive_after_failures == 3 and changed.unresponsive_for_seconds == 12.5


def test_cli_zero_disables_endpoint_watchdog_and_help_explains_it():
    parsed = parser()
    config = configuration(parsed.parse_args([
        "run", "--url", "http://localhost", "--model", "test", "--unresponsive-after-failures", "0"]))
    assert config.unresponsive_after_failures == 0
    assert "0 disables" in parsed._subparsers._group_actions[0].choices["run"].format_help()
