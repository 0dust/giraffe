"""The real runner and its API/CLI projections share one stop-cause contract."""

import asyncio
import json
import threading

import httpx

import pytest

from giraffe import judging, resources, runner, web
from giraffe.client import LLMClient
from giraffe.evidence import recover_evidence
from tests.test_runner import (FakeClient, client as client, config, judged_pack_config,
                               judge_summary)
from tests.test_web import browser, finished, start

TOKEN_PROBE = LLMClient.token_evidence_probe


async def wait_event(api, identifier, name):
    for _ in range(500):
        feed = (await api.get(f"/api/runs/{identifier}/events?after=0")).json()
        if any(event["event"] == name for event in feed["events"]):
            return feed
        await asyncio.sleep(.01)
    pytest.fail(f"No {name} for {identifier}")


async def assert_stop_contract(api, root, identifier, expected, *, cursor=None, acks=()):
    detail = await finished(api, identifier)
    assert detail["state"] in {"completed", "interrupted"}, detail.get("error")
    report = detail["report"]
    assert report is not None, detail
    feed = (await api.get(f"/api/runs/{identifier}/events?after=0")).json()
    reset = (await api.get(f"/api/runs/{identifier}/events?after=1000000000")).json()
    row = next(row for row in (await api.get("/api/runs")).json()["runs"] if row["id"] == identifier)
    metadata = json.loads((root / identifier / ".web.json").read_text())
    recovered = recover_evidence(root / identifier, metadata)
    assert runner.stop_kind_of(report) == expected
    for surface in (detail, row, feed, reset, metadata):
        assert "stop_kind" in surface
        assert surface["stop_kind"] == expected
    assert recovered is not None
    assert runner.stop_kind_of(recovered) == expected
    assert (recovered.observations.get("stop_cause") or {}).get("scope") == (
        report["observations"].get("stop_cause") or {}).get("scope")
    events = feed["events"]
    assert not feed["reset"]
    assert [event["seq"] for event in events] == sorted({event["seq"] for event in events})
    assert events[-1]["event"] == "run_finished" and events[-1]["state"] == detail["state"]
    assert events[-1]["stop_kind"] == expected
    assert events[-1]["abort_reason"] == detail["abort_reason"]
    stops = [event for event in events if event["event"] in {"run_stopping", "run_finished"}]
    for event in stops:
        assert "stop_kind" in event and "abort_reason" in event
        if event["stop_kind"] is not None:
            assert event["stop_kind"] == expected
    if cursor is not None:
        after_cancel = [event for event in stops if event["seq"] > cursor and event["event"] == "run_stopping"]
        assert [event["stop_kind"] for event in after_cancel if event.get("state") == "stopping"] == list(acks)
        if acks:
            assert "stop_kind" in after_cancel[0]
    return detail, events


# (scenario, acknowledgment kinds, runner stopping kinds, authoritative kind)
CASES = [
    ("none", (), (), None),
    ("before_start", (None,), ("user",), "user"),
    ("inference", (None,), ("user",), "user"),
    ("twice", (None, None), ("user",), "user"),
    ("duration_announced", ("duration",), ("duration",), "duration"),
    ("judging_budget", (None,), (), "request_budget"),
    ("judging", (None,), (), "user"),
    ("probe_retry", (None,), ("user",), "user"),
    ("duration", (), ("duration",), "duration"),
    ("budget", (), (), "request_budget"),
    ("budget_inference", (None,), ("user",), "user"),
    ("unresponsive", (), ("endpoints_unresponsive",), "endpoints_unresponsive"),
    ("errors", (), ("error_threshold",), "error_threshold"),
    ("forced_judging", (), (), "request_budget"),
    ("gateway", (None,), ("user",), "user"),
]


@pytest.mark.parametrize("scenario,acks,stopping,expected", CASES, ids=[row[0] for row in CASES])
async def test_stop_contract_matrix(tmp_path, monkeypatch, scenario, acks, stopping, expected):
    # Preserve the journal so every row also exercises the real recovery reader.
    write = web.write_report
    entered, release = threading.Event(), threading.Event()
    current = {}
    original_init, original_latch, original_emit = runner._Run.__init__, runner._Run._latch_stop, runner._Run.emit

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        current["run"] = self
        current["suite"] = asyncio.current_task()
        if scenario == "duration":
            self.deadline = 0
        if scenario == "unresponsive":
            self.endpoint_health["service"]["abort_reason"] = "Endpoint service unresponsive"
        if scenario == "errors":
            self.errors = self.config.stop_after_errors

    def latch(self, *args, **kwargs):
        if scenario == "twice" and args[0] == "user":
            assert release.wait(5)
        changed = original_latch(self, *args, **kwargs)
        if scenario == "budget_inference" and args[0] == "request_budget" and changed:
            self.emit("budget_latched")
            assert release.wait(5)
        return changed

    def emit(self, event, *args, **kwargs):
        original_emit(self, event, *args, **kwargs)
        if scenario == "duration_announced" and event == "request_started":
            self.deadline = 0
            self.stop_reason()
        if scenario == "duration_announced" and event == "run_stopping":
            assert release.wait(5)

    def export(*args, **kwargs):
        return write(*args, **{**kwargs, "preserve_evidence": True})

    monkeypatch.setattr(runner._Run, "__init__", initialize)
    monkeypatch.setattr(runner._Run, "_latch_stop", latch)
    monkeypatch.setattr(runner._Run, "emit", emit)
    monkeypatch.setattr(web, "write_report", export)
    monkeypatch.setattr(resources, "current_rss_bytes", lambda: 1)
    settings = config(checks=["serving"], samples=5, concurrency=1, max_requests=None, stream=False)
    if scenario in {"inference", "twice", "duration_announced", "gateway"}:
        FakeClient.delay = .2
    if scenario in {"budget", "budget_inference"}:
        settings.max_requests = 1
    if "judging" in scenario:
        settings = judged_pack_config(checks=["correctness"], max_requests=(
            1 if "budget" in scenario or scenario == "forced_judging" else None))
        settings.load_test.schedule.duration_seconds = 1

        async def evaluate(config, records, specs, *, stop_event, progress, **kwargs):
            progress({"event": "judging_started"})
            if scenario == "forced_judging":
                stop_event.set()
                current["suite"].cancel()
            await stop_event.wait()
            return judge_summary(records, stopped="cancelled")
        monkeypatch.setattr(judging, "judge_records", evaluate)
    if scenario == "probe_retry":
        settings.stream = True
        from tests.test_automatic_performance_evidence import completion
        actual_http = httpx.AsyncClient

        async def endpoint(request):
            if request.method == "GET":
                return httpx.Response(404)
            body = json.loads(request.content)
            if body.get("max_tokens") == 16:
                if body.get("logprobs"):
                    return httpx.Response(400, json={"error": {"param": "logprobs"}})
                current["run"].emit("probe_retry")
                await asyncio.Event().wait()
            return completion()

        def http_client(**kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(endpoint))
            return actual_http(**kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", http_client)
        monkeypatch.setattr(runner, "LLMClient", LLMClient)
        monkeypatch.setattr(LLMClient, "token_evidence_probe", TOKEN_PROBE)
    if scenario == "gateway":
        from giraffe import gateway_access

        async def gateway(run, target, client):
            with run.gateway_limit_phase():
                run.emit("gateway_waiting")
                await run.stop.wait()
                run.stop_reason()
        monkeypatch.setattr(gateway_access, "execute", gateway)
        settings = config(checks=["gateway_access"], max_requests=None)
    if scenario == "before_start":
        original_writer = web.EvidenceWriter

        def writer(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original_writer(*args, **kwargs)
        monkeypatch.setattr(web, "EvidenceWriter", writer)
    cursor = None
    try:
        async with browser(tmp_path) as api:
            identifier = await start(api, settings.model_dump(mode="json"))
            if scenario == "before_start":
                assert await asyncio.to_thread(entered.wait, 3)
            names = {"inference": "request_started", "twice": "request_started",
                     "duration_announced": "run_stopping", "budget_inference": "budget_latched",
                     "judging_budget": "judging_started", "judging": "judging_started", "gateway": "gateway_waiting",
                     "probe_retry": "probe_retry"}
            if scenario in names:
                await wait_event(api, identifier, names[scenario])
            if acks:
                cursor = 0 if scenario == "before_start" else (
                    await api.get(f"/api/runs/{identifier}/events?after=0")).json()["seq"]
                for index in range(max(1, len(acks))):
                    response = (await api.post(f"/api/runs/{identifier}/cancel", json={})).json()
                    assert response["result"] == ("already_stopping" if index else "stopping")
                    assert response["stop_kind"] == acks[index]
                    assert response["abort_reason"] == (
                        "maximum run duration reached" if acks[index] == "duration" else None)
                release.set()
            detail, events = await assert_stop_contract(api, tmp_path, identifier, expected, cursor=cursor, acks=acks)
            runner_stops = [event["stop_kind"] for event in events
                            if event["event"] == "run_stopping" and "state" not in event]
            assert runner_stops == list(stopping)
            assert all(event["abort_reason"] == detail["abort_reason"]
                       for event in events if event["event"] == "run_finished")
            if "judging" in scenario:
                assert detail["report"]["observations"]["judging"]["stop_reason"] == "cancelled"
            if scenario == "judging_budget":
                assert detail["abort_reason"] == "maximum request budget reached"
            if scenario == "gateway":
                assert detail["report"]["observations"]["stop_cause"]["scope"] == "gateway"
    finally:
        release.set()


@pytest.mark.parametrize("count", [1, 2])
def test_cli_interrupt_contract(tmp_path, monkeypatch, capsys, count):
    import signal
    from giraffe import cli

    handlers = {}
    loop = asyncio.new_event_loop()
    loop_type = type(loop)
    loop.close()
    install = loop_type.add_signal_handler
    from giraffe import reporting
    write = reporting.write_report
    monkeypatch.setattr(reporting, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))

    def capture(loop, sig, callback, *args):
        handlers[sig] = lambda: callback(*args)
        return install(loop, sig, callback, *args)

    signals = []

    def send_interrupt():
        signals.append(True)
        handlers[signal.SIGINT]()

    class InterruptClient(FakeClient):
        fired = False

        async def execute(self, spec, **kwargs):
            if not type(self).fired:
                type(self).fired = True
                send_interrupt()
                if count == 2:
                    # Delivery is independent of the request task, which the
                    # cooperative stop will cancel. Hold cleanup until then.
                    asyncio.get_running_loop().call_later(.02, send_interrupt)
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        await asyncio.sleep(.2)
            return await super().execute(spec, **kwargs)

    monkeypatch.setattr(loop_type, "add_signal_handler", capture)
    monkeypatch.setattr(runner, "LLMClient", InterruptClient)
    source, directory = tmp_path / "config.json", tmp_path / "run"
    source.write_text(config(samples=20, concurrency=1, max_requests=None).model_dump_json())
    code = cli.main(["run", "--config", str(source), "--skip-preflight", "--json", "--output", str(directory),
                     "--min-free-disk", "0", "--max-total-storage", "0"])
    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.out.splitlines()]
    progress = [json.loads(line) for line in captured.err.splitlines() if line.startswith('{"event"')]
    assert len(signals) == count
    assert code == lines[-1]["exit_code"] == 130
    metadata = json.loads((directory / ".web.json").read_text())
    recovered = recover_evidence(directory, metadata)
    saved = json.loads((directory / "report.json").read_text()) if (directory / "report.json").exists() else None
    expected = runner.stop_kind_of(saved or recovered)
    assert expected == "user"
    assert lines[-1]["stop_kind"] == metadata["stop_kind"] == runner.stop_kind_of(recovered) == expected
    assert metadata["abort_reason"] == "cancelled by user"
    assert any(line.get("event") == "run_stopping" for line in progress)
    for line in progress:
        if line.get("event") in {"run_stopping", "run_finished"}:
            assert "stop_kind" in line and "abort_reason" in line
            assert line["stop_kind"] == expected


@pytest.mark.parametrize("announced", [False, True])
async def test_retained_terminal_feed_and_restart_recover_last_journal_cause(tmp_path, monkeypatch, announced):
    from giraffe.storage import write_metadata

    suite, write = runner.run_suite, web.write_report

    async def interrupted(config, *, progress, stop_event, **kwargs):
        def stop(event):
            if announced or event["event"] not in {"run_stopping", "run_finished"}:
                progress(event)
            if event["event"] == "request_started":
                stop_event.set()
        await suite(config, progress=stop, stop_event=stop_event, **kwargs)
        # Simulate losing the report between engine completion and export.
        raise asyncio.CancelledError

    monkeypatch.setattr(web, "run_suite", interrupted)
    monkeypatch.setattr(web, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))
    monkeypatch.setattr(web, "_RECOVERY_SECONDS", .01)
    app = web.create_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8787") as api:
        async with app.router.lifespan_context(app):
            identifier = await start(api, config(max_requests=None).model_dump(mode="json"))
            detail = await finished(api, identifier)
            assert detail["state"] == "interrupted" and detail["recoverable"]
            feed = (await api.get(f"/api/runs/{identifier}/events")).json()
            assert feed["stop_kind"] == feed["events"][-1]["stop_kind"] == ("user" if announced else None)
            assert not (tmp_path / identifier / "report.json").exists()
            cursor = feed["seq"]
            previous_event = dict(feed["events"][-1])
        # Restart this server instance to exercise recovery with its retained
        # feed, rather than testing only a fresh reset response.
        async with app.router.lifespan_context(app):
            for _ in range(300):
                if (tmp_path / identifier / "report.json").exists():
                    break
                await asyncio.sleep(.01)
            poll = (await api.get(f"/api/runs/{identifier}/events?after={cursor}")).json()
            assert poll["seq"] == cursor + 1 and not poll["reset"]
            assert len(poll["events"]) == 1
            delta = poll["events"][0]
            assert delta["event"] == "run_finished" and delta["seq"] == cursor + 1
            assert delta["stop_kind"] == "user"
            assert delta["abort_reason"].startswith("Giraffe stopped before this run finished;")
            again = (await api.get(f"/api/runs/{identifier}/events?after={poll['seq']}")).json()
            assert again["seq"] == poll["seq"] and again["events"] == []
            detail, events = await assert_stop_contract(api, tmp_path, identifier, "user")
            assert events[-2] == previous_event
            assert detail["report"]["observations"]["interrupted"]
            assert detail["abort_reason"].startswith("Giraffe stopped before this run finished;")
            assert events[-1]["abort_reason"] == detail["abort_reason"]
    # A fresh server reads the recovered kind from disk on detail/list/reset.
    async with browser(tmp_path) as api:
        detail = (await api.get(f"/api/runs/{identifier}")).json()
        row = (await api.get("/api/runs")).json()["runs"][0]
        reset = (await api.get(f"/api/runs/{identifier}/events?after=0")).json()
        assert reset["reset"] and reset["events"] == []
        assert detail["stop_kind"] == row["stop_kind"] == reset["stop_kind"] == "user"
    # The saved report remains authoritative over stale metadata.
    directory = tmp_path / identifier
    meta = json.loads((directory / ".web.json").read_text())
    meta["stop_kind"] = "duration"
    write_metadata(directory, meta)
    async with browser(tmp_path) as api:
        assert (await api.get(f"/api/runs/{identifier}")).json()["stop_kind"] == "user"


async def test_live_cli_journal_events_and_list_keep_cause_without_stop_events(tmp_path):
    import os
    import socket
    from datetime import datetime, timezone
    from giraffe.evidence import EvidenceWriter
    from giraffe.storage import write_metadata

    directory = tmp_path / "cli"
    writer = EvidenceWriter(directory, manifest={"config": config().model_dump(mode="json")})
    writer.record_stop_cause({"kind": "user", "text": "cancelled by user", "scope": "gateway"})
    writer.close()
    write_metadata(directory, {"id": "cli", "state": "running", "owner_pid": os.getpid(),
                              "owner_host": socket.gethostname(), "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    async with browser(tmp_path) as api:
        detail = (await api.get("/api/runs/cli")).json()
        row = (await api.get("/api/runs")).json()["runs"][0]
        feed = (await api.get("/api/runs/cli/events?after=0")).json()
        reset = (await api.get("/api/runs/cli/events?after=999999999999999999")).json()
        assert feed["events"] == reset["events"] == []
        assert reset["reset"]
        for surface in (detail, row, feed, reset):
            assert surface["stop_kind"] == "user"


@pytest.mark.parametrize("kind,exit_code", [(None, 0), ("request_budget", 2), ("user", 2)])
def test_cli_final_json_matches_report_and_metadata(tmp_path, monkeypatch, capsys, kind, exit_code):
    from giraffe import cli, reporting
    suite, write = runner.run_suite, reporting.write_report

    async def run(config, *, progress, stop_event, **kwargs):
        def update(event):
            progress(event)
            if kind == "user" and event["event"] == "request_completed":
                stop_event.set()
        return await suite(config, progress=update, stop_event=stop_event, **kwargs)

    monkeypatch.setattr(runner, "run_suite", run)
    monkeypatch.setattr(reporting, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))
    source, directory = tmp_path / "config.json", tmp_path / "run"
    source.write_text(config(max_requests=1 if kind == "request_budget" else None).model_dump_json())
    code = cli.main(["run", "--config", str(source), "--skip-preflight", "--output-format", "json",
                     "--output", str(directory), "--min-free-disk", "0", "--max-total-storage", "0"])
    captured = capsys.readouterr()
    final = json.loads(captured.out.splitlines()[-1])
    saved = json.loads((directory / "report.json").read_text())
    meta = json.loads((directory / ".web.json").read_text())
    assert code == final["exit_code"] == exit_code
    assert final["stop_kind"] == runner.stop_kind_of(saved) == meta["stop_kind"] == kind
    assert runner.stop_kind_of(recover_evidence(directory, meta)) == kind
    progress = [json.loads(line) for line in captured.err.splitlines() if line.startswith('{"event"')]
    stops = [event for event in progress if event["event"] in {"run_stopping", "run_finished"}]
    assert stops and stops[-1]["stop_kind"] == kind
    assert all("stop_kind" in event and "abort_reason" in event for event in stops)


async def test_cancellation_fallback_is_journaled_before_export(tmp_path, monkeypatch):
    from tests.test_web import report
    write = web.write_report

    async def cancelled(config, *, progress, stop_event):
        error = asyncio.CancelledError()
        error.giraffe_report = report(config)
        raise error

    monkeypatch.setattr(web, "run_suite", cancelled)
    monkeypatch.setattr(web, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))
    async with browser(tmp_path) as api:
        identifier = await start(api, config().model_dump(mode="json"))
        detail, _ = await assert_stop_contract(api, tmp_path, identifier, "cancelled")
        assert detail["abort_reason"] == "run cancelled"


def test_report_command_recovery_metadata_uses_journal_kind_and_recovery_prose(tmp_path):
    from giraffe import cli
    from giraffe.evidence import EvidenceWriter
    from giraffe.storage import write_metadata

    directory = tmp_path / "interrupted"
    writer = EvidenceWriter(directory, manifest={"config": config().model_dump(mode="json")})
    writer.record_stop_cause({"kind": "user", "text": "cancelled by user", "scope": "gateway"})
    writer.close()
    write_metadata(directory, {"id": directory.name, "state": "interrupted",
                              "heartbeat_at": "1970-01-01T00:00:00+00:00"})
    recovered = cli._report_for_render(directory)
    meta = json.loads((directory / ".web.json").read_text())
    assert meta["stop_kind"] == runner.stop_kind_of(recovered) == "user"
    assert meta["abort_reason"] == recovered.abort_reason == "Giraffe stopped before this run finished; 0 requests recorded."
    assert runner.stop_kind_of(recover_evidence(directory, meta)) == "user"


def test_cli_summary_only_failure_preserves_stop_kind(tmp_path, monkeypatch, capsys):
    from giraffe import cli, reporting
    suite = runner.run_suite

    async def stopped(config, *, progress, stop_event, **kwargs):
        def update(event):
            progress(event)
            if event["event"] == "request_completed":
                stop_event.set()
        return await suite(config, progress=update, stop_event=stop_event, **kwargs)

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(runner, "run_suite", stopped)
    monkeypatch.setattr(reporting, "write_report", fail)
    source, directory = tmp_path / "config.json", tmp_path / "run"
    source.write_text(config(samples=20, concurrency=1, max_requests=None).model_dump_json())
    code = cli.main(["run", "--config", str(source), "--skip-preflight", "--json", "--output", str(directory),
                     "--min-free-disk", "0", "--max-total-storage", "0"])
    final = json.loads(capsys.readouterr().out.splitlines()[-1])
    summary = json.loads((directory / "summary.json").read_text())
    meta = json.loads((directory / ".web.json").read_text())
    assert code == final["exit_code"] == 4
    assert final["stop_kind"] == summary["stop_kind"] == meta["stop_kind"] == "user"
    assert final["abort_reason"] == summary["abort_reason"] == meta["abort_reason"] == "cancelled by user"
    assert not (directory / "report.json").exists()
