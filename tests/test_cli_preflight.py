import json

import httpx
import pytest

from giraffe.cli import main


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_failed_preflight_stops_before_artifacts(tmp_path, monkeypatch, capsys, output_format):
    from tests.test_connection import endpoint

    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(404))
    monkeypatch.setattr("giraffe.cli._run", lambda *a: pytest.fail("Suite ran after failed preflight"))
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://server", "--model", "wrong", "--output", str(directory),
                 "--output-format", output_format]) == 3
    captured = capsys.readouterr()
    assert "giraffe: preflight failed for local:" in captured.err
    assert "Check the model name" in captured.err
    assert captured.err.strip().endswith("Use --skip-preflight to run anyway.")
    assert len(requests) == 1 and not directory.exists()
    if output_format == "json":
        value = json.loads(captured.out)
        assert value["outcome"] == "blocked" and value["exit_code"] == 3
        assert isinstance(value["warnings"], list)
        assert value["message"].endswith("Use --skip-preflight to run anyway.")


def values(**updates):
    return {"targets": [{"name": "local", "url": "http://server", "model": "tiny"}],
            "checks": ["correctness"], **updates}


@pytest.mark.parametrize("status", [401, 403, 404, "unreachable", "connect_tls", "invalid_url", "tls", "auth"])
def test_cli_preflight_errors_are_bounded_and_actionable(status, tmp_path, monkeypatch, capsys):
    from tests.test_connection import endpoint

    def respond(request):
        if status == "unreachable":
            raise httpx.ConnectError("private transport details", request=request)
        if status == "connect_tls":
            raise httpx.ConnectError("private TLS failure", request=request)
        if status == "invalid_url":
            raise httpx.InvalidURL("private URL")
        return httpx.Response(status)

    requests, _ = endpoint(monkeypatch, respond)
    config = values()
    if status == "tls":
        config["ca_bundle"] = "/missing/private-tls.pem"
    if status == "auth":
        monkeypatch.delenv("MISSING_PREFLIGHT_KEY", raising=False)
        config["targets"][0]["api_key_env"] = "MISSING_PREFLIGHT_KEY"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    directory = tmp_path / "output"
    assert main(["run", "--config", str(path), "--output", str(directory)]) == 3
    captured = capsys.readouterr()
    expected = {401: "denied access", 403: "denied access", 404: "Check the model name",
                "unreachable": "Could not reach", "connect_tls": "Could not reach",
                "invalid_url": "Could not connect",
                "tls": "TLS trust file", "auth": "environment variable"}[status]
    assert expected in captured.err and "private" not in captured.err
    assert captured.err.strip().endswith("Use --skip-preflight to run anyway.")
    assert len(requests) == (0 if status in {"tls", "auth"} else 1)
    assert not directory.exists()


def test_successful_preflight_checks_each_target_with_run_settings(tmp_path, monkeypatch, capsys):
    from giraffe.models import RunReport
    from tests.test_connection import answer, endpoint

    config = values(request_options={"temperature": 0}, max_requests=1)
    config["targets"].append({"name": "other", "url": "http://other/v1", "model": "second",
                              "headers_env": {"X-Gateway": "PREFLIGHT_HEADER"}})
    monkeypatch.setenv("PREFLIGHT_HEADER", "header-value")
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json=answer()))
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))

    async def run(config, **kwargs):
        assert len(requests) == 2
        return RunReport(run_id="ok", started_at="now", finished_at="now", overall="pass",
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(["run", "--config", str(path), "--output", str(tmp_path / "output"),
                 "--output-format", "json"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    from giraffe.models import RunConfig
    from giraffe.planning import request_plan

    assert result["warnings"] == request_plan(RunConfig.model_validate(config))["warnings"]
    assert captured.err.startswith("Warning:")
    for request, model in zip(requests, ["tiny", "second"], strict=True):
        payload = json.loads(request.content)
        assert payload["model"] == model and payload["max_tokens"] == 32
        assert payload["temperature"] == 0 and payload["stream"] is False
    assert requests[1].headers["X-Gateway"] == "header-value"


@pytest.mark.parametrize("flag", [[], ["--show-config"]])
def test_validate_text_summary_and_optional_config(flag, tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values(max_requests=1)))
    monkeypatch.setattr("giraffe.preflight.check_target", lambda *a: pytest.fail("Validation probed"))
    assert main(["validate", "--config", str(path), *flag]) == 0
    output = capsys.readouterr().out
    assert output.startswith("Configuration is valid. No traffic was sent.\nTargets: local\n")
    assert "Checks: correctness\nmax_requests: 1\n" in output and "Warning: " in output
    assert ('"targets":' in output) == bool(flag)
    if not flag:
        assert len(output.splitlines()) < 12


@pytest.mark.parametrize("command", ["run", "validate", "preview", "ui"])
def test_missing_config_has_friendly_error(command, tmp_path, capsys):
    path = tmp_path / "nope.yaml"
    assert main([command, "--config", str(path)]) == 4
    captured = capsys.readouterr()
    message = json.loads(captured.out)["errors"][0]["message"] if command == "preview" else captured.err.strip()
    assert f"config file not found: {path}" in message and "Errno" not in message


@pytest.mark.parametrize("override,expected", [
    ({"targets": [{"url": "http://server", "model": "tiny"}]}, "name: local"),
    ({"targets": [{"name": "local", "url": "localhost:11434", "model": "tiny"}]},
     "target url must start with http:// or https:// (for example http://localhost:11434/v1)"),
    ({"checks": ["foo", "bar"]}, "Unknown check name(s): bar, foo. Valid names: access"),
])
def test_friendly_configuration_messages(override, expected, tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values(**override)))
    assert main(["validate", "--config", str(path)]) == 4
    assert expected in capsys.readouterr().err


def test_unknown_cli_checks_list_bad_and_valid_names(capsys):
    assert main(["run", "--url", "http://server", "--model", "tiny", "--checks", "foo,bar"]) == 4
    message = capsys.readouterr().err
    assert "bar, foo" in message and "Valid names: access" in message


def test_run_help_has_all_flags_examples_and_exit_codes(capsys):
    from giraffe.cli import configuration, parser

    with pytest.raises(SystemExit) as stopped:
        main(["run", "--help"])
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "--structured-output" in output and "--json " not in output
    assert "--checks CHECKS" in output and "first_output" in output
    assert "llama3.2:3b" in output and "--config giraffe.yaml" in output and "--baseline runs/previous" in output
    assert "2  inconclusive" in output and "3  blocked" in output and "4  usage" in output
    app = parser()
    run = app._subparsers._group_actions[0].choices["run"]
    assert all(action.help for action in run._actions)
    for flag in ["--json", "--structured-output"]:
        config = configuration(app.parse_args(["run", "--url", "http://server", "--model", "tiny", flag]))
        assert config.structured_json and "json" in config.checks


def test_parser_errors_exit_four():
    from giraffe.cli import parser

    with pytest.raises(SystemExit) as stopped:
        parser().parse_args(["run", "--samples", "bad"])
    assert stopped.value.code == 4


def test_ui_port_in_use_prints_no_banner_or_creates_app(monkeypatch, capsys):
    import errno

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr("giraffe.cli.socket.socket", lambda *a: Listener())
    monkeypatch.setattr("giraffe.web.create_app", lambda **kw: pytest.fail("Created app before binding"))
    assert main(["ui", "--port", "8765"]) == 4
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.strip() == "giraffe: port 8765 is in use; choose another with --port"


@pytest.mark.parametrize("json_progress", [False, True])
async def test_progress_throttles_text_and_preserves_json_events(json_progress, monkeypatch, capsys):
    from giraffe.cli import _run
    from giraffe.models import RunConfig, RunReport

    now = [10.0]
    monkeypatch.setattr("giraffe.cli.time.monotonic", lambda: now[0])
    events = []

    async def run(config, *, progress, stop_event):
        for stage, instant in [("warm", 10), ("warm", 11), ("warm", 14.9), ("warm", 15),
                               ("generation", 15.1), ("generation", 16)]:
            now[0] = instant
            event = {"target": "local", "scenario": stage, "completed": len(events),
                     "elapsed_seconds": instant}
            events.append(event)
            progress(event)
        return RunReport(run_id="progress", started_at="now", finished_at="now", overall="pass", manifest={})

    monkeypatch.setattr("giraffe.runner.run_suite", run)
    await _run(RunConfig.model_validate(values()), json_progress=json_progress)
    output = capsys.readouterr().err.splitlines()
    if json_progress:
        assert [json.loads(line) for line in output] == events
    else:
        assert len(output) == 3
        assert "warm" in output[0] and "warm" in output[1] and "generation" in output[2]


@pytest.mark.parametrize("command", ["inspect", "export", "baseline", "manifest", "compare"])
def test_report_inputs_accept_run_directories(command, tmp_path, monkeypatch, capsys):
    from giraffe.models import RunReport

    source = tmp_path / "saved-run"
    source.mkdir()
    report = RunReport(run_id="saved", started_at="now", finished_at="now", overall="pass",
                       manifest={"config": values()})
    (source / "report.json").write_text(report.model_dump_json())
    if command in {"inspect", "export"}:
        assert main([command, str(source)]) == 0
    elif command == "baseline":
        destination = tmp_path / "baseline.json"
        args = ["baseline", "save", str(source), str(destination)]
        assert main(args) == (2 if command == "compare" else 0)
        assert main(args) == 4
        assert "use --replace" in capsys.readouterr().err
        assert main([*args, "--replace"]) == 0
    else:
        async def run(config):
            return report

        monkeypatch.setattr("giraffe.cli._run", run)
        args = ["run", "--skip-preflight", "--output", str(tmp_path / "new-run")]
        if command == "manifest":
            args += ["--manifest", str(source)]
        else:
            args += ["--url", "http://server", "--model", "tiny", "--baseline", str(source)]
        assert main(args) == (2 if command == "compare" else 0)


@pytest.mark.parametrize("output_format", ["text", "json"])
@pytest.mark.parametrize("summary", [None, "Could not connect to local (ConnectError); no checks could run"])
def test_run_surfaces_optional_overall_summary(summary, output_format, tmp_path, monkeypatch, capsys):
    from giraffe.models import RunReport

    async def run(config, **kwargs):
        return RunReport(run_id="summary", started_at="now", finished_at="now", overall="blocked",
                         manifest={"config": config.model_dump(mode="json")},
                         observations={"summary": summary} if summary else {}, abort_reason="stopped")

    monkeypatch.setattr("giraffe.cli._run", run)
    assert main(["run", "--url", "http://server", "--model", "tiny", "--skip-preflight",
                 "--output", str(tmp_path / "output"), "--output-format", output_format]) == 3
    output = capsys.readouterr().out
    if output_format == "json":
        result = json.loads(output)
        assert result.get("summary") == summary
        if summary is None:
            assert "summary" not in result
    else:
        lines = output.splitlines()
        assert lines[0] == "BLOCKED: 0 requests"
        assert lines[1] == (summary or "Stopped: stopped")


@pytest.mark.parametrize("source", ["config", "manifest"])
def test_request_pack_replay_keeps_exact_traffic_after_unrecorded_probe(source, tmp_path, monkeypatch, capsys):
    from giraffe.models import RunConfig, RunReport
    from tests.test_connection import endpoint

    pack = {"version": 1, "cases": [
        {"fixture_id": f"question-{index}", "messages": [{"role": "user", "content": f"Return 42 for {index}"}],
         "scorer": "exact", "expected": "42"} for index in range(6)]}
    config = RunConfig.model_validate(values(request_source="request_pack", request_pack=pack, stream=False))
    path = tmp_path / "source.json"
    if source == "manifest":
        path.write_text(RunReport(run_id="original", started_at="now", finished_at="now", overall="pass",
                                 manifest={"config": config.model_dump(mode="json")}).model_dump_json())
    else:
        path.write_text(config.model_dump_json())
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}]}))
    assert main(["run", "--" + source, str(path), "--output", str(tmp_path / "output"),
                 "--output-format", "json"]) in {0, 2}
    envelope = json.loads(capsys.readouterr().out)
    saved = json.loads((tmp_path / "output" / "report.json").read_text())
    assert requests[0].headers.get("X-Giraffe-Preflight") == "1"
    assert all("X-Giraffe-Preflight" not in request.headers for request in requests[1:])
    assert len(requests) == len(saved["requests"]) + 1 == 13
    expected = [{"model": "tiny", "messages": case["messages"], "max_tokens": config.max_output_tokens,
                 "stream": False, "n": 1} for case in pack["cases"]]
    assert [json.loads(request.content) for request in requests[1:]] == expected * 2
    assert saved["manifest"]["config"] == config.model_dump(mode="json")
    assert envelope["request_count"] == 12
    assert {record["fixture_id"] for record in saved["requests"]} == {
        f"pack:question-{index}" for index in range(6)}
    assert {record["scenario"] for record in saved["requests"]} == {"pack_control", "pack_loaded"}


@pytest.mark.parametrize("status", [200, 429, 500, 503, 400, "read_timeout", "connect_timeout", "deadline", "protocol"])
@pytest.mark.parametrize("output_format", ["text", "json"])
def test_advisory_preflight_proceeds_and_keeps_warnings(status, output_format, tmp_path, monkeypatch, capsys):
    from giraffe.models import RunReport
    from tests.test_connection import endpoint

    def respond(request):
        if status == "read_timeout":
            raise httpx.ReadTimeout("private timeout", request=request)
        if status == "connect_timeout":
            raise httpx.ConnectTimeout("private timeout", request=request)
        if status == "deadline":
            raise TimeoutError("private deadline")
        if status == "protocol":
            raise httpx.RemoteProtocolError("private protocol", request=request)
        return httpx.Response(status, json={"choices": [{"message": {"reasoning": "private thinking"},
                                                        "finish_reason": "length"}]})

    requests, _ = endpoint(monkeypatch, respond)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values(targets=[
        {"name": name, "url": "http://server", "model": "tiny"} for name in ["local", "other"]])))

    async def run(config, **kwargs):
        assert len(requests) == 2
        return RunReport(run_id="advisory", started_at="now", finished_at="now", overall="fail",
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.cli._run", run)
    directory = tmp_path / "run"
    assert main(["run", "--config", str(path), "--output", str(directory),
                 "--output-format", output_format]) == 1
    captured = capsys.readouterr()
    for secret in ["private timeout", "private deadline", "private protocol", "private thinking"]:
        assert secret not in captured.err + captured.out
    for target in ["local", "other"]:
        assert f"Warning: preflight for {target}: " in captured.err
    assert captured.err.count("; continuing.") == 2
    assert (directory / "report.json").exists()
    if output_format == "json":
        warnings = json.loads(captured.out)["warnings"]
        assert sum(warning.startswith("preflight for ") for warning in warnings) == 2
        assert all(warning.endswith("; continuing.") for warning in warnings if warning.startswith("preflight for "))


def test_ui_listener_reuses_address_before_binding(monkeypatch, tmp_path, capsys):
    import socket

    events = []

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def setsockopt(self, *args):
            events.append(("option", args))

        def bind(self, address):
            events.append(("bind", address))

        def listen(self, backlog):
            events.append(("listen", backlog))

    listener = Listener()
    monkeypatch.setattr("giraffe.cli.socket.socket", lambda *args: listener)
    monkeypatch.setattr("giraffe.web.create_app", lambda **kwargs: object())

    def run(self, *, sockets):
        assert sockets == [listener]
        events.append(("run",))

    monkeypatch.setattr("uvicorn.Server.run", run)
    assert main(["ui", "--port", "8765", "--runs-dir", str(tmp_path)]) == 0
    assert events == [("option", (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)),
                      ("bind", ("127.0.0.1", 8765)), ("listen", 128), ("run",)]


@pytest.mark.parametrize("policy,retained", [("none", False), ("failures", True), ("all", True)])
def test_cli_prints_the_retained_check_summary(policy, retained, tmp_path, monkeypatch, capsys):
    from giraffe.fixtures import answer_difference
    from giraffe.models import CheckResult, RunReport

    async def run(config):
        return RunReport(run_id="mismatch", started_at="now", finished_at="now", overall="fail",
                         manifest={"config": config.model_dump(mode="json")}, checks=[CheckResult(
                             id="correctness", target="local", title="Correctness", status="fail",
                             summary="Answers differ." + answer_difference("EXPECTED_SECRET", "RECEIVED_SECRET"))])

    path = tmp_path / "config.json"
    path.write_text(json.dumps(values(retention=policy)))
    monkeypatch.setattr("giraffe.cli._run", run)
    directory = tmp_path / "run"
    assert main(["run", "--config", str(path), "--skip-preflight", "--output", str(directory)]) == 1
    captured = capsys.readouterr()
    saved = json.loads((directory / "report.json").read_text())
    assert saved["checks"][0]["summary"] in captured.out
    for secret in ["EXPECTED_SECRET", "RECEIVED_SECRET"]:
        assert (secret in captured.out) == retained
        assert (secret in (directory / "report.html").read_text()) == retained
        assert secret not in captured.err



def test_invalid_endpoint_port_blocks_before_traffic(tmp_path, monkeypatch, capsys):
    from tests.test_connection import endpoint

    requests, _ = endpoint(monkeypatch, lambda request: pytest.fail("Invalid URL sent traffic"))
    monkeypatch.setattr("giraffe.cli._run", lambda *args: pytest.fail("Invalid URL ran suite"))
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://server:bad", "--model", "tiny",
                 "--output", str(directory), "--output-format", "json"]) == 3
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["message"].endswith("Use --skip-preflight to run anyway.")
    assert not requests and not directory.exists()


@pytest.mark.parametrize("error,blocking", [(TimeoutError, False), (httpx.ReadTimeout, False),
                                           (httpx.RemoteProtocolError, False), (httpx.ConnectError, True)])
def test_probe_exception_policy_before_a_request_record(error, blocking, tmp_path, monkeypatch, capsys):
    from giraffe.models import RunReport

    async def execute(self, spec):
        raise error("private probe error")

    async def run(config, **kwargs):
        assert not blocking
        return RunReport(run_id="probe-error", started_at="now", finished_at="now", overall="inconclusive",
                         manifest={"config": config.model_dump(mode="json")})

    monkeypatch.setattr("giraffe.preflight._PreflightClient.execute", execute)
    monkeypatch.setattr("giraffe.cli._run", run)
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://server", "--model", "tiny", "--output", str(directory),
                 "--output-format", "json"]) == (3 if blocking else 2)
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert (directory / "report.json").exists() == (not blocking)
    assert "private probe error" not in captured.err + captured.out
    if blocking:
        assert result["message"].endswith("Use --skip-preflight to run anyway.")
    else:
        assert any(warning.startswith("preflight for local:") for warning in result["warnings"])
