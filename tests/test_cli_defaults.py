"""Default CLI selection and readable output without binding a server socket."""

import asyncio
import json
import os
import signal
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
import yaml

from giraffe.cli import _print_result, _run, configuration, main, parser
from giraffe.models import CheckResult, RunConfig, RunReport
from tests.test_connection import endpoint

pytestmark = pytest.mark.usefixtures("local_tokenizer")


def report(**updates):
    return RunReport(run_id="cli", started_at="now", finished_at="now", overall="pass",
                     manifest={}, **updates)


def test_url_only_discovers_single_model_with_connection_settings(monkeypatch, capsys):
    monkeypatch.setenv("DISCOVERY_KEY", "private-key")
    requests, clients = endpoint(monkeypatch, lambda request: httpx.Response(
        200, json={"data": [{"id": "tiny"}]}))
    config = configuration(parser().parse_args([
        "run", "--url", "https://server/v1", "--api-key-env", "DISCOVERY_KEY",
        "--proxy", "http://proxy", "--json",
    ]))
    assert config.targets[0].model == "tiny"
    assert config.checks == ["access", "serving", "correctness", "first_output"]
    assert not config.structured_json
    assert requests[0].method == "GET" and requests[0].url.path == "/v1/models"
    assert requests[0].headers["Authorization"] == "Bearer private-key"
    assert clients[0]["proxy"] == "http://proxy"
    captured = capsys.readouterr()
    assert captured.err == "Using model tiny (only model served)\n" and not captured.out


@pytest.mark.parametrize("status,models,message", [
    (200, ["a", "b", "c"], "Choose --model: a, b, c"),
    (200, [], "Provide --model"),
    (404, [], "Provide --model"),
    (500, [], "Provide --model"),
])
@pytest.mark.parametrize("output_flags", [[], ["--json"]])
def test_url_only_requires_model_when_ambiguous_or_unavailable(
    status, models, message, output_flags, tmp_path, monkeypatch, capsys,
):
    endpoint(monkeypatch, lambda request: httpx.Response(
        status, json={"data": [{"id": name} for name in models]}))
    monkeypatch.setattr("giraffe.cli._run", lambda *a: pytest.fail("Selection error ran suite"))
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://server/v1", "--output", str(directory), *output_flags]) == 2
    captured = capsys.readouterr()
    assert message in captured.err and not directory.exists()
    if output_flags:
        result = json.loads(captured.out)
        assert result["exit_code"] == 2 and message in result["message"]
    else:
        assert not captured.out


def test_config_model_is_never_discovered(tmp_path, monkeypatch):
    async def discover(*args, **kwargs):
        pytest.fail("Config model was discovered")

    monkeypatch.setattr("giraffe.preflight.discover_models", discover)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"targets": [{"name": "test", "url": "http://server", "model": "chosen"}]}))
    assert configuration(parser().parse_args(["run", "--config", str(path)])).targets[0].model == "chosen"


LEGACY_CHECKS = ["access", "serving", "first_output", "generation", "load",
                 "context", "correctness", "cancellation", "recovery"]


@pytest.mark.parametrize("format", ["json", "yaml"])
@pytest.mark.parametrize("concurrency", [None, 4])
def test_saved_legacy_config_preserves_omitted_check_selection(tmp_path, format, concurrency):
    values = {"targets": [{"name": "test", "url": "http://server", "model": "chosen"}]}
    if concurrency is not None:
        values["concurrency"] = concurrency
    path = tmp_path / f"config.{format}"
    path.write_text(json.dumps(values) if format == "json" else yaml.safe_dump(values))

    config = configuration(parser().parse_args(["run", "--config", str(path)]))

    assert config.checks == LEGACY_CHECKS
    assert config.config_version == 2
    path.write_text(config.model_dump_json())
    assert configuration(parser().parse_args(["run", "--config", str(path)])).checks == LEGACY_CHECKS


@pytest.mark.parametrize("field,value,check", [
    ("structured_json", True, "json"), ("metrics", True, "gpu"),
    ("prefix", {}, "prefix"), ("sessions", {}, "sessions"),
    ("consistency", {}, "consistency"), ("tool_calling", True, "tools"),
    ("tool_checks", {}, "tools"), ("arrivals", {}, "load"),
])
def test_saved_legacy_config_preserves_implicit_optional_checks(tmp_path, field, value, check):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"targets": [{"name": "test", "url": "http://server", "model": "chosen"}],
                                    field: value}))

    config = configuration(parser().parse_args(["run", "--config", str(path)]))

    assert config.checks == list(dict.fromkeys([*LEGACY_CHECKS, check]))
    if field == "arrivals":
        assert config.load_test.legacy_source == "arrivals"
        assert config.load_test.schedule.duration_seconds == 8


@pytest.mark.parametrize("disabled", [False, "false", 0])
def test_saved_legacy_config_does_not_select_disabled_optional_checks(tmp_path, disabled):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"targets": [{"name": "test", "url": "http://server", "model": "chosen"}],
        "structured_json": disabled, "metrics": disabled, "tool_calling": disabled,
        "prefix": None, "sessions": None, "consistency": None, "tool_checks": None, "arrivals": None}))
    assert configuration(parser().parse_args(["run", "--config", str(path)])).checks == LEGACY_CHECKS


@pytest.mark.parametrize("field", ["buckets", "concurrent_workloads"])
def test_saved_legacy_implicit_load_block_keeps_its_workloads(tmp_path, field):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"targets": [{"name": "test", "url": "http://server", "model": "chosen"}],
                                    field: {}, "sessions": {}, "tool_calling": True}))
    config = configuration(parser().parse_args(["run", "--config", str(path)]))
    assert config.checks == [*LEGACY_CHECKS, "sessions", "tools"]
    assert config.load_test.legacy_source == "concurrency"
    assert config.load_test.workloads.stress.enabled
    assert len(config.load_test.workloads.combinations.selected) == 9


@pytest.mark.parametrize("format", ["json", "yaml"])
@pytest.mark.parametrize("null_fields", [
    {"buckets": None}, {"concurrent_workloads": None},
    {"buckets": None, "concurrent_workloads": None},
])
def test_saved_legacy_null_load_blocks_are_absent(tmp_path, format, null_fields):
    values = {"targets": [{"name": "test", "url": "http://server", "model": "chosen"}]}
    serialize = json.dumps if format == "json" else yaml.safe_dump
    baseline = tmp_path / f"baseline.{format}"
    baseline.write_text(serialize(values))
    saved = tmp_path / f"with-null.{format}"
    saved.write_text(serialize({**values, **null_fields}))

    expected = configuration(parser().parse_args(["run", "--config", str(baseline)]))
    actual = configuration(parser().parse_args(["run", "--config", str(saved)]))

    assert actual.checks == LEGACY_CHECKS
    assert actual.model_dump() == expected.model_dump()
    assert actual._migration_notes == expected._migration_notes


@pytest.mark.parametrize("version,selection", [(None, ["serving"]), (2, None)])
def test_saved_check_selection_remains_explicit_or_uses_v2_defaults(tmp_path, version, selection):
    values = {"targets": [{"name": "test", "url": "http://server", "model": "chosen"}]}
    if version is not None:
        values["config_version"] = version
    if selection is not None:
        values.update(checks=selection, structured_json=True, metrics=True, sessions={})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))

    config = configuration(parser().parse_args(["run", "--config", str(path)]))

    assert config.checks == (selection or ["access", "serving", "correctness", "first_output"])


@pytest.mark.parametrize("flags,format", [
    (["--json"], "json"), (["--output-format", "json"], "json"),
    (["--json", "--output-format", "text"], "text"),
    (["--output-format", "text", "--json"], "json"),
])
def test_json_alias_changes_output_only(flags, format):
    args = parser().parse_args(["run", "--url", "http://server", "--model", "tiny", *flags])
    assert args.output_format == format
    assert not configuration(args).structured_json


def test_json_alias_emits_machine_report(tmp_path, monkeypatch, capsys):
    async def run(config, **kwargs):
        assert kwargs == {"json_progress": True}
        assert "json" not in config.checks
        return report()

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(["run", "--url", "http://server", "--model", "tiny", "--json", "--skip-preflight",
                 "--output", str(tmp_path / "run")]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "pass"


@pytest.mark.parametrize("outcome", ["pass", "fail", "inconclusive"])
@pytest.mark.parametrize("verbose", [False, True])
def test_final_block_starts_with_verdict_collapses_unselected_and_gates_ram(outcome, verbose, capsys):
    checks = [CheckResult(id="access", target="local", title="Connection", status="pass", summary="Connected.")]
    if outcome != "pass":
        checks.append(CheckResult(id="first_output", target="local", title="First output", status=outcome,
                                  summary="First output\nexceeded its limit."))
    for target in ("local", "second"):
        for check in ("json", "cancellation"):
            checks.append(CheckResult(id=check, target=target, title=check, status="skipped", summary="Not selected." if target == "local" else "Not selected for this run."))
    value = report(checks=checks)
    value.overall = outcome
    _print_result(value, Path("runs/run/report.json"), Path("runs/run/report.html"), Path("runs/run"), {}, verbose=verbose)
    output = capsys.readouterr().out
    assert output.splitlines()[0] == f"{outcome.upper()}  1 checks passed · 0 requests · report: runs/run/report.html"
    assert output.count("Not selected: json, cancellation") == 1
    assert "skipped — Not selected" not in output
    assert ("Peak memory (RAM)" in output) == verbose
    if outcome != "pass":
        assert output.splitlines()[1] == f"  local / first_output: {outcome} — First output exceeded its limit."


@pytest.mark.parametrize("verbose", [False, True])
async def test_progress_uses_human_names_and_hides_unknown_stages(verbose, monkeypatch, capsys):
    async def run(config, *, progress, stop_event):
        for stage in ("runtime_metrics_discovery", "initial", "warm", "correctness_c1"):
            progress({"target": "local", "scenario": stage})
        return report()

    monkeypatch.setattr("giraffe.runner.run_suite", run)
    await _run(RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}]), verbose=verbose)
    output = capsys.readouterr().err
    if verbose:
        assert "runtime_metrics_discovery" in output and "correctness_c1" in output
    else:
        assert "runtime_metrics_discovery" not in output and "correctness_c1" not in output
        for title in ("Connection check", "Warm-up", "Answer correctness"):
            assert title in output


@pytest.mark.parametrize("arguments", [["describe"], ["run", "--help"], ["--version"]])
def test_broken_stdout_returns_cleanly_for_commands_and_help(arguments, monkeypatch):
    class ClosedPipe:
        def write(self, value):
            raise BrokenPipeError("consumer closed")

        def flush(self):
            raise BrokenPipeError("consumer closed")

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    assert main(arguments) == 0


@pytest.mark.parametrize("arguments", [["run", "--help"], ["describe"]])
def test_piped_command_has_no_broken_pipe_traceback(arguments):
    command = [sys.executable, "-c", "from giraffe.cli import main; raise SystemExit(main())", *arguments]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    assert process.stdout.readline()
    process.stdout.close()
    error = process.stderr.read().decode()
    assert process.wait(timeout=20) == 0
    assert "BrokenPipe" not in error and "Traceback" not in error


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
async def test_sigint_sets_the_runner_user_stop_event_and_removes_handlers(monkeypatch, stop_signal):
    loop = asyncio.get_running_loop()
    handlers, removed = {}, []
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", removed.append)

    partial = report(abort_reason="cancelled by user")

    async def run(config, *, progress, stop_event):
        assert not stop_event.is_set()
        handlers[stop_signal]()
        assert stop_event.is_set()
        return partial

    monkeypatch.setattr("giraffe.runner.run_suite", run)
    with pytest.raises(asyncio.CancelledError) as interrupted:
        await _run(RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}]))
    assert interrupted.value.giraffe_report is partial
    assert partial.abort_reason == "cancelled by user"
    assert set(handlers) == set(removed) == {signal.SIGINT, signal.SIGTERM}


@pytest.mark.parametrize('outcome,code', [('pass', 0), ('fail', 1)])
@pytest.mark.parametrize('closed', ['stdout', 'stderr'])
def test_closed_pipe_preserves_verdict_and_saved_report(tmp_path, outcome, code, closed):
    script = '''
import sys
from giraffe import cli
from giraffe.models import RunReport
async def run(config, **kwargs):
    for index in range(100):
        print('progress ' + str(index), file=sys.stderr, flush=True)
    return RunReport(run_id='pipe', started_at='now', finished_at='now', overall=sys.argv[2], manifest={})
cli._run = run
sys.exit(cli.main(['run', '--url', 'http://server', '--model', 'tiny', '--skip-preflight',
                  '--min-free-disk', '0', '--output', sys.argv[1]]))
'''
    directory = tmp_path / 'run'
    proc = subprocess.Popen([sys.executable, '-c', script, str(directory), outcome],
                            stdout=subprocess.PIPE if closed == 'stdout' else subprocess.DEVNULL,
                            stderr=subprocess.PIPE if closed == 'stderr' else subprocess.DEVNULL)
    getattr(proc, closed).close()
    assert proc.wait(timeout=20) == code
    assert json.loads((directory / 'report.json').read_text())['overall'] == outcome


@pytest.mark.parametrize('scenario', ['sustained', 'pack_control', 'pack_loaded', 'client_cancel',
                                      'tool_calling', 'traffic_stage_1'])
async def test_long_phases_have_human_progress_without_unset_budget_noise(scenario, monkeypatch, capsys):
    async def run(config, *, progress, stop_event):
        progress({'target': 'local', 'scenario': scenario, 'completed': 3})
        return report()
    monkeypatch.setattr('giraffe.runner.run_suite', run)
    await _run(RunConfig(targets=[{'name': 'local', 'url': 'http://server', 'model': 'tiny'}]))
    output = capsys.readouterr().err
    assert 'local:' in output
    assert scenario not in output and 'no request limit set' not in output


def test_missing_discovery_key_has_no_model_advice(monkeypatch, capsys):
    monkeypatch.delenv('MISSING_MODEL_KEY', raising=False)
    assert main(['run', '--url', 'http://server', '--api-key-env', 'MISSING_MODEL_KEY']) == 2
    message = capsys.readouterr().err
    assert 'API key environment variable MISSING_MODEL_KEY is not set' in message
    assert 'Provide --model' not in message


def test_config_model_discovery_uses_saved_transport_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv('SAVED_KEY', 'secret')
    seen=[]
    async def discover(body, **kwargs):
        seen.append(body)
        return {'models': ['tiny']}
    monkeypatch.setattr('giraffe.preflight.discover_models', discover)
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'config_version': 2, 'ca_bundle': '/saved.pem', 'proxy': 'http://proxy:123',
        'targets': [{'name': 'local', 'url': 'http://server', 'api_key_env': 'SAVED_KEY'}]}))
    config=configuration(parser().parse_args(['run', '--config', str(path)]))
    assert config.targets[0].model == 'tiny'
    assert (seen[0].api_key_env, seen[0].ca_bundle, seen[0].proxy) == ('SAVED_KEY', '/saved.pem', 'http://proxy:123')


@pytest.mark.parametrize('scenario', ['correctness_c1', 'initial', 'sustained', 'load_primary_combinations_short_short_constant_r1'])
@pytest.mark.parametrize('verbose', [False, True])
def test_performance_headers_hide_internal_window_ids(scenario, verbose, capsys):
    from tests.test_check_intent_round2_core import record
    value = report(requests=[record(target='local', scenario=scenario)])
    _print_result(value, Path('runs/report.json'), None, Path('runs'), {}, verbose=verbose)
    output = capsys.readouterr().out
    assert 'Performance · local ·' in output
    assert ('full:' in output) == verbose
    if not verbose:
        assert scenario not in output


@pytest.mark.parametrize('targets', [['invalid'], None, 'invalid', {'name': 'invalid'}])
def test_malformed_config_target_returns_configuration_error(tmp_path, capsys, targets):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'config_version': 2, 'targets': targets}))
    assert main(['run', '--config', str(path)]) == 4
    message = capsys.readouterr().err
    assert 'targets' in message
    assert 'Traceback' not in message
