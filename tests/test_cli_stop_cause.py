"""CLI interruption keeps its latched outcome without reading the journal."""

import asyncio
import json
import os
import signal

import pytest

from giraffe import cli, evidence, runner, states, storage
from giraffe.models import RunConfig
from tests.test_runner import FakeClient, client as client, config


def run_arguments(directory):
    return ["run", "--url", "http://localhost", "--model", "tiny", "--checks", "correctness",
            "--skip-preflight", "--json", "--output", str(directory),
            "--min-free-disk", "0", "--max-total-storage", "0"]


@pytest.mark.parametrize("cause", [None, {"kind": "user", "text": "cancelled by user", "scope": "gateway"}])
def test_engine_error_retains_cached_stop_fields_without_reading_journal(tmp_path, monkeypatch, capsys, cause):
    async def failed(config, **kwargs):
        if cause is not None:
            runner.stop_cause_sink.get()(cause)
        raise OSError("engine failed")

    def no_recovery(*args, **kwargs):
        pytest.fail("Engine error parsed the evidence journal")

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", lambda config: asyncio.sleep(0))
    monkeypatch.setattr(runner, "run_suite", failed)
    monkeypatch.setattr(evidence, "recover_evidence", no_recovery)
    assert cli.main(run_arguments(tmp_path)) == states.EXIT_CODE_USAGE
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    metadata = json.loads((tmp_path / ".web.json").read_text())
    assert metadata["state"] == states.RUN_ERROR
    assert output["outcome"] == "error"
    assert output["stop_kind"] == metadata["stop_kind"] == (cause["kind"] if cause else None)
    assert output["abort_reason"] == metadata["abort_reason"] == (cause["text"] if cause else None)


@pytest.mark.parametrize("kind,text,live_kind,live_write_failure", [
    ("user", "cancelled by user", "user", False),
    ("user", "cancelled by user", "user", True),
    ("request_budget", "maximum request budget reached", None, False),
])
def test_live_metadata_uses_announced_cause_and_final_metadata_uses_latched_cause(
        tmp_path, monkeypatch, capsys, kind, text, live_kind, live_write_failure):
    from giraffe import reporting

    writes = []
    atomic_write = reporting._atomic_write

    def record_write(path, contents):
        if path.name == ".web.json":
            metadata = json.loads(contents)
            writes.append(metadata)
            if live_write_failure and metadata.get("state") == states.RUN_RUNNING and metadata.get("stop_kind"):
                raise OSError("live metadata unavailable")
        return atomic_write(path, contents)

    async def interrupted(config, **kwargs):
        initial = json.loads((tmp_path / ".web.json").read_text())
        assert initial.get("stop_kind") is initial.get("abort_reason") is None
        sink = runner.stop_cause_sink.get()
        cause = {"kind": kind, "text": text, "scope": None}
        sink(cause)
        sink(cause)
        metadata = json.loads((tmp_path / ".web.json").read_text())
        assert metadata.get("stop_kind") == (None if live_write_failure else live_kind)
        assert metadata.get("abort_reason") == (text if live_kind and not live_write_failure else None)
        raise KeyboardInterrupt

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", lambda config: asyncio.sleep(0))
    monkeypatch.setattr(runner, "run_suite", interrupted)
    monkeypatch.setattr(reporting, "_atomic_write", record_write)
    assert cli.main(run_arguments(tmp_path)) == 130
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    metadata = json.loads((tmp_path / ".web.json").read_text())
    assert output["stop_kind"] == metadata["stop_kind"] == kind
    assert output["abort_reason"] == metadata["abort_reason"] == text
    assert len([item for item in writes if item["state"] == states.RUN_RUNNING]) == (2 if live_kind else 1)


@pytest.mark.parametrize("cleanup_failure", [None, KeyboardInterrupt, OSError])
def test_forced_interrupt_uses_cached_cause_and_attempts_metadata_once(tmp_path, monkeypatch, capsys, cleanup_failure):
    async def prepared(config):
        pass

    async def interrupted(config, **kwargs):
        runner.stop_cause_sink.get()({"kind": "user", "text": "cancelled by user", "scope": "gateway"})
        raise KeyboardInterrupt

    def no_recovery(*args, **kwargs):
        pytest.fail("Interrupt path parsed the evidence journal")

    write_metadata = storage.write_metadata_unless
    attempts = []

    def save_interruption(directory, metadata, predicate):
        attempts.append(metadata)
        # A third physical signal must be deferred while metadata is finalized.
        os.kill(os.getpid(), signal.SIGINT)
        if cleanup_failure:
            raise cleanup_failure
        return write_metadata(directory, metadata, predicate)

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", prepared)
    monkeypatch.setattr(runner, "run_suite", interrupted)
    monkeypatch.setattr(evidence, "recover_evidence", no_recovery)
    monkeypatch.setattr(storage, "write_metadata_unless", save_interruption)
    assert cli.main(run_arguments(tmp_path)) == 130
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["exit_code"] == 130
    assert lines[-1]["stop_kind"] == "user"
    assert lines[-1]["abort_reason"] == "cancelled by user"
    assert len(attempts) == 1
    assert attempts[0]["stop_kind"] == "user"


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
async def test_signals_keep_head_stop_event_behavior(monkeypatch, stop_signal):
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: None)

    async def run(config, *, progress, stop_event):
        handlers[stop_signal]()
        assert stop_event.is_set()
        assert not hasattr(stop_event, "stop_kind")
        other = signal.SIGTERM if stop_signal == signal.SIGINT else signal.SIGINT
        with pytest.raises(KeyboardInterrupt):
            handlers[other]()
        assert not hasattr(stop_event, "stop_kind")
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "run_suite", run)
    with pytest.raises(asyncio.CancelledError):
        await cli._run(RunConfig(targets=[{"name": "local", "url": "http://server", "model": "tiny"}]))


@pytest.mark.parametrize("output_failure", [KeyboardInterrupt, OSError])
def test_interrupt_output_failure_still_exits_130(tmp_path, monkeypatch, capsys, output_failure):
    async def interrupted(config, **kwargs):
        runner.stop_cause_sink.get()({"kind": "request_budget", "text": "maximum request budget reached", "scope": None})
        raise KeyboardInterrupt

    original = cli.json.dumps
    emitted = []

    def output(value, *args, **kwargs):
        if isinstance(value, dict) and value.get("outcome") == "interrupted":
            emitted.append(value)
            if len(emitted) == 1:
                raise output_failure
        return original(value, *args, **kwargs)

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", lambda config: asyncio.sleep(0))
    monkeypatch.setattr(runner, "run_suite", interrupted)
    monkeypatch.setattr(cli.json, "dumps", output)
    assert cli.main(run_arguments(tmp_path)) == states.EXIT_CODE_CANCELLED
    if output_failure is KeyboardInterrupt:
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert lines[-1]["stop_kind"] == "request_budget"
        assert lines[-1]["abort_reason"] == "maximum request budget reached"
    assert json.loads((tmp_path / ".web.json").read_text())["stop_kind"] == "request_budget"


@pytest.mark.parametrize("flush_failure", [KeyboardInterrupt, OSError])
def test_interrupt_final_stream_flush_cannot_escape(tmp_path, monkeypatch, capsys, flush_failure):
    async def interrupted(config, **kwargs):
        runner.stop_cause_sink.get()({"kind": "user", "text": "cancelled by user", "scope": None})
        raise KeyboardInterrupt

    class BrokenFlush:
        def write(self, value):
            return stdout.write(value)

        def flush(self):
            raise flush_failure

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", lambda config: asyncio.sleep(0))
    monkeypatch.setattr(runner, "run_suite", interrupted)
    stdout = cli.sys.stdout
    with monkeypatch.context() as patch:
        patch.setattr(cli.sys, "stdout", BrokenFlush())
        assert cli.main(run_arguments(tmp_path)) == 130
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(lines) == 1
    assert lines[0]["stop_kind"] == "user"
    assert json.loads((tmp_path / ".web.json").read_text())["stop_kind"] == "user"


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_real_runner_cli_signal_outcome_matches_report_and_journal(
        tmp_path, monkeypatch, capsys, client, stop_signal):
    from giraffe import reporting

    class SignalClient(FakeClient):
        fired = False

        async def execute(self, spec, **kwargs):
            if not type(self).fired:
                type(self).fired = True
                os.kill(os.getpid(), stop_signal)
                await asyncio.sleep(.02)
            return await super().execute(spec, **kwargs)

    write_report = reporting.write_report
    monkeypatch.setattr(reporting, "write_report", lambda *args, **kwargs: write_report(
        *args, **{**kwargs, "preserve_evidence": True}))
    monkeypatch.setattr(runner, "LLMClient", SignalClient)
    source, directory = tmp_path / "config.json", tmp_path / "run"
    source.write_text(config(samples=20, concurrency=1, max_requests=None).model_dump_json())
    assert cli.main(["run", "--config", str(source), "--skip-preflight", "--json", "--output", str(directory),
                     "--min-free-disk", "0", "--max-total-storage", "0"]) == 130
    captured = capsys.readouterr()
    output = json.loads(captured.out.splitlines()[-1])
    report = json.loads((directory / "report.json").read_text())
    metadata = json.loads((directory / ".web.json").read_text())
    recovered = evidence.recover_evidence(directory, metadata)
    kind, text = "user", "cancelled by user"
    assert output["stop_kind"] == metadata["stop_kind"] == runner.stop_kind_of(report) == kind
    assert runner.stop_kind_of(recovered) == kind
    assert output["abort_reason"] == metadata["abort_reason"] == report["abort_reason"] == text
    stops = [json.loads(line) for line in captured.err.splitlines() if line.startswith('{"event"')]
    assert any(event["event"] == "run_stopping" for event in stops)
    assert all(event["stop_kind"] == kind for event in stops
               if event["event"] in {"run_stopping", "run_finished"})


@pytest.mark.parametrize("partial", [False, True])
def test_ci_output_oserror_after_cancel_remains_a_json_error(tmp_path, monkeypatch, capsys, client, partial):
    from giraffe import reporting
    from giraffe.models import RunReport

    write_report = reporting.write_report

    def export(*args, **kwargs):
        if not partial:
            os.kill(os.getpid(), signal.SIGINT)
        return write_report(*args, **kwargs)

    async def cancelled(config, **kwargs):
        error = asyncio.CancelledError()
        error.giraffe_report = RunReport(run_id="partial", started_at="now", finished_at="now",
                                        overall="inconclusive", manifest={})
        raise error

    def ci(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "/ro/junit.xml")

    if partial:
        monkeypatch.setattr(runner, "run_suite", cancelled)
    monkeypatch.setattr(reporting, "write_report", export)
    monkeypatch.setattr(cli, "_ci_outputs", ci)
    source, directory = tmp_path / "config.json", tmp_path / "run"
    source.write_text(config(samples=2, concurrency=1).model_dump_json())
    assert cli.main(["run", "--config", str(source), "--skip-preflight", "--json", "--output", str(directory),
                     "--min-free-disk", "0", "--max-total-storage", "0"]) == states.EXIT_CODE_USAGE
    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "error" and output["exit_code"] == 4
    assert output["errors"][0]["type"] == "PermissionError"
    assert "/ro/junit.xml" in output["errors"][0]["message"]


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("budget", [None, 1])
def test_cli_live_export_uses_runner_finished_fields(tmp_path, client, monkeypatch, capsys, quiet, budget):
    from fastapi.testclient import TestClient
    from giraffe import reporting, web

    write = reporting.write_report
    seen = []
    monkeypatch.setattr(web, "_EVENT_WAIT_SECONDS", .001)

    def export(report, directory, **kwargs):
        meta = json.loads((directory / ".web.json").read_text())
        assert meta["state"] == "running"
        expected = runner.finished_fields(runner.cause_of(report), report.abort_reason)
        assert expected["stop_kind"] == ("request_budget" if budget else None)
        app = web.create_app(tmp_path)
        with TestClient(app, base_url="http://localhost:8787") as api:
            detail = api.get(f"/api/runs/{directory.name}").json()
            row = api.get("/api/runs").json()["runs"][0]
            feed = api.get(f"/api/runs/{directory.name}/events?after=0").json()
        for surface in (meta, detail, row, feed):
            assert {key: surface[key] for key in expected} == expected
            assert surface["state"] == "running"
        seen.append(True)
        return write(report, directory, **kwargs)

    monkeypatch.setattr(reporting, "write_report", export)
    source, directory = tmp_path / "config.json", tmp_path / "run"
    source.write_text(config(samples=4, concurrency=1, max_requests=budget).model_dump_json())
    assert cli.main(["run", "--config", str(source), "--skip-preflight", "--json", "--output", str(directory),
                     "--min-free-disk", "0", "--max-total-storage", "0", *(["--quiet"] if quiet else [])]) == (2 if budget else 0)
    assert seen == [True]
    assert json.loads(capsys.readouterr().out)["stop_kind"] == ("request_budget" if budget else None)


@pytest.mark.parametrize("reason", [None, "verdict-specific reason"])
def test_late_keyboard_interrupt_preserves_report_abort_reason(tmp_path, monkeypatch, capsys, reason):
    from giraffe.models import RunReport

    async def run(config, **kwargs):
        cause = {"kind": "memory", "text": "memory limit reached", "scope": None}
        runner.stop_cause_sink.get()(cause)
        return RunReport(run_id="done", started_at="now", finished_at="now", overall="pass",
                         abort_reason=reason, observations={"stop_cause": cause}, manifest={})

    def ci(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("giraffe.local_tokenizer.ensure_tokenizers", lambda config: asyncio.sleep(0))
    monkeypatch.setattr(runner, "run_suite", run)
    monkeypatch.setattr(cli, "_ci_outputs", ci)
    assert cli.main(run_arguments(tmp_path)) == 130
    output = json.loads(capsys.readouterr().out)
    report = json.loads((tmp_path / "report.json").read_text())
    meta = json.loads((tmp_path / ".web.json").read_text())
    assert output["outcome"] == "interrupted" and output["stop_kind"] == "memory"
    assert output["abort_reason"] == report["abort_reason"] == meta["abort_reason"] == reason
    assert meta["state"] == "completed"
