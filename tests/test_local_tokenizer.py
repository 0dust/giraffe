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
@pytest.mark.parametrize("offset", [-4, 4])
async def test_explicit_token_range_constrains_sizing_tolerance(offset):
    run, target, _ = run_and_target()
    result = await sizing.prepare_messages(run, target, "context", lambda n: [
        {"role": "user", "content": "x" * (4 + n + (offset if n else 0))}],
        100, tolerance=.01, accepted_token_range=(100, 100))
    assert result["status"] == "calibrated"
    assert result["input_tokens"] == 100
    assert result["attempts"] > 2  # The first estimate is within tolerance but outside the bound.


@pytest.mark.usefixtures("local_tokenizer")
async def test_explicit_token_range_rejects_minimum_prompt_that_does_not_fit():
    run, target, _ = run_and_target()
    def build(n):
        return [{"role": "user", "content": "x" * (400 + n)}]
    result = await sizing.prepare_messages(run, target, "context", build, 90,
                                          accepted_token_range=(80, 90))
    assert result["status"] == "unavailable"
    assert result["input_tokens"] == 100
    assert result["attempts"] == 1
    inherited = await sizing.prepare_messages(run, target, "load", build, 90)
    assert inherited["status"] == "calibrated"
    assert inherited["reason_code"] == "minimum_prompt_length"


@pytest.mark.usefixtures("local_tokenizer")
async def test_explicit_token_range_accepts_minimum_prompt_above_target_that_still_fits():
    run, target, _ = run_and_target()
    result = await sizing.prepare_messages(run, target, "context", lambda n: [
        {"role": "user", "content": "x" * (400 + n)}],
        90, accepted_token_range=(80, 110))
    assert result["status"] == "calibrated"
    assert result["input_tokens"] == 100
    assert result["reason_code"] == "minimum_prompt_length"
    assert result["attempts"] == 1


@pytest.mark.usefixtures("local_tokenizer")
async def test_unrealizable_exact_token_range_stays_bounded_and_unavailable():
    run, target, _ = run_and_target()
    result = await sizing.prepare_messages(run, target, "context", lambda n: [
        {"role": "user", "content": "x" * (8 * (n // 4))}],
        99, accepted_token_range=(99, 99))
    assert result["status"] == "unavailable"
    assert result["attempts"] <= sizing.MAX_SIZING_ATTEMPTS
    assert not result.get("messages")


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
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(60)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    workers = []
    responsive = asyncio.Event()
    pending_at_probe = []
    def probe():
        pending_at_probe.append(not task.done())
        responsive.set()
    async def hung(_):
        workers.append(process)
        asyncio.get_running_loop().call_soon(probe)
        return process
    monkeypatch.setattr(sizing, "_start_worker", hung)
    monkeypatch.setattr(sizing, "PREPARATION_TIMEOUT_SECONDS", .08)
    run, target, events = run_and_target()
    target.tokenizer = "explicit/model"
    began = time.monotonic()
    task = asyncio.create_task(sizing.prepare_messages(run, target, "load", lambda n: [], 128))
    try:
        await asyncio.wait_for(responsive.wait(), 5)
        assert pending_at_probe == [True]
        result = await asyncio.wait_for(task, 5)
        assert workers[0].returncode is not None
        assert time.monotonic() - began < 5
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if process.returncode is None:
            process.kill()
        await process.wait()
    assert result["status"] == "unavailable"
    assert result["reason"] == "Local tokenizer preparation exceeded 0.08s; no inference calibration was sent."
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
    assert events[-1][0][3] == "cancelled"
    assert run._tokenizer_preparation_seconds[target.name] > 0


@pytest.mark.parametrize("content_only", [False, True])
async def test_worker_forces_offline_matching_tokenizer_and_counts_chat_template(tmp_path, monkeypatch, content_only):
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
    def encode(self, content, *, add_special_tokens):
        assert not add_special_tokens
        return [0] * len(content)
''')
    from pathlib import Path
    monkeypatch.setenv("PYTHONPATH", f"{tmp_path}:{Path(sizing.__file__).parents[1]}")
    run, target, _ = run_and_target()
    result = await sizing.prepare_messages(run, target, "load", lambda n: [{"role": "user", "content": "x" * n}], 100, tolerance=0, content_only=content_only)
    assert result["status"] == "calibrated"
    assert result["input_tokens"] in {99, 100, 101}
    assert result["input_tokens"] == (0 if content_only else 7) + result["filler_chars"]
    assert result["token_count_scope"] == ("message_content" if content_only else "chat_template")


@pytest.mark.parametrize("budget", ["run", "target"])
async def test_preparation_timeout_reports_the_binding_budget_without_a_monitor(monkeypatch, budget):
    from giraffe import runner

    clock = SimpleNamespace(now=0.)
    local_time = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(sizing, "time", local_time)
    monkeypatch.setattr(runner, "time", local_time)

    class Worker:
        returncode = None

        async def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    worker = Worker()

    async def start(_):
        return worker

    async def blocked(_):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            clock.now = .02
            raise

    monkeypatch.setattr(sizing, "_start_worker", start)
    monkeypatch.setattr(sizing, "_read_worker", blocked)
    configured = config()
    configured.max_duration_seconds = .01 if budget == "run" else None
    run = runner._Run(configured, None, None)
    target = configured.targets[0]
    if budget == "target":
        run._tokenizer_preparation_seconds = {target.name: sizing.TARGET_PREPARATION_TIMEOUT_SECONDS - .01}
    result = await sizing.prepare_messages(run, target, "load", lambda n: [], 128)
    assert result["status"] == "unavailable" and result["request_ids"] == []
    assert worker.returncode == -9
    if budget == "run":
        assert "maximum run duration reached" in result["reason"]
        assert "exceeded 10s" not in result["reason"]
        assert run.reason == "maximum run duration reached"
        assert run.limits_reached == {"max_duration_seconds"}
    else:
        assert "20s target budget" in result["reason"]
        assert "exceeded 10s" not in result["reason"]
        assert run.reason is None and not run.limits_reached
        assert not run._tokenizer_unavailable


@pytest.mark.parametrize("reason", ["cancelled by user", None])
async def test_cancelled_preparation_retains_stop_cause_and_does_not_cache_failure(monkeypatch, reason):
    entered = asyncio.Event()

    async def blocked(_):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(sizing, "_start_worker", blocked)
    run, target, events = run_and_target()
    run.stop_reason = lambda name: reason
    task = asyncio.create_task(sizing.prepare_messages(run, target, "load", lambda n: [], 128))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[-1][0][3] == "cancelled"
    assert events[-1][1]["reason"] == (reason or "Preparation cancelled")
    assert not run._tokenizer_unavailable


@pytest.mark.usefixtures("local_tokenizer")
async def test_successful_preparation_cleanup_does_not_create_a_later_run_deadline_abort(monkeypatch):
    from giraffe import runner

    clock = SimpleNamespace(now=0.)
    local_time = SimpleNamespace(monotonic=lambda: clock.now)
    monkeypatch.setattr(sizing, "time", local_time)
    monkeypatch.setattr(runner, "time", local_time)
    original_start = sizing._start_worker

    async def start(name):
        worker = await original_start(name)

        async def cleanup():
            clock.now = 2.
            return worker.returncode

        worker.wait = cleanup
        return worker

    monkeypatch.setattr(sizing, "_start_worker", start)
    configured = config()
    configured.max_duration_seconds = 1
    run = runner._Run(configured, None, None)
    result = await sizing.prepare_messages(run, configured.targets[0], "load", lambda n: [
        {"role": "user", "content": "x" * n}], 128)
    assert result["status"] == "calibrated"
    assert clock.now > run.deadline
    assert run.reason is None and not run.limits_reached


@pytest.mark.usefixtures("local_tokenizer")
async def test_request_budget_does_not_become_a_preparation_timeout():
    run = _Run(config(), None, None)
    run.reason = "maximum request budget reached"
    result = await sizing.prepare_messages(run, run.config.targets[0], "load", lambda n: [
        {"role": "user", "content": "x" * n}], 128)
    assert result["status"] == "calibrated"
    assert run.reason == "maximum request budget reached"
    assert not run._tokenizer_unavailable


@pytest.mark.parametrize("limit, offset", [(limit, offset) for limit in (1024, 2048, 3000) for offset in (0, 1, 3)])
async def test_context_sizing_converges_near_full_coverage_boundary(limit, offset):
    # Available input just above ceil(0.8 * limit) leaves a one-to-few token window;
    # (2048, available == 1639) hit a gap in the builtin tokenizer's token counts.
    import math
    from collections import defaultdict
    from giraffe.fixtures import builtin_fixtures
    from giraffe.models import RunConfig
    from giraffe.runner import _context_fixtures

    output = limit - (math.ceil(limit * .8) + offset)
    config = RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://endpoint.invalid/v1", "model": "m", "tokenizer": "builtin"}],
        "checks": ["context"], "context_limit": limit, "max_output_tokens": output})
    run = SimpleNamespace(config=config, deadline=time.monotonic() + 60, preparation=lambda *a, **kw: None,
                          observations=defaultdict(dict), mark_unfinished=lambda *a, **kw: None,
                          stop_reason=lambda name: None)
    prepared = await _context_fixtures(run, config.targets[0], None, builtin_fixtures(config))
    assert len(prepared) == 6, run.observations["local"]["context_calibration"]
    for spec in prepared:
        if spec.fixture_id.startswith("context.long."):
            tokens = spec.workload["calibrated_input_tokens"]
            assert math.ceil(limit * .8) <= tokens <= limit - output


@pytest.mark.usefixtures("local_tokenizer")
async def test_sizing_gap_is_reported_only_when_counts_straddle_the_accepted_range():
    run, target, _ = run_and_target()
    # The fake counter is len/4 and this builder grows in 8-character steps, so every
    # count is even and the single accepted value 101 is skipped (100 or 102 only).
    gap = await sizing.prepare_messages(run, target, "context", lambda n: [
        {"role": "user", "content": "x" * (8 * (n // 8) + 8)}], 101, tolerance=.01, accepted_token_range=(101, 101))
    assert gap["status"] == "unavailable" and gap["reason_code"] == "input_sizing_unavailable"
    assert gap["sizing_gap"] is True
    # A minimum prompt that already exceeds the range is not a gap.
    over = await sizing.prepare_messages(run, target, "context", lambda n: [
        {"role": "user", "content": "x" * (400 + n)}], 90, accepted_token_range=(80, 90))
    assert "sizing_gap" not in over
    # Counts that never grow cannot bracket the target either.
    flat = await sizing.prepare_messages(run, target, "context", lambda n: [
        {"role": "user", "content": "x" * 40}], 100, tolerance=.01, accepted_token_range=(100, 100))
    assert flat["reason_code"] == "input_sizing_unavailable" and "sizing_gap" not in flat


def _context_run(limit, output):
    from collections import defaultdict
    from giraffe.models import RunConfig
    config = RunConfig.model_validate({
        "targets": [{"name": "local", "url": "http://endpoint.invalid/v1", "model": "m", "tokenizer": "builtin"}],
        "checks": ["context"], "context_limit": limit, "max_output_tokens": output})
    run = SimpleNamespace(config=config, deadline=time.monotonic() + 60, preparation=lambda *a, **kw: None,
                          observations=defaultdict(dict), mark_unfinished=lambda *a, **kw: None,
                          stop_reason=lambda name: None)
    return config, run


@pytest.mark.parametrize("extra", [{}, {"sizing_gap": True}])
async def test_context_variant_retry_runs_only_for_a_sizing_gap(monkeypatch, extra):
    from giraffe import local_tokenizer
    from giraffe.fixtures import builtin_fixtures
    from giraffe.runner import _context_fixtures

    config, run = _context_run(2048, 409)
    calls = []

    async def unavailable(run, target, scenario, build, desired, **kwargs):
        calls.append(build(0))
        return dict(status="unavailable", reason="stub", reason_code="input_sizing_unavailable", input_tokens=None,
                   filler_chars=0, request_ids=[], method="local_tokenizer", tokenizer="builtin", **extra)

    monkeypatch.setattr(local_tokenizer, "prepare_messages", unavailable)
    assert await _context_fixtures(run, config.targets[0], None, builtin_fixtures(config)) == []
    calibration = run.observations["local"]["context_calibration"]
    assert calibration["reason"] == "stub"
    assert len(calls) == (2 if extra else 1)
    assert calibration["preparations"][0].get("prompt_variant") == ("closing_instruction_no_other_text" if extra else None)
    if extra:
        assert calls[0][0]["content"].endswith("Reply with the box label only.")
        assert calls[1][0]["content"].endswith("Reply with the box label only, no other text.")


async def test_context_fallback_prompt_is_recorded_and_normal_prompt_is_unchanged():
    from giraffe.fixtures import builtin_fixtures
    from giraffe.runner import _context_fixtures

    config, run = _context_run(2048, 409)  # a single-token range; the builtin counts skip it for long.middle
    prepared = {spec.fixture_id: spec for spec in await _context_fixtures(
        run, config.targets[0], None, builtin_fixtures(config))}
    evidence = run.observations["local"]["context_calibration"]["preparations"]
    variants = {spec.fixture_id for spec in prepared.values() if "prompt_variant" in spec.workload}
    assert variants and all(spec.fixture_id.startswith("context.long.") for spec in prepared.values()
                            if spec.fixture_id in variants)
    flagged = [item for item in evidence if "prompt_variant" in item]
    assert len(flagged) == len(variants)
    for spec in prepared.values():
        content = spec.messages[0]["content"]
        assert spec.input_chars == len(content)
        assert content.endswith("no other text.") == (spec.fixture_id in variants)
    # Non-variant specs are byte-identical to the stock builder's prompt shape.
    normal = next(spec for spec in prepared.values() if spec.fixture_id not in variants)
    assert normal.messages[0]["content"].endswith("\nReply with the box label only.")


async def test_fallback_run_does_not_change_planning_profile_text_or_fixture_identity():
    from giraffe.deployment import fixture_hash
    from giraffe.fixtures import CONTEXT_SIZING_VERSION, FIXTURE_VERSION, builtin_fixtures
    from giraffe.latency import calibrated_context_fixtures
    from giraffe.runner import _context_fixtures

    config, run = _context_run(2048, 409)
    stock = builtin_fixtures(config)
    before = fixture_hash({key: [spec.model_dump() for spec in specs] for key, specs in stock.items()})
    prepared = await _context_fixtures(run, config.targets[0], None, stock)
    assert any("prompt_variant" in spec.workload for spec in prepared)
    # Manifest fixture identity derives from the unsized fixtures, so it is the same with or without fallback.
    assert fixture_hash({key: [spec.model_dump() for spec in specs] for key, specs in stock.items()}) == before
    shapes = [{"fixture_id": spec.fixture_id, "input_chars": spec.input_chars, "output_cap": spec.max_tokens}
              for spec in prepared]
    profile = {"fixture_version": FIXTURE_VERSION, "context_limit": 2048, "fixture_context_limit": 2048,
               "sizing_version": CONTEXT_SIZING_VERSION, "method": "local_tokenizer", "tokenizer": "builtin",
               "fixtures": shapes}
    reused = calibrated_context_fixtures({"local": [{"family": "context", "context_profile": profile}]},
                                         "local", config)
    # A reused profile is a planning preview that is never sent: stock text, recorded sizes.
    assert reused and all(spec.messages[0]["content"].endswith("\nReply with the box label only.") for spec in reused)
    assert {spec.fixture_id: spec.input_chars for spec in reused} == {spec.fixture_id: spec.input_chars for spec in prepared}
