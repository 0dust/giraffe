"""Short requests arriving after a long answer starts, with deterministic time."""

import asyncio

import pytest
import yaml

from giraffe import load_test
from giraffe.models import RunConfig
from giraffe.runner import _Run
from tests.test_load_test_phase_a import TARGET, record
from tests.virtual_clock import run_virtual


pytestmark = pytest.mark.usefixtures("local_tokenizer")


def configuration(mode="short_during_long"):
    settings = yaml.safe_load("""
checks: [load]
max_memory_mb: 0
load_test:
  workloads:
    combinations: {enabled: false}
    interference: {enabled: true}
  patterns:
    concurrent: {enabled: true, levels: [2]}
    constant: {enabled: false}
  pass_criteria:
    min_valid_responses: 1
    max_first_answer_ms: 1000
    max_completion_ms: 1000
""")
    settings["targets"] = [TARGET]
    if mode is not None:
        settings["load_test"]["workloads"]["interference"]["mode"] = mode
    result = RunConfig.model_validate(settings)
    result.load_test.schedule.duration_seconds = 1
    return result


class StreamingClient:
    def __init__(self, *, queued=False, buffered=False, early=False, missing_usage=False):
        self.queued, self.buffered, self.early, self.missing_usage = queued, buffered, early, missing_usage
        self.calls, self.long_answers, self.dispatch_chunks = [], [], []

    async def execute(self, spec, first_output_event=None):
        loop = asyncio.get_running_loop()
        start = loop.time()
        shape, phase = spec.load_point["shape"], spec.load_point["phase"]
        active = [item for item in self.long_answers if item["started"] and not item["done"].is_set()]
        self.calls.append((shape, phase, bool(active)))
        if shape == "short_short" and phase == "probe":
            self.dispatch_chunks.append(min(item["progress"].chunks for item in active))
        if shape == "short_long":
            state = {"started": False, "done": asyncio.Event(), "progress": first_output_event}
            self.long_answers.append(state)
            await asyncio.sleep(.005)
            state["started"] = True
            first = (loop.time() - start) * 1000
            if not self.buffered:
                first_output_event.set()
                first_output_event.on_answer_chunk()
            for _ in range(1 if self.buffered else 9):
                await asyncio.sleep(.01)
                if not self.buffered:
                    first_output_event.on_answer_chunk()
            if self.buffered:
                first_output_event.set()
                first_output_event.on_answer_chunk()
            first_output_event.on_stream_finished()
            state["done"].set()
        else:
            if self.queued and active:
                await asyncio.gather(*(item["done"].wait() for item in active))
            await asyncio.sleep(.005)
            first = (loop.time() - start) * 1000 - 1
        elapsed = (loop.time() - start) * 1000
        result = record().model_copy(update={
            "id": f"r{len(self.calls)}-{start}", "fixture_id": spec.fixture_id,
            "scenario": spec.scenario, "load_point": spec.load_point,
            "elapsed_ms": elapsed, "first_output_ms": first,
            "last_output_ms": elapsed, "stream_end_ms": elapsed,
            "answer_chunks": 1 if self.buffered and shape == "short_long" else 10,
            "answer_reads": 1 if self.buffered and shape == "short_long" else 10,
            "output_tokens": 64 if self.early else spec.max_tokens,
            "completion_tokens": 64 if self.early else spec.max_tokens,
            "input_tokens": 128, "stream": True, "requested_max_tokens": spec.max_tokens,
            "max_stream_gap_ms": 10, "answer_chars": 20, "output_chars": 20,
            "first_read_answer_chars": 2,
            "output": "K7P4",
        })
        if self.missing_usage and shape == "short_long":
            result.output_tokens = result.completion_tokens = None
        return result


def test_yaml_mode_and_default_keep_distinct_preparation_and_labels():
    reverse = configuration()
    default = configuration(None)
    assert default.load_test.workloads.interference.mode == "long_input_during_short"
    assert reverse.model_dump()["load_test"]["workloads"]["interference"]["mode"] == "short_during_long"
    assert load_test.running_shapes(reverse.load_test) == ["short_short", "short_long"]
    assert load_test.calibration_keys(reverse.load_test) == [("short", "short_answer"), ("short", "long_output")]
    assert load_test.running_shapes(default.load_test) == ["short_short", "long_short"]
    point, = load_test.test_points(reverse.load_test)
    assert "long answer" in load_test.point_label(point).lower()


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("trigger,chunks", [("streaming_started", 1), ("after_five_chunks", 5)])
def test_short_dispatch_follows_live_long_output_and_keeps_queued_delay(monkeypatch, queued, trigger, chunks):
    async def scenario():
        config = configuration()
        config.load_test.workloads.interference.trigger = trigger
        run, client = _Run(config, None, None), StreamingClient(queued=queued)
        observation = await load_test.execute(run, config.targets[0], client)
        point, = observation["points"]
        evidence = point["interference"]
        shorts = [call for call in client.calls if call[:2] == ("short_short", "probe")]
        assert shorts and all(active for _, _, active in shorts)
        assert all(count >= chunks for count in client.dispatch_chunks)
        assert all(shape == "short_short" for shape, phase, _ in client.calls if phase == "baseline")
        assert evidence["probe"]["matched_count"] > 0
        assert evidence["baseline"]["matched_count"] > 0
        assert evidence["probe"]["first_answer_median_ms"] is not None
        assert evidence["mode"] == "short_during_long"
        if queued:
            assert evidence["probe"]["matched_count"] == len(shorts)
            assert evidence["slowdown"] > 10
            assert evidence["first_answer_slowdown"] > 10
            assert point["status"] == "fail"
        else:
            assert point["status"] == "pass", point["reason"]
        assert run.slots.qsize() == run.slots.maxsize

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("client_options", [{"buffered": True}, {"early": True}, {"missing_usage": True}])
def test_unexercised_long_answer_cannot_pass(monkeypatch, client_options):
    async def scenario():
        config = configuration()
        run = _Run(config, None, None)
        point, = (await load_test.execute(run, config.targets[0], StreamingClient(**client_options)))["points"]
        assert point["status"] == "inconclusive", point["reason"]

    run_virtual(monkeypatch, scenario())


def test_finished_long_answer_is_rechecked_at_request_admission(monkeypatch):
    async def scenario():
        config = configuration()
        run, client = _Run(config, None, None), StreamingClient()
        original = run.request

        async def request(target, endpoint, spec, **kwargs):
            if spec.load_point["phase"] == "probe" and spec.load_point["shape"] == "short_short":
                for state in client.long_answers:
                    state["progress"].on_stream_finished()
            return await original(target, endpoint, spec, **kwargs)

        monkeypatch.setattr(run, "request", request)
        point, = (await load_test.execute(run, config.targets[0], client))["points"]
        assert not any(shape == "short_short" and phase == "probe" for shape, phase, _ in client.calls)
        assert point["interference"]["triggered"] == 0
        assert point["status"] == "inconclusive"

    run_virtual(monkeypatch, scenario())


@pytest.mark.parametrize("difference", ["fixture", "budget", "actual_length"])
def test_different_short_work_cannot_be_reported_as_interference(difference):
    config = configuration()
    point, = load_test.test_points(config.load_test)
    cap = 512 if difference == "actual_length" else 64
    baseline = [record().model_copy(update={
        "id": f"baseline-{i}", "fixture_id": "saved-a", "requested_max_tokens": cap,
        "input_tokens": 128, "output_tokens": 64, "completion_tokens": 64,
        "dispatch_ms": i * 10, "timing_start_ms": i * 10,
        "completed_ms": i * 10 + 10, "elapsed_ms": 10, "first_output_ms": 1,
        "last_output_ms": 10, "stream_end_ms": 10, "answer_chunks": 2, "stream": True,
        "load_point": {"shape": "short_short", "role": "short_stream", "phase": "baseline"},
    }) for i in range(99)]
    short = baseline[0].model_copy(deep=True, update={
        "id": "short", "dispatch_ms": 1100, "timing_start_ms": 1100, "completed_ms": 1200,
        "elapsed_ms": 100, "last_output_ms": 100, "stream_end_ms": 100,
        "load_point": {"shape": "short_short", "role": "short_stream", "phase": "probe"},
    })
    if difference == "fixture":
        short.fixture_id = "saved-b"
    elif difference == "budget":
        short.requested_max_tokens = 512
    else:
        short.output_tokens = short.completion_tokens = 256
    long = short.model_copy(deep=True, update={
        "id": "long", "fixture_id": "long", "requested_max_tokens": 512,
        "dispatch_ms": 1000, "timing_start_ms": 1000, "completed_ms": 2000,
        "elapsed_ms": 1000, "last_output_ms": 1000, "stream_end_ms": 1000,
        "output_tokens": 512, "completion_tokens": 512,
        "load_point": {"shape": "short_long", "role": "long_stream", "phase": "probe"},
    })
    point.update(cut_short=False, sending_start_ms=0, sending_end_ms=2000,
                 delivered_window_seconds=2, observation_seconds=2, measurement_windows=[
                     {"phase": "baseline", "sending_start_ms": 0, "sending_end_ms": 990},
                     {"phase": "probe", "sending_start_ms": 1000, "sending_end_ms": 2000, "triggered": 1},
                 ])
    run = _Run(config, None, None)
    run.records = baseline + [short, long]
    load_test.assess_point(run, config.targets[0], config.load_test, point, run.records)
    assert point["interference"]["slowdown"] == 10
    assert point["status"] == "inconclusive", point["reason"]
    assert "comparable" in point["reason"].lower()


def test_all_background_streams_need_long_output_coverage(monkeypatch):
    class MixedCoverageClient(StreamingClient):
        sequence = 0

        async def execute(self, spec, first_output_event=None):
            self.sequence += spec.load_point["shape"] == "short_long"
            sequence = self.sequence
            response = await super().execute(spec, first_output_event)
            if spec.load_point["shape"] == "short_long" and sequence % 3 != 1:
                response.output_tokens = response.completion_tokens = 64
            return response

    async def scenario():
        config = configuration()
        config.load_test.patterns.concurrent.levels = [4]
        run = _Run(config, None, None)
        point, = (await load_test.execute(run, config.targets[0], MixedCoverageClient()))["points"]
        assert point["status"] == "inconclusive", point["reason"]

    run_virtual(monkeypatch, scenario())


def test_reverse_initial_budget_uses_one_baseline_short_and_requested_population():
    config = configuration()
    values = config.model_dump()
    values["load_test"]["patterns"]["concurrent"]["levels"] = [4]
    values["max_requests"] = 5
    restored = RunConfig.model_validate(values)
    assert load_test.minimum_reservation(restored.load_test) == 5
    restored.load_test.workloads.interference.mode = "long_input_during_short"
    assert load_test.minimum_reservation(restored.load_test) == 7
