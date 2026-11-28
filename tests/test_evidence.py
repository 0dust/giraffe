from concurrent.futures import ThreadPoolExecutor
import json
import errno
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from giraffe.evidence import EvidenceWriter, recover_evidence
from giraffe.models import RequestRecord, RunReport
from giraffe.reporting import _atomic_write, save_baseline_report, write_report
from giraffe.storage import Heartbeat, run_alive, update_metadata, write_metadata


def record(index=0, **updates):
    return RequestRecord(id=f"request-{index}", target="local", fixture_id="fixture", scenario="warm",
        check_ids=["correctness"], started_at="2026-10-27T10:00:00Z", status="completed",
        output="sensitive answer", valid=True, **updates)


def test_recover_finished_records_before_runner_finalizes(tmp_path):
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "all"}}, run_id="original")
    for index in range(7):
        writer.record_finished(record(index))
    writer.progress({"event": "condition_finished", "condition_summary": {
        "condition_id": "warm", "target": "local", "status": "finished"}})
    writer.close()  # Simulate the process disappearing before report finalization.
    with (tmp_path / "evidence.jsonl").open("a") as stream:
        stream.write('{"type":"record",')
    report = recover_evidence(tmp_path, {})
    assert report.run_id == "original"
    assert len(report.requests) == 7
    assert report.requests[0].output == "sensitive answer"
    assert report.checks[0].status == "inconclusive"
    assert report.checks[0].summary == "Giraffe stopped before this run finished; 7 requests recorded."
    assert report.conditions[0]["status"] == "inconclusive"
    assert report.conditions[0]["lifecycle_status"] == "interrupted"
    assert report.observations["judge_scores_missing"]
    assert (tmp_path / "evidence.jsonl").exists()
    write_report(report, tmp_path)
    assert not (tmp_path / "evidence.jsonl").exists()
    assert len(RunReport.model_validate_json((tmp_path / "report.json").read_text()).requests) == 7


@pytest.mark.parametrize("retention,expected", [("all", "sensitive answer"), ("none", ""), ("failures", "")])
def test_evidence_applies_retention_and_snapshots_pre_judging_record(tmp_path, retention, expected):
    writer = EvidenceWriter(tmp_path, {"config": {"retention": retention}})
    finished = record().model_copy(update={"refused": True})
    writer.record_finished(finished)
    finished.score = False
    finished.output = "changed by judging"
    writer.close()
    report = recover_evidence(tmp_path, {})
    assert report.requests[0].refused is True
    assert report.requests[0].score is None
    assert report.requests[0].output == expected
    assert (tmp_path / "evidence.jsonl").stat().st_mode & 0o777 == 0o600
    saved, _ = write_report(report, tmp_path)
    assert RunReport.model_validate_json(saved.read_bytes()).requests[0].refused is True


def test_evidence_fsync_is_on_writer_thread_and_happens_without_close(tmp_path, monkeypatch):
    real_fsync = os.fsync
    seen = []
    durable = threading.Event()
    def sync(fd):
        seen.append(threading.current_thread().name)
        real_fsync(fd)
        if len(seen) >= 3:
            durable.set()
    monkeypatch.setattr("giraffe.evidence.os.fsync", sync)
    writer = EvidenceWriter(tmp_path, fsync_records=2, fsync_seconds=0.03)
    writer.record_finished(record())
    assert durable.wait(2), "time based sync did not persist the pending record"
    writer.close()
    assert all(name == "giraffe-evidence" for name in seen)


def test_evidence_count_threshold_syncs_before_timer(tmp_path, monkeypatch):
    real_fsync = os.fsync
    calls = []
    durable = threading.Event()
    def sync(fd):
        calls.append(fd)
        real_fsync(fd)
        if len(calls) >= 3:
            durable.set()
    monkeypatch.setattr("giraffe.evidence.os.fsync", sync)
    writer = EvidenceWriter(tmp_path, fsync_records=2, fsync_seconds=60)
    writer.record_finished(record(0))
    writer.record_finished(record(1))
    assert durable.wait(2), "record threshold did not trigger a sync"
    writer.close()


def test_unique_tempfiles_private_baseline_and_durable_publication(tmp_path, monkeypatch):
    from giraffe import reporting, storage
    names, syncs = [], []
    real_replace = Path.replace
    real_fsync = os.fsync
    def replace(path, target):
        names.append(path.name)
        assert path.name != Path(target).name + ".tmp"
        return real_replace(path, target)
    def sync(fd):
        syncs.append(fd)
        real_fsync(fd)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(storage.os, "fsync", sync)
    write_metadata(tmp_path, {"state": "running"})
    update_metadata(tmp_path, {"state": "completed"})
    _atomic_write(tmp_path / "report.json", "{}")
    _atomic_write(tmp_path / "report.json", "{}")
    assert len(names) == len(set(names))
    assert len(syncs) >= 8  # Every publication syncs both the file and directory.
    assert (tmp_path / ".web.json").stat().st_mode & 0o777 == 0o600
    source = tmp_path / "source.json"
    report = RunReport(run_id="private", started_at="now", finished_at="now", overall="inconclusive",
                       manifest={"config": {"retention": "all"}}, requests=[record()])
    source.write_text(report.model_dump_json())
    os.chmod(source, 0o644)
    destination = tmp_path / ".baselines" / "chosen" / "report.json"
    reporting.save_baseline_report(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert source.stat().st_mode & 0o777 == 0o644
    assert destination.stat().st_ino != source.stat().st_ino
    portable = tmp_path / "portable.json"
    save_baseline_report(source, portable, portable=True)
    assert portable.stat().st_mode & 0o777 == 0o644


def test_failed_report_publication_preserves_evidence(tmp_path, monkeypatch):
    writer = EvidenceWriter(tmp_path)
    writer.record_finished(record())
    writer.close()
    report = recover_evidence(tmp_path, {})
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr("giraffe.reporting._atomic_write_chunks", fail)
    with pytest.raises(OSError):
        write_report(report, tmp_path)
    assert (tmp_path / "evidence.jsonl").exists()


def test_heartbeat_thread_updates_and_cannot_resurrect_completed_state(tmp_path):
    write_metadata(tmp_path, {"state": "running"})
    heartbeat = Heartbeat(tmp_path, interval=0.02).start()
    deadline = time.monotonic() + 2
    while "heartbeat_at" not in json.loads((tmp_path / ".web.json").read_text()):
        assert time.monotonic() < deadline
        time.sleep(0.005)
    update_metadata(tmp_path, {"state": "completed"})
    time.sleep(0.06)
    heartbeat.close()
    assert json.loads((tmp_path / ".web.json").read_text())["state"] == "completed"


def test_liveness_heartbeat_first_and_stale_requires_dead_or_foreign(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    base = {"state": "running", "owner_pid": os.getpid() + 1, "owner_host": socket.gethostname()}
    fresh = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr("giraffe.storage.os.kill", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    assert run_alive(tmp_path, base | {"heartbeat_at": fresh})
    assert not run_alive(tmp_path, base | {"heartbeat_at": "2000-01-01T00:00:00Z"})
    monkeypatch.setattr("giraffe.storage.os.kill", lambda *args: None)
    assert run_alive(tmp_path, base | {"heartbeat_at": "2000-01-01T00:00:00Z"})
    assert not run_alive(tmp_path, base | {"owner_host": "another-host", "heartbeat_at": "2000-01-01T00:00:00Z"})
    assert not run_alive(tmp_path, base | {"owner_pid": os.getpid(), "heartbeat_at": "2000-01-01T00:00:00Z"})


@pytest.mark.parametrize("worker", ["evidence", "heartbeat"])
def test_background_persistence_failures_retain_run_log_context(tmp_path, monkeypatch, worker):
    from giraffe.diagnostics import run_logging

    write_metadata(tmp_path, {"state": "running"})
    reached = threading.Event()
    def fail(*args, **kwargs):
        reached.set()
        raise RuntimeError("background persistence fault")
    with run_logging(tmp_path):
        if worker == "evidence":
            monkeypatch.setattr(EvidenceWriter, "_retained", fail)
            writer = EvidenceWriter(tmp_path)
            with pytest.raises(RuntimeError, match="background persistence fault"):
                writer.close()
        else:
            monkeypatch.setattr("giraffe.storage._write_metadata", fail)
            heartbeat = Heartbeat(tmp_path).start()
            assert reached.wait(2)
            heartbeat.close()
    log = (tmp_path / "giraffe.log").read_text()
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError: background persistence fault" in log


def test_journal_cleanup_failure_never_removes_durable_report(tmp_path, monkeypatch):
    from giraffe import reporting
    writer = EvidenceWriter(tmp_path)
    writer.record_finished(record())
    writer.close()
    recovered = recover_evidence(tmp_path, {})
    sync = reporting.fsync_directory

    def fail_after_unlink(directory):
        if not (tmp_path / "evidence.jsonl").exists():
            raise OSError("post-report directory fsync failure")
        sync(directory)

    monkeypatch.setattr(reporting, "fsync_directory", fail_after_unlink)
    with pytest.raises(reporting.ReportWriteError) as failure:
        write_report(recovered, tmp_path)
    assert failure.value.json_complete
    reporting.save_failure_summary(recovered, tmp_path, failure.value)
    assert len(RunReport.model_validate_json((tmp_path / "report.json").read_text()).requests) == 1


def test_evidence_status_lines_apply_none_retention_to_check_excerpts(tmp_path):
    message = 'Mismatch. Expected: "private expected"; received: "private answer".'
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "none"}})
    writer.progress({"event": "check_finished", "check": {
        "id": "correctness", "target": "local", "title": "Correctness", "status": "fail",
        "summary": message, "metrics": {"scenarios": {"warm": {"summary": message}}}}})
    writer.close()
    contents = (tmp_path / "evidence.jsonl").read_text()
    assert "private expected" not in contents and "private answer" not in contents


@pytest.mark.parametrize("retention", ["all", "none"])
def test_evidence_replaces_non_finite_values_and_flags_the_line(tmp_path, retention):
    writer = EvidenceWriter(tmp_path, {"config": {"retention": retention}})
    writer.record_finished(record(workload={"nan": float("nan"), "infinity": float("inf")},
                                  response_events=[{"negative": -float("inf")}]))
    writer.progress({"event": "check_finished", "check": {
        "id": "correctness", "target": "local", "title": "Correctness", "status": "pass",
        "summary": "finished", "metrics": {"endpoint_value": float("nan")}}})
    writer.close()
    contents = (tmp_path / "evidence.jsonl").read_text()
    assert "NaN" not in contents and "Infinity" not in contents
    lines = [json.loads(line) for line in contents.splitlines()]
    row = next(line for line in lines if line["type"] == "record")
    assert row["non_finite_values"] is True
    assert row["record"]["workload"] == {"nan": None, "infinity": None}
    assert row["record"]["response_events"] == [{"negative": None}]
    assert next(line for line in lines if line["type"] == "progress")["non_finite_values"]
    assert len(recover_evidence(tmp_path, {}).requests) == 1


@pytest.mark.parametrize("failure", ["disk_full", "retention"])
def test_failed_writer_drops_further_evidence_without_queueing(tmp_path, monkeypatch, failure):
    failed = threading.Event()

    def fail(*args, **kwargs):
        failed.set()
        if failure == "disk_full":
            raise OSError(errno.ENOSPC, "disk full")
        raise ValueError("retention failed")

    if failure == "disk_full":
        monkeypatch.setattr("giraffe.evidence.os.open", fail)
    else:
        monkeypatch.setattr(EvidenceWriter, "_retained", fail)
    writer = EvidenceWriter(tmp_path, queue_size=2)
    assert failed.wait(2)
    writer._thread.join(timeout=2)
    assert not writer._closed
    for index in range(100):
        writer.record_finished(record(index))
    writer.progress({"event": "check_finished", "check": {"status": "pass"}})
    assert writer.stopped
    assert writer._queue.qsize() == 0
    assert writer.dropped_count == 101
    with pytest.raises((OSError, ValueError)):
        writer.close()


def test_full_evidence_queue_never_blocks_run_and_close_is_bounded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    retained = EvidenceWriter._retained

    def held(self, *args, **kwargs):
        entered.set()
        release.wait()
        return retained(self, *args, **kwargs)

    monkeypatch.setattr(EvidenceWriter, "_retained", held)
    writer = EvidenceWriter(tmp_path, queue_size=2, close_seconds=0.03)
    try:
        assert entered.wait(2)
        for index in range(10):
            writer.record_finished(record(index))
        assert writer._queue.qsize() == 2
        assert writer.dropped_count == 8
        def close():
            with pytest.raises(TimeoutError, match="Evidence writer did not stop"):
                writer.close()
        with ThreadPoolExecutor(1) as pool:
            try:
                pool.submit(close).result(1)
                assert not writer.stopped
            finally:
                release.set()
    finally:
        release.set()
        writer.close_seconds = 2
        writer.close()
    assert writer.stopped
    recovered = recover_evidence(tmp_path, {})
    assert len(recovered.requests) == 2
    assert recovered.observations["evidence_dropped_count"] == 8
    assert "8 evidence records not written to evidence.jsonl" in recovered.warnings


def test_report_keeps_final_summary_and_optionally_open_evidence(tmp_path):
    summary = {"overall": "fail", "checks": [{"id": "correctness", "status": "fail"}]}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    (tmp_path / "evidence.jsonl").write_text("unfinished writer\n")
    report = RunReport(run_id="finished", started_at="now", finished_at="now", overall="fail", manifest={})
    write_report(report, tmp_path, preserve_evidence=True)
    assert json.loads((tmp_path / "summary.json").read_text()) == summary
    assert (tmp_path / "evidence.jsonl").exists()
    write_report(report, tmp_path)
    assert not (tmp_path / "evidence.jsonl").exists()
    assert json.loads((tmp_path / "summary.json").read_text()) == summary


def test_legacy_mtime_only_runs_keep_two_minute_liveness_grace(tmp_path):
    write_metadata(tmp_path, {"state": "running", "owner_host": "another-host"})
    marker = tmp_path / ".web.json"
    now = time.time()
    os.utime(marker, (now - 90, now - 90))
    assert run_alive(tmp_path)
    os.utime(marker, (now - 121, now - 121))
    assert not run_alive(tmp_path)


def test_heartbeat_close_is_bounded_when_metadata_write_stalls(tmp_path, monkeypatch, caplog):
    write_metadata(tmp_path, {"state": "running"})
    entered, release = threading.Event(), threading.Event()

    def held(*args):
        entered.set()
        release.wait()

    monkeypatch.setattr("giraffe.storage._write_metadata", held)
    monkeypatch.setattr("giraffe.storage.HEARTBEAT_CLOSE_SECONDS", 0.03)
    heartbeat = Heartbeat(tmp_path).start()
    try:
        assert entered.wait(2)
        with ThreadPoolExecutor(1) as pool:
            try:
                pool.submit(heartbeat.close).result(1)
                assert "Heartbeat did not stop" in caplog.text
            finally:
                release.set()
    finally:
        release.set()
        heartbeat._thread.join(timeout=2)
    assert not heartbeat._thread.is_alive()


def test_recovered_timeout_warning_uses_latest_update_once(tmp_path):
    from giraffe.timeout_warning import format_timeout_warning

    payload = {"key": "local|2.0|run", "target": "local", "deadline_seconds": 2,
        "deadline_source": "run", "setting": "request_timeout_seconds", "cli_flag": "--request-timeout",
        "finished": 3, "timed_out": 3, "answer_started": 0, "reasoning_only": 0, "no_output": 3,
        "checks": ["correctness"], "first_at_seconds": 2, "updated_at_seconds": 2, "update": 1}
    latest = {**payload, "finished": 10, "timed_out": 6, "no_output": 6, "updated_at_seconds": 4, "update": 2}
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "all"}})
    writer.record_finished(record())
    for warning in (payload, latest, latest):
        writer.progress({"event": "timeout_warning", "target": "local", "timeout_warning": warning})
    writer.close()
    recovered = recover_evidence(tmp_path, {})
    assert len(recovered.requests) == 1
    assert recovered.observations["targets"]["local"]["timeout_warnings"] == [latest]
    assert recovered.warnings.count(format_timeout_warning(latest)) == 1
    assert format_timeout_warning(payload) not in recovered.warnings


def test_recovery_uses_one_snapshot_including_inflight_requests(tmp_path):
    from giraffe.runner import _Run
    from giraffe.models import RunConfig, Target

    config = RunConfig(targets=[Target(name=name, model="m", url=f"http://{name}/v1")
                               for name in ("local", "other")], checks=["access"])
    run = _Run(config, None, None)
    writer = EvidenceWriter(tmp_path, {"config": {"retention": "all"}})
    writer.request_counts_start({"local": 1})
    run.note_sent("local", "preflight")
    run.note_sent("local", "restart_readiness")
    readiness = record(1).model_copy(update={"scenario": "readiness", "sent": True})
    readiness._budget_exempt = True
    readiness._budget_exemption = "restart_readiness"
    writer.record_finished(readiness)
    for target in ("local", "other"):
        assert run.reserve_offer(target)
        run.note_sent(target)  # Neither request has finished when the process dies.
    assert run.reserve_offer("other")  # Charged, not yet sent.
    writer.request_counts_snapshot(run.request_counts())
    writer.close()
    journal = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert journal[1] == {"type": "request_counts_start", "preflight": {"local": 1}}
    assert len([row for row in journal if row["type"] == "record"]) == 1
    report = recover_evidence(tmp_path, {})
    counts = report.observations["request_counts"]
    assert counts["available"] is True and counts["complete"] is False
    assert counts["sent"] == 4 and counts["budget_counted"] == 3
    assert counts["budget_exempt"] == 2 and counts["budget_counted_unsent"] == 1
    assert counts["saved_observations"] == 0
    assert counts["budget_exempt_by_reason"] == {"preflight": 1, "restart_readiness": 1}
    for row in [counts, *counts["targets"].values()]:
        assert row["sent"] == row["budget_counted"] - row["budget_counted_unsent"] + row["budget_exempt"]
    for key in ("sent", "budget_counted", "budget_counted_unsent", "budget_exempt", "saved_observations"):
        assert counts[key] == sum(value[key] for value in counts["targets"].values())


def test_recovery_without_snapshot_does_not_invent_traffic_counts(tmp_path):
    writer = EvidenceWriter(tmp_path)
    writer.request_counts_start({"local": 1})
    writer.record_finished(record().model_copy(update={"sent": True}))
    writer.progress({"event": "check_finished", "budget_used": 3,
                     "request_counts": {"sent": 2}})
    writer.close()
    counts = recover_evidence(tmp_path, {}).observations["request_counts"]
    assert counts == {"complete": False, "available": False, "saved_observations": 1}
    journal = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert all("request_counts" not in row.get("progress", {}) for row in journal)


def test_request_count_snapshots_coalesce_outside_the_bounded_queue(tmp_path, monkeypatch):
    release = threading.Event()
    original = EvidenceWriter._write

    def held_write(self):
        release.wait(5)
        original(self)

    monkeypatch.setattr(EvidenceWriter, "_write", held_write)
    writer = EvidenceWriter(tmp_path, queue_size=1000, fsync_records=1000, fsync_seconds=60)
    try:
        for index in range(100):
            writer.record_finished(record(index))
            for update in range(20):
                writer.request_counts_snapshot({"sent": index + 1, "update": update})
            # Ordinary live events must not consume the evidence queue.
            writer.progress({"event": "request_completed", "budget_used": index + 1,
                             "request_counts": {"sent": index + 1}})
    finally:
        release.set()
        writer.close()
    journal = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    snapshots = [row for row in journal if row["type"] == "request_counts"]
    records = [row for row in journal if row["type"] == "record"]
    assert len(records) == 100 and 0 < len(snapshots) < len(records) / 10
    assert snapshots[-1]["sent"] == 100 and snapshots[-1]["update"] == 19
    assert writer.dropped_count == 0


def test_recovery_adds_only_preflight_not_in_the_snapshot(tmp_path):
    from giraffe.runner import _Run
    from giraffe.models import RunConfig, Target

    run = _Run(RunConfig(targets=[Target(name="local", model="m", url="http://local/v1")],
                         checks=["access"]), None, None)
    writer = EvidenceWriter(tmp_path)
    writer.request_counts_start({"local": 1})
    writer.request_counts_snapshot(run.request_counts())
    writer.close()
    counts = recover_evidence(tmp_path, {}).observations["request_counts"]
    assert counts["sent"] == counts["budget_exempt"] == 1
    assert counts["targets"]["local"]["budget_exempt_by_reason"] == {"preflight": 1}


def test_request_counts_flush_without_completed_records(tmp_path, monkeypatch):
    durable = threading.Event()
    real_fsync = os.fsync

    def sync(descriptor):
        real_fsync(descriptor)
        path = tmp_path / "evidence.jsonl"
        if path.exists() and '"type":"request_counts"' in path.read_text().replace(" ", ""):
            durable.set()

    monkeypatch.setattr(os, "fsync", sync)
    writer = EvidenceWriter(tmp_path, fsync_seconds=0.01)
    try:
        writer.request_counts_snapshot({"sent": 1})
        assert durable.wait(2), "in-flight traffic snapshot was not flushed"
        assert writer.dropped_count == 0
    finally:
        writer.close()


def test_each_record_batch_ends_with_fresh_counts_and_recovers_before_close(tmp_path, monkeypatch):
    from collections import Counter
    from giraffe.models import RunConfig, Target
    from giraffe.runner import _Run

    writes = []
    real_fdopen = os.fdopen
    real_fsync = os.fsync
    initial_durable, records_durable = threading.Event(), threading.Event()

    class CapturedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, text):
            writes.append(text)
            return self.stream.write(text)

    def sync(descriptor):
        real_fsync(descriptor)
        rows = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
        if any(row["type"] == "request_counts" for row in rows):
            initial_durable.set()
        if sum(row["type"] == "record" for row in rows) == 3:
            records_durable.set()

    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: CapturedStream(real_fdopen(*args, **kwargs)))
    monkeypatch.setattr(os, "fsync", sync)
    config = RunConfig(targets=[Target(name=name, model="m", url=f"http://{name}/v1")
                               for name in ("local", "other")], checks=["access"])
    run = _Run(config, None, None)
    writer = EvidenceWriter(tmp_path, fsync_records=1, fsync_seconds=0.01)
    run.request_counts_sink = writer.request_counts_snapshot
    try:
        run.note_sent("local", "preflight")
        assert initial_durable.wait(2), "old snapshot was not flushed before record traffic"
        for index, target in enumerate(("local", "other", "local")):
            assert run.reserve_offer(target)
            run.note_sent(target)
            writer.record_finished(record(index).model_copy(update={"target": target, "sent": True}))
        assert records_durable.wait(2)
        # Read the durable journal before normal close/final report, as after a kill.
        recovered = recover_evidence(tmp_path, {})
        counts = recovered.observations["request_counts"]
        assert counts["complete"] is False and counts["available"] is True
        assert counts["saved_observations"] == 3
        assert counts["saved_observations"] <= counts["sent"] - counts["budget_exempt"]
        assert writer.dropped_count == 0
    finally:
        writer.close()
    sent = Counter()
    batches = 0
    for text in writes:
        rows = [json.loads(line) for line in text.splitlines()]
        records = [row for row in rows if row["type"] == "record"]
        if not records:
            continue
        batches += 1
        for row in records:
            sent[row["record"]["target"]] += row["sent"]
        assert rows[-1]["type"] == "request_counts"
        assert sum(row["type"] == "request_counts" for row in rows) == 1
        for target, total in sent.items():
            assert rows[-1]["targets"][target]["sent"] >= total
    assert batches == 3


def test_recovery_does_not_fabricate_missing_target_counters():
    from giraffe.evidence import _recovered_request_counts
    from giraffe.models import RunConfig, Target
    from giraffe.runner import _Run
    run = _Run(RunConfig(targets=[Target(name="local", model="m", url="http://local/v1")],
                         checks=["access"]), None, None)
    saved = record().model_copy(update={"target": "missing"})
    counts = _recovered_request_counts([saved], {"missing": 1}, run.request_counts())
    assert counts["saved_observations"] == 1
    assert "missing" not in counts["targets"]
    assert counts["targets"]["local"]["saved_observations"] == 0
    assert counts["sent"] == counts["budget_exempt"] == 1
    assert counts["budget_exempt_by_reason"] == {"preflight": 1}


@pytest.mark.parametrize("snapshot", [{"sent": 3}, {"available": True, "complete": True,
    "sent": "3"}, {"available": True, "complete": True, "sent": True}])
def test_recovery_rejects_partial_or_invalid_snapshots(snapshot):
    from giraffe.evidence import _recovered_request_counts
    assert _recovered_request_counts([record()], {}, snapshot) == {
        "available": False, "complete": False, "saved_observations": 1}
