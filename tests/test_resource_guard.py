"""Run the real isolation fixtures in child pytest sessions, including leak controls."""

import json
import os
from pathlib import Path
import subprocess
import sys

import giraffe
import pytest


PROBE = r'''
import asyncio
import json
from pathlib import Path
import threading

import anyio
from anyio._backends._asyncio import WorkerThread
from giraffe import runner
import pytest


MODE = MODE_VALUE
release = threading.Event()
queues_drained = asyncio.Event()
drained_workers = set()
workers, ordinary, joins = [], [], []
original_run, original_stop = WorkerThread.run, WorkerThread.stop
original_join = threading.Thread.join


@pytest.fixture(scope="session", autouse=True)
def controlled_worker_exit():
    def run(worker):
        workers.append(worker)
        task_done = worker.queue.task_done
        if MODE == "result_tail":
            def held_task_done():
                release.wait(5)
                task_done()
            worker.queue.task_done = held_task_done
        elif MODE == "pruned":
            first_job = True
            def report_drained():
                drained_workers.add(worker)
                if len(drained_workers) == 2:
                    queues_drained.set()
            def observed_task_done():
                nonlocal first_job
                task_done()
                if first_job:
                    first_job = False
                    worker.loop.call_soon_threadsafe(report_drained)
            worker.queue.task_done = observed_task_done
        original_run(worker)
        if MODE in {"normal", "stuck", "mixed", "busy", "pruned"}:
            # Hold a worker after it consumed AnyIO's actual stop sentinel.
            # Normal shutdown is released by the observer's join, not a timer.
            release.wait(5)

    def join(worker, timeout=None):
        joins.append({"timeout": timeout, "stopping": worker.stopping,
                      "loop_closed": worker.loop.is_closed()})
        if MODE in {"normal", "mixed", "busy", "pruned", "result_tail"}:
            release.set()
        if MODE in {"mixed", "busy"}:
            # Force the real leak to end during this eligible worker's join.
            # A guard that only scans afterwards must miss it deterministically.
            for other in [*workers, *ordinary]:
                if other is not worker:
                    original_join(other, 2)
                    assert not other.is_alive()
        return original_join(worker, timeout)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(WorkerThread, "run", run)
        patch.setattr(WorkerThread, "join", join)
        if MODE == "pruned":
            patch.setattr(WorkerThread, "MAX_IDLE_TIME", 0)
        if MODE == "unstopped":
            patch.setattr(WorkerThread, "stop", lambda worker, *_: None)
        try:
            yield
        finally:
            snapshot = {"joins": joins, "alive_before_cleanup": sum(w.is_alive() for w in workers),
                        "worker_count": len(workers),
                        "ordinary_alive_before_cleanup": sum(t.is_alive() for t in ordinary)}
            release.set()
            for worker in workers:
                if not worker.stopping:
                    original_stop(worker)
                original_join(worker, 2)
                assert not worker.is_alive()
            for thread in ordinary:
                original_join(thread, 2)
                assert not thread.is_alive()
            Path("probe.json").write_text(json.dumps(snapshot))


async def test_probe():
    if MODE in {"mixed", "thread"}:
        thread = threading.Thread(target=release.wait, name="giraffe-leaked-probe")
        ordinary.append(thread)
        thread.start()
    if MODE == "busy":
        loop, started = asyncio.get_running_loop(), asyncio.Event()
        def busy_call():
            loop.call_soon_threadsafe(started.set)
            release.wait(5)
        namespace = {"__name__": "giraffe.guard_probe", "anyio": anyio, "busy_call": busy_call}
        exec("async def invoke_busy():\n    return await anyio.to_thread.run_sync(busy_call, abandon_on_cancel=True)\n", namespace)
        task = asyncio.create_task(namespace["invoke_busy"]())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    if MODE in {"normal", "stuck", "unstopped", "mixed", "busy", "pruned", "result_tail"}:
        count = 3 if MODE == "stuck" else 2 if MODE == "pruned" else 1
        barrier = threading.Barrier(count, timeout=3)
        namespace = {"__name__": "giraffe.guard_probe", "anyio": anyio, "barrier": barrier}
        exec("async def invoke():\n    return await anyio.to_thread.run_sync(barrier.wait)\n", namespace)
        await asyncio.gather(*(namespace["invoke"]() for _ in range(count)))
        if MODE == "pruned":
            # Make both workers idle with fully drained queues, then exercise
            # AnyIO's real idle pruning before the test runner stops its loop.
            await queues_drained.wait()
            namespace["barrier"] = threading.Barrier(1, timeout=3)
            await namespace["invoke"]()
    elif MODE == "task":
        asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
    elif MODE == "state":
        runner.LLMClient = object()
'''


def run_guard_probe(tmp_path, mode):
    (tmp_path / "conftest.py").write_text(Path(__file__).with_name("conftest.py").read_text())
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "test_probe.py").write_text(PROBE.replace("MODE_VALUE", repr(mode)))
    environment = dict(os.environ)
    # Child tests change cwd; bind their imports to this test session's checkout.
    environment["PYTHONPATH"] = str(Path(giraffe.__file__).resolve().parent.parent)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", "test_probe.py"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=15)
    return result, json.loads((tmp_path / "probe.json").read_text())


@pytest.mark.parametrize("mode", ["normal", "pruned", "result_tail"])
def test_resource_guard_waits_for_requested_anyio_shutdown_after_loop_close(mode, tmp_path):
    result, probe = run_guard_probe(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert probe["worker_count"] == (2 if mode == "pruned" else 1)
    assert probe["alive_before_cleanup"] == 0
    assert len(probe["joins"]) == probe["worker_count"]
    for join in probe["joins"]:
        assert join["stopping"] and join["loop_closed"]
        assert 0 < join["timeout"] <= 1


def test_resource_guard_still_fails_stuck_workers_with_one_shared_wait_budget(tmp_path):
    result, probe = run_guard_probe(tmp_path, "stuck")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Giraffe threads left running: AnyIO worker thread" in result.stdout
    assert probe["worker_count"] == probe["alive_before_cleanup"] == 3
    assert len(probe["joins"]) == 3
    assert all(join["stopping"] and join["loop_closed"] for join in probe["joins"])
    assert 0 < sum(join["timeout"] for join in probe["joins"]) <= 1.01


@pytest.mark.parametrize("mode,diagnostic", [
    ("unstopped", "Giraffe threads left running: AnyIO worker thread"),
    ("thread", "Giraffe threads left running: giraffe-leaked-probe"),
    ("task", "Asyncio tasks left running:"),
    ("state", "Unrestored runner.LLMClient:"),
])
def test_resource_guard_keeps_other_leak_detection(mode, diagnostic, tmp_path):
    result, probe = run_guard_probe(tmp_path, mode)
    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout
    assert probe["joins"] == []


@pytest.mark.parametrize("mode,diagnostic", [
    ("mixed", "Giraffe threads left running: giraffe-leaked-probe"),
    ("busy", "Giraffe threads left running: AnyIO worker thread"),
])
def test_resource_guard_does_not_hide_leaks_that_finish_during_another_workers_join(
        mode, diagnostic, tmp_path):
    result, probe = run_guard_probe(tmp_path, mode)
    assert result.returncode == 1, result.stdout + result.stderr
    assert diagnostic in result.stdout
    assert len(probe["joins"]) == 1
    assert probe["worker_count"] == (2 if mode == "busy" else 1)
    assert probe["alive_before_cleanup"] == probe["ordinary_alive_before_cleanup"] == 0
