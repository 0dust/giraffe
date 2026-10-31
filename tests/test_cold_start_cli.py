"""CLI confirmation and handoffs require no live server or socket binding."""

import io
import json

import pytest

from giraffe.cli import _confirm_restart_script, _print_result, configuration, main, parser
from giraffe.handoff import cli_command
from giraffe.models import RunConfig, RunReport, Target


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "restart script.sh"
    path.write_text("#!/bin/sh\nprintf 'restart issued\\n'\n")
    path.chmod(0o700)
    return path


@pytest.fixture
def config(script):
    return RunConfig(targets=[Target(name="local", url="http://localhost:11434/v1",
                                     model="tiny", restart_script=str(script))],
                     restart_target="local", checks=["correctness"])


def arguments(script, *extra):
    return ["cold-start", "--url", "http://localhost:11434/v1", "--model", "tiny",
            "--restart-script", str(script), *extra]


def test_single_target_selects_script_and_timeout(script):
    config = configuration(parser().parse_args(arguments(script, "--restart-script-timeout-seconds", "17")))
    assert config.restart_target == "local"
    assert config.targets[0].restart_script == str(script)
    assert config.restart_script_timeout_seconds == 17
    assert config.restart_script_sha256 is None


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf"])
def test_script_timeout_must_be_positive_finite(script, timeout):
    with pytest.raises(ValueError):
        configuration(parser().parse_args(arguments(script, "--restart-script-timeout-seconds", timeout)))


def test_multiple_targets_require_explicit_selection(tmp_path, script):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": 2, "targets": [
        {"name": name, "url": "http://localhost", "model": "tiny", "restart_script": str(script)}
        for name in ("one", "two")]}))
    with pytest.raises(ValueError, match="--restart NAME"):
        configuration(parser().parse_args(["cold-start", "--config", str(path)]))
    config = configuration(parser().parse_args(["cold-start", "--config", str(path), "--restart", "two"]))
    assert config.restart_target == "two"


def test_yes_prints_same_preview_to_stderr_and_excludes_hash_from_handoff(config, capsys):
    from giraffe.restart_script import preview_script

    preview = preview_script(config.targets[0].restart_script)
    _confirm_restart_script(config, yes=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert all(str(preview[key]) in captured.err for key in ("path", "machine", "sha256", "size", "contents"))
    assert config.restart_script_sha256 == preview["sha256"]
    assert "restart_script_sha256" not in config.model_dump()
    command = cli_command(config, mode="cold-start")
    assert "giraffe cold-start" in command
    assert "--yes" not in command
    assert preview["sha256"] not in command
    assert "restart_command" not in command


@pytest.mark.parametrize("response", ["y\n", "YES\n"])
def test_interactive_prompt_confirms_exact_hash(config, monkeypatch, response):
    terminal = io.StringIO(response)
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    _confirm_restart_script(config)
    assert config.restart_script_sha256


def test_decline_clears_inherited_confirmation(config, monkeypatch):
    terminal = io.StringIO("no\n")
    monkeypatch.setattr(terminal, "isatty", lambda: True)
    monkeypatch.setattr("sys.stdin", terminal)
    config.restart_script_sha256 = "f" * 64
    with pytest.raises(ValueError, match="not confirmed"):
        _confirm_restart_script(config)
    assert config.restart_script_sha256 is None


def test_stdin_handoff_uses_controlling_terminal(config, monkeypatch):
    terminal = io.StringIO("yes\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("configuration already consumed"))
    opened = []

    def open_terminal(path, *args, **kwargs):
        opened.append(path)
        return terminal

    monkeypatch.setattr("giraffe.cli.open", open_terminal, raising=False)
    _confirm_restart_script(config)
    assert opened == ["/dev/tty"]
    assert terminal.closed
    assert config.restart_script_sha256


def test_nonterminal_refuses_before_any_preparation(script, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def no_terminal(*args, **kwargs):
        raise OSError("No controlling terminal")

    monkeypatch.setattr("giraffe.cli.open", no_terminal, raising=False)
    monkeypatch.setattr("giraffe.planning.request_plan", lambda *args, **kwargs: pytest.fail("planning started"))
    monkeypatch.setattr("giraffe.cli._run", lambda *args, **kwargs: pytest.fail("script ran"))
    assert main(arguments(script, "--output-format", "json")) == 4
    captured = capsys.readouterr()
    assert "interactive terminal or --yes" in captured.out + captured.err
    assert json.loads(captured.out)["outcome"] == "error"


def test_yes_json_run_keeps_preview_off_stdout_and_passes_fresh_confirmation(
    script, tmp_path, monkeypatch, capsys,
):
    from giraffe.restart_script import preview_script

    preview = preview_script(str(script))
    restart = {"script_ms": 12, "model_load": "Model was already loaded"}
    output_dir = tmp_path / "report"

    async def run(config, **kwargs):
        assert config.restart_script_sha256 == preview["sha256"]
        assert config._restart_log_dir == output_dir
        return RunReport(run_id="cold", started_at="now", finished_at="now", overall="pass",
                         manifest={"config": config.model_dump(mode="json")},
                         observations={"targets": {"local": {"restart": restart}}})

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(arguments(script, "--yes", "--output", str(output_dir), "--output-format", "json")) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert len(captured.out.splitlines()) == 1
    assert result["cold_start"] == {"local": restart}
    assert preview["sha256"] in captured.err
    assert "restart_script_sha256" not in (output_dir / "report.json").read_text()


def test_no_script_confirmation_preserves_normal_run(monkeypatch, capsys):
    config = RunConfig(targets=[Target(name="local", url="http://localhost", model="tiny")])
    monkeypatch.setattr("giraffe.restart_script.preview_script", lambda *args: pytest.fail("script previewed"))
    _confirm_restart_script(config)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("output_format", ["text", "json"])
@pytest.mark.parametrize("has_model", [False, True])
def test_cold_start_without_script_exits_two_before_running(tmp_path, monkeypatch, capsys, legacy, output_format, has_model):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"config_version": 2, "targets": [{"name": "local",
        "url": "http://localhost", **({"model": "tiny"} if has_model else {}),
        **({"restart_command": ["false"]} if legacy else {})}]}))
    monkeypatch.setattr("giraffe.cli._run", lambda *args, **kwargs: pytest.fail("plain run started"))
    monkeypatch.setattr("giraffe.cli._discover_model", lambda *args, **kwargs: pytest.fail("model discovery started"))
    assert main(["cold-start", "--config", str(path), "--output-format", output_format,
                 "--output", str(tmp_path / "run")]) == 2
    captured = capsys.readouterr()
    message = "cold-start needs a restart script. Use --restart-script or set restart_script in the config."
    assert message in captured.out + captured.err
    if output_format == "json":
        assert json.loads(captured.out)["exit_code"] == 2
    if legacy:
        assert "restart_command" in captured.err


def test_url_only_cold_start_without_script_refuses_before_model_discovery(monkeypatch, capsys):
    monkeypatch.setattr("giraffe.cli._discover_model", lambda *args, **kwargs: pytest.fail("model discovery started"))
    assert main(["cold-start", "--url", "http://localhost"]) == 2
    assert "cold-start needs a restart script. Use --restart-script or set restart_script in the config." in capsys.readouterr().err


def test_text_missing_model_load_evidence_has_a_label(tmp_path, capsys):
    report = RunReport(run_id="cold", started_at="now", finished_at="now", overall="pass",
        manifest={}, observations={"targets": {"local": {"restart": {
            "model_load": "Not reported by this server"}}}})
    _print_result(report, tmp_path / "report.json", None, tmp_path, {})
    assert "Model load: Not reported by this server" in capsys.readouterr().out


@pytest.mark.parametrize("contents,message", [
    ("#!/bin/sh\r\ntrue\r\n", "Restart script contains control or invisible characters. Remove them to review it safely."),
    ("echo not-a-script\n", "Restart script must start with a #! line.")])
def test_yes_does_not_bypass_script_validation(script, contents, message, monkeypatch, capsys):
    script.write_bytes(contents.encode())
    monkeypatch.setattr("giraffe.cli._run", lambda *args, **kwargs: pytest.fail("unsafe script ran"))
    assert main(arguments(script, "--yes")) == 4
    assert message in capsys.readouterr().err


def test_ui_flag_renamed():
    args = parser().parse_args(["ui", "--host", "0.0.0.0", "--allow-restart-script"])
    assert args.allow_restart_script
    with pytest.raises(SystemExit):
        parser().parse_args(["ui", "--allow-restart-command"])


@pytest.mark.parametrize("kind", ["answer", "reasoning"])
def test_text_cold_start_uses_reported_evidence_only(tmp_path, capsys, kind):
    report = RunReport(run_id="cold", started_at="now", finished_at="now", overall="pass",
        manifest={}, observations={"targets": {"local": {"restart": {
            "script_ms": 10, "service_answered_ms": 20, "readiness_ms": 3000,
            "first_request": {"first_output_ms": 2900, "elapsed_ms": 2980, "first_output_kind": kind},
            "warm_requests": [{"first_output_ms": 7, "elapsed_ms": 10},
                              {"first_output_ms": 8, "elapsed_ms": 11}],
            "model_load": "Model was already loaded",
            "model_loaded_before_first_request": "Yes",
            "measurement_api": "Measured through Ollama's native API"}}}})
    _print_result(report, tmp_path / "report.json", None, tmp_path, {})
    output = capsys.readouterr().out
    assert "Cold start · local" in output
    assert "First answer after: 3000 ms" in output
    assert "Warm request 2: first answer 8 ms; latency 11 ms" in output
    assert "Model was already loaded" in output
    assert "Model load:" not in output
    note = "First output was reasoning; no answer within the output cap."
    assert (note in output) == (kind == "reasoning")
