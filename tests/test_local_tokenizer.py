"""Local prompt preparation sends zero inference requests and fails boundedly."""
import asyncio
import sys
import time
from types import SimpleNamespace

import pytest

from giraffe import local_tokenizer as sizing
from giraffe.load_test import execute
from giraffe.runner import _Run
from tests.test_load_test_phase_a import Client, config


def run_and_target():
    events = []
    run = SimpleNamespace(deadline=time.monotonic() + 30,
                          preparation=lambda *a, **kw: events.append((a, kw)))
    return run, SimpleNamespace(name="local", model="exact/model", tokenizer="exact/model"), events


@pytest.mark.usefixtures("local_tokenizer")
async def test_local_sizing_counts_complete_prompt_and_discloses_provenance():
    run, target, events = run_and_target()
    result = await sizing.prepare_messages(run, target, "load", lambda n: [
        {"role": "user", "content": "mandatory instructions " + "x" * n}], 128)
    assert result["status"] == "calibrated"
    assert abs(result["input_tokens"] - 128) <= 12.8
    assert result["input_tokens"] == round(len(result["messages"][0]["content"]) / 4)
    assert result["method"] == "local_tokenizer"
    assert result["tokenizer"] == "exact/model"
    assert result["chat_template"] and result["local_files_only"]
    assert result["request_ids"] == []
    assert events[0][0][3] == "running" and events[-1][0][3] == "completed"


@pytest.mark.usefixtures("local_tokenizer")
async def test_load_and_stress_do_not_send_calibration_requests():
    c = config()
    c.load_test.schedule.duration_seconds = .04
    run, client = _Run(c, None, None), Client((.01,))
    observation = await execute(run, c.targets[0], client)
    assert client.specs
    assert all(not spec.scenario.startswith("load_calibration") for spec in client.specs)
    assert all(spec.max_tokens != 1 for spec in client.specs)
    assert all(spec.workload["input_preparation"]["method"] == "local_tokenizer" for spec in client.specs)
    assert observation["calibrations"][0]["request_ids"] == []
    c.load_test.workloads.combinations.enabled = False
    c.load_test.workloads.stress.enabled = True
    c.load_test.workloads.stress.cases = ["long_short"]
    run, client = _Run(c, None, None), Client((.01,))
    observation = await execute(run, c.targets[0], client)
    assert client.specs and all(not s.scenario.startswith("load_calibration") for s in client.specs)
    assert observation["calibrations"][0]["method"] == "local_tokenizer"


async def test_builtin_failure_never_reverts_to_character_estimator(monkeypatch):
    calls = []
    async def missing(name):
        calls.append(name)
        raise sizing.TokenizerUnavailable("Built-in encoding unavailable")
    monkeypatch.setattr(sizing, "_start_worker", missing)
    c = config()
    c.load_test.workloads.combinations.selected = ["short_short", "long_long"]
    run, client = _Run(c, None, None), Client()
    observation = await execute(run, c.targets[0], client)
    assert calls == ["builtin"]
    assert not client.specs
    assert all(row["status"] == "unavailable" for row in observation["calibrations"])


async def test_hung_tokenizer_is_killed_and_other_event_loop_work_continues(monkeypatch):
    workers = []
    async def hung(_):
        p = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        workers.append(p)
        return p
    monkeypatch.setattr(sizing, "_start_worker", hung)
    monkeypatch.setattr(sizing, "PREPARATION_TIMEOUT_SECONDS", .08)
    run, target, events = run_and_target()
    target.tokenizer = "explicit/model"
    began = time.monotonic()
    task = asyncio.create_task(sizing.prepare_messages(run, target, "load", lambda n: [], 128))
    await asyncio.sleep(.01)
    assert not task.done()
    result = await task
    assert time.monotonic() - began < .5
    assert result["status"] == "unavailable" and "exceeded" in result["reason"]
    assert workers[0].returncode is not None
    assert events[-1][0][3] == "unavailable"


async def test_target_preparation_budget_prevents_additional_workers(monkeypatch):
    async def forbidden(_):
        raise AssertionError("No worker should start after the target preparation budget")
    monkeypatch.setattr(sizing, "_start_worker", forbidden)
    run, target, _ = run_and_target()
    target.tokenizer = "explicit/model"
    run._tokenizer_preparation_seconds = {target.name: sizing.TARGET_PREPARATION_TIMEOUT_SECONDS}
    result = await sizing.prepare_messages(run, target, "load", lambda n: [], 128)
    assert result["status"] == "unavailable" and "target budget" in result["reason"]


async def test_stop_during_preparation_kills_worker_and_finishes_progress(monkeypatch):
    spawned = asyncio.Event()
    workers = []
    async def slow(_):
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        workers.append(process)
        spawned.set()
        return process
    monkeypatch.setattr(sizing, "_start_worker", slow)
    run, target, events = run_and_target()
    task = asyncio.create_task(sizing.prepare_messages(run, target, "load", lambda n: [], 128))
    await spawned.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert workers[0].returncode is not None
    assert events[-1][0][3] == "cancelled"
    assert run._tokenizer_preparation_seconds[target.name] > 0


async def test_worker_forces_offline_matching_tokenizer_and_counts_chat_template(tmp_path, monkeypatch):
    # Exercise the actual import/worker entry point without a model download.
    # The stub rejects incorrect options and includes role/template token costs.
    (tmp_path / "transformers.py").write_text('''
import os
class AutoTokenizer:
    chat_template = "matching-template"
    @classmethod
    def from_pretrained(cls, name, **kwargs):
        assert name == "exact/model"
        assert kwargs == {"local_files_only": True, "trust_remote_code": False}
        assert os.environ["HF_HUB_OFFLINE"] == os.environ["TRANSFORMERS_OFFLINE"] == "1"
        return cls()
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt, return_dict):
        assert tokenize and add_generation_prompt and not return_dict
        return [0] * (7 + sum(len(m["content"]) for m in messages))
''')
    from pathlib import Path
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path}:{Path(sizing.__file__).parents[1]}")
    run, target, _ = run_and_target()
    result = await sizing.prepare_messages(run, target, "load", lambda n: [{"role": "user", "content": "x" * n}], 100, tolerance=0)
    assert result["status"] == "calibrated"
    assert result["input_tokens"] in {99, 100, 101}
    assert result["input_tokens"] == 7 + result["filler_chars"]
