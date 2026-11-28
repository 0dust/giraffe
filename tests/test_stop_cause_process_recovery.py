"""Process death preserves the last journaled cause without a network endpoint."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from giraffe import runner, storage, web
from giraffe.evidence import recover_evidence
from tests.test_runner import config
from tests.test_web import browser


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires SIGKILL")
async def test_sigkilled_writer_server_recovers_last_cause(tmp_path, monkeypatch):
    source, directory = tmp_path / "config.json", tmp_path / "killed"
    source.write_text(config(samples=20, concurrency=1, max_requests=None).model_dump_json())
    child = r'''
import sys
import threading
from pathlib import Path
from pytest import MonkeyPatch
from giraffe import cli, runner
from tests.conftest import local_tokenizer
from tests.test_runner import client
patch = MonkeyPatch()
local_tokenizer.__wrapped__(patch, Path(sys.argv[2]))
client.__wrapped__(patch, None)
suite = runner.run_suite
async def run(config, *, progress, stop_event, **kwargs):
    def update(event):
        progress(event)
        if event["event"] == "request_completed":
            stop_event.set()
        elif event["event"] == "run_stopping":
            # The real runner has latched and sent its cause to EvidenceWriter.
            # Hold report construction while the journal's own thread flushes.
            threading.Event().wait()
    return await suite(config, progress=update, stop_event=stop_event, **kwargs)
runner.run_suite = run
raise SystemExit(cli.main(["run", "--config", sys.argv[1], "--output", sys.argv[2],
    "--skip-preflight", "--json", "--min-free-disk", "0", "--max-total-storage", "0"]))
'''
    log_path = tmp_path / "child.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, "-c", child, str(source), str(directory)],
                                   cwd=Path(__file__).parents[1], env=os.environ.copy(),
                                   stdout=log, stderr=log)
        try:
            recovered = None
            for _ in range(500):
                recovered = recover_evidence(directory, {})
                if recovered and recovered.requests and runner.stop_kind_of(recovered) == "user":
                    break
                assert process.poll() is None, log_path.read_text()
                await asyncio.sleep(.02)
            assert recovered and recovered.requests, log_path.read_text()
            assert runner.stop_kind_of(recovered) == "user"
            journal_cause = recovered.observations["stop_cause"]
            process.kill()
            assert await asyncio.to_thread(process.wait, 3) == -signal.SIGKILL
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 3)
    assert not (directory / "report.json").exists()
    # Shorten only the heartbeat grace; ownership still probes the real dead PID.
    monkeypatch.setattr(storage, "HEARTBEAT_STALE_SECONDS", 0)
    write = web.write_report
    monkeypatch.setattr(web, "write_report", lambda *args, **kwargs: write(
        *args, **{**kwargs, "preserve_evidence": True}))
    async with browser(tmp_path) as api:
        detail = (await api.get("/api/runs/killed")).json()
        row = next(row for row in (await api.get("/api/runs")).json()["runs"] if row["id"] == "killed")
        reset = (await api.get("/api/runs/killed/events")).json()
        assert detail["state"] == "interrupted" and detail["recoverable"] is False
        assert detail["report"]["observations"]["stop_cause"] == journal_cause
        assert detail["stop_kind"] == row["stop_kind"] == reset["stop_kind"] == "user"
        assert reset["reset"] and reset["events"] == []
        meta = json.loads((directory / ".web.json").read_text())
        assert meta["stop_kind"] == "user"
        assert recover_evidence(directory, meta).observations["stop_cause"] == journal_cause
