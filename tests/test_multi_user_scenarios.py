"""Matched competing users and cancellation preserve each user's observations."""
import asyncio
import json
from copy import deepcopy

import pytest

from giraffe import runner
from giraffe.multi_user import _planned_observation, execute, result
from tests.test_multi_user import settings
from tests.test_multi_user_metrics import record


pytestmark = pytest.mark.usefixtures("local_tokenizer")


PROFILE = {"short": {"input_tokens": 64, "output_tokens": 16},
           "medium": {"input_tokens": 128, "output_tokens": 24},
           "long": {"input_tokens": 256, "output_tokens": 32}}


def test_noisy_neighbors_plan_matches_short_users_and_preserves_roles():
    cfg = settings(levels=[3], scenarios=["noisy_neighbors"], noisy_neighbors={
        "heavy_user_ids": ["user-03"], "heavy_profiles": ["long_short", "short_long"],
        "triggers": ["before_output", "during_stream"]})
    plan = _planned_observation(cfg.multi_user)
    assert len(plan["windows"]) == 8
    for control, probe in zip(plan["windows"][::2], plan["windows"][1::2]):
        assert control["role"] == "control"
        assert probe["role"] == "probe"
        assert probe["control_window_id"] == control["id"]
        assert [u["user_id"] for u in control["users"]] == ["user-01", "user-02"]
        assert [u["user_id"] for u in probe["users"]] == ["user-01", "user-02", "user-03"]
        assert all(u["profile"] == "short_short" for u in control["users"])
        assert control["phase"] == probe["phase"] == "measurement"


def test_selected_cancellation_is_a_distinct_user_scenario():
    cfg = settings(levels=[2], scenarios=["cancellation"], cancellation={"user_id": "user-02"})
    window, = _planned_observation(cfg.multi_user)["windows"]
    assert window["scenario"] == "cancellation"
    assert window["selected_user_id"] == "user-02"
    assert window["users"][1]["profile"] == "short_long"


@pytest.mark.parametrize("scenario", ["cancellation", "noisy_neighbors"])
def test_waiting_for_scenario_opportunity_is_not_pending_admission(monkeypatch, scenario):
    from giraffe.models import LoadTestSettings
    from giraffe.multi_user import run_window
    from giraffe.multi_user_metrics import summarize
    from tests.test_runner import FakeClient

    async def run():
        cfg = settings(levels=[2], duration_seconds=.12, scenarios=[scenario],
            cancellation={"user_id": "user-02"}, noisy_neighbors={
                "heavy_user_ids": ["user-02"], "heavy_profiles": ["long_short"],
                "triggers": ["during_stream"]},
            pass_criteria={"max_first_answer_ms": 70}).model_copy(update={"concurrency": 2})
        state = runner._Run(cfg, None, None)
        observation = _planned_observation(cfg.multi_user)
        window = observation["windows"][-1]
        observation["windows"] = [window]
        monkeypatch.setattr(FakeClient, "useful_event", False)
        calibration = {(size, mode): {"filler_chars": 100}
                       for size in ("short", "long") for mode in ("short_answer", "long_output")}
        await run_window(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg), cfg.multi_user,
                         LoadTestSettings(profile=cfg.multi_user.profile), calibration, window)
        assert state.records and all(r.workload["user_id"] == "user-01" for r in state.records)
        assert state.slots.qsize() == 2
        selected = window["users"][1]
        assert selected["dispatched_count"] == 0
        summary = summarize(observation, state.records)["windows"][0]
        user = summary["users"][1]
        assert user["pending_wait"] is None
        assert user["status"] == "inconclusive"
        assert selected["ready_count"] == 0
        assert selected["pending_ready_ms"] is None

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["cancellation", "noisy_neighbors"])
def test_peer_lost_while_waiting_for_slot_does_not_dispatch_or_leave_pending_wait(monkeypatch, scenario):
    from giraffe.models import LoadTestSettings
    from giraffe.multi_user import run_window
    from giraffe.multi_user_metrics import summarize
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    async def run():
        cfg = settings(levels=[2], duration_seconds=.13, think_time_seconds={"values": [1]},
            profile=PROFILE, scenarios=[scenario], cancellation={"user_id": "user-02"},
            noisy_neighbors={"heavy_user_ids": ["user-02"], "heavy_profiles": ["long_short"],
                             "triggers": ["before_output"]},
            pass_criteria={"max_first_answer_ms": 30}).model_copy(update={"concurrency": 1})
        state = runner._Run(cfg, None, None)
        observation = _planned_observation(cfg.multi_user)
        window = observation["windows"][-1]
        calibration = {(size, mode): {"filler_chars": 100}
                       for size in ("short", "long") for mode in ("short_answer", "long_output")}
        await run_window(state, cfg.targets[0], FakeClient(cfg.targets[0], cfg), cfg.multi_user,
                         LoadTestSettings(profile=cfg.multi_user.profile), calibration, window)
        assert state.attempted == 1
        assert [r.workload["user_id"] for r in state.records] == ["user-01"]
        assert state.slots.qsize() == 1
        selected = window["users"][1]
        assert selected["ready_count"] == 1
        user = summarize(observation, state.records)["windows"][-1]["users"][1]
        assert user["pending_wait"] is None
        assert user["status"] == "inconclusive"

    run_virtual(monkeypatch, run())


def test_matched_generated_work_does_not_repeat_prefixes_across_windows(monkeypatch):
    from giraffe.models import LoadTestSettings
    from giraffe.multi_user import run_window
    from tests.test_review26_phase4_accuracy import run_virtual
    from tests.test_runner import FakeClient

    class CaptureClient(FakeClient):
        async def execute(self, spec, **kwargs):
            if spec.workload["user_id"] == "user-01":
                sent.append((spec.messages[0]["content"], spec.workload["matched_work_sha256"]))
            return await super().execute(spec, **kwargs)

    sent = []

    async def run():
        cfg = settings(levels=[2], duration_seconds=.03, warmup_seconds=.03,
            think_time_seconds={"values": [1]}, profile=PROFILE, scenarios=["noisy_neighbors"],
            noisy_neighbors={"heavy_user_ids": ["user-02"], "heavy_profiles": ["long_short"],
                             "triggers": ["before_output"]})
        state = runner._Run(cfg, None, None)
        client = CaptureClient(cfg.targets[0], cfg)
        load = LoadTestSettings(profile=cfg.multi_user.profile)
        calibration = {(size, "short_answer"): {"filler_chars": 100} for size in ("short", "long")}
        for window in _planned_observation(cfg.multi_user)["windows"]:
            await run_window(state, cfg.targets[0], client, cfg.multi_user, load, calibration, window)

    run_virtual(monkeypatch, run())
    assert len(sent) == 4
    assert len({prompt for prompt, _ in sent}) == 4
    assert len({identity for _, identity in sent}) == 1


@pytest.mark.parametrize("retention", ["none", "failures", "all"])
def test_http_selected_cancellation_retains_once_and_peers_continue(retention):
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[2], duration_seconds=.6, scenarios=["cancellation"],
                       profile={"short": {"input_tokens": 64, "output_tokens": 16},
                                "medium": {"input_tokens": 128, "output_tokens": 24},
                                "long": {"input_tokens": 256, "output_tokens": 32}},
                       cancellation={"user_id": "user-02", "cancel_after_chunks": 2}).model_copy(update={
            "targets": [Target(name="service", model="fake-model", url=endpoint.url)], "retention": retention})
        state = runner._Run(cfg, None, None)
        retained = []
        state.record_sink = retained.append
        async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
            await asyncio.wait_for(execute(state, cfg.targets[0], client), 5)
        cancelled, = [r for r in state.records if r.status == "cancelled"]
        assert cancelled.intentional and cancelled.answer_chunks >= 2 and not cancelled.stream_terminated
        assert len({r.id for r in retained}) == len(retained) == len(state.records)
        assert state.slots.qsize() == 2
        assert any(r.status == "completed" and r.workload.get("cancellation_role") == "followup" for r in state.records)
        summary = result(state, cfg.targets[0]).metrics["multi_user"]
        evidence = summary["windows"][0]["cancellation"]
        assert evidence["cancellation"]["status"] == "pass"
        assert evidence["peer_continuation"]["status"] == "pass"
        assert evidence["followup"]["status"] == "pass"
        assert evidence["backend_release"] == "unverified"
        from giraffe.models import RunReport
        from giraffe.reporting import _retained
        saved = _retained(RunReport(run_id="cancel", started_at="now", finished_at="later", overall="pass",
            manifest={"config": cfg.model_dump(mode="json")}, requests=state.records))
        terminal = next(r for r in saved.requests if r.id == cancelled.id)
        assert terminal.answer_chunks == cancelled.answer_chunks and terminal.workload["user_id"] == "user-02"
        assert bool(terminal.output) == (retention == "all")

    with FakeEndpoint(first_token_delay=.02, chunk_delay=.02) as endpoint:
        asyncio.run(run(endpoint))


@pytest.mark.parametrize(("read_mode", "threshold"), [
    ("separate", 2), ("partial", 1), ("complete", 2), ("finish_then_done", 1), ("done_only_cr", 2),
])
def test_selected_cancellation_checks_terminal_events_in_the_same_read(monkeypatch, read_mode, threshold):
    import httpx
    from giraffe.client import LLMClient
    from giraffe.models import LoadTestSettings
    from giraffe.multi_user import run_window
    from giraffe.multi_user_metrics import summarize
    from tests.test_review26_phase4_accuracy import run_virtual

    finish = (b'data: {"choices":[{"delta":{},"finish_reason":"length"}],'
              b'"usage":{"prompt_tokens":64,"completion_tokens":8}}\n\n')
    done = b"data: [DONE]\n\n"
    selected_reads = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, selected):
            self.selected = selected

        def frame(self, index):
            field = "reasoning_content" if self.selected and index < 2 else "content"
            content = "K7P4" if index == 0 else " 42"
            return f'data: {json.dumps({"choices": [{"delta": {field: content}}]})}\n\n'.encode()

        async def __aiter__(self):
            if self.selected and read_mode != "separate":
                await asyncio.sleep(.025)
                selected_reads.append("first")
                if read_mode == "partial":
                    yield b"".join(self.frame(index) for index in range(3))
                    selected_reads.append("after partial")
                    return
                answer = b"".join(self.frame(index) for index in range(8))
                if read_mode == "done_only_cr":
                    yield (answer + done).replace(b"\n", b"\r")
                elif read_mode == "complete":
                    yield answer + finish + done
                else:
                    yield answer + finish
                    yield done
                return

            for index in range(8):
                await asyncio.sleep(.01)
                yield self.frame(index)
            yield finish
            yield done

    async def run():
        cfg = settings(levels=[2], duration_seconds=.36, scenarios=["cancellation"],
                       profile=PROFILE, think_time_seconds={"values": [.1]},
                       cancellation={"user_id": "user-02", "cancel_after_chunks": threshold})
        target = cfg.targets[0]
        state = runner._Run(cfg, None, None)
        client = LLMClient(target, cfg, max_connections=2)
        requests = 0

        async def respond(request):
            nonlocal requests
            requests += 1
            # The selected request waits for the first peer to begin streaming.
            return httpx.Response(200, stream=Stream(requests == 2))

        observation = _planned_observation(cfg.multi_user)
        window, = observation["windows"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            client._client = transport
            await run_window(state, target, client, cfg.multi_user, LoadTestSettings(profile=cfg.multi_user.profile),
                             {("short", "long_output"): {"filler_chars": 100}}, window)
        selected, = [r for r in state.records if r.workload.get("cancellation_role") == "selected"]
        assert any(r.valid and r.workload.get("cancellation_role") == "followup" for r in state.records)
        assert state.slots.qsize() == 2
        evidence = summarize(observation, state.records)["windows"][0]["cancellation"]
        if read_mode in {"separate", "partial"}:
            assert selected.status == "cancelled" and selected.intentional
            assert selected.termination_reason == "generation_chunk_limit"
            assert selected.generation_chunks == (3 if read_mode == "partial" else 2)
            assert selected.answer_chunks == (1 if read_mode == "partial" else 0)
            assert all(evidence[key]["status"] == "pass" and evidence[key]["request_ids"]
                       for key in ("cancellation", "peer_continuation", "followup")), evidence
            if read_mode == "partial":
                assert selected_reads == ["first"]
        else:
            assert selected.generation_chunks == 8 and selected.stream_terminated
            assert not selected.intentional and selected.client_cancelled_ms is None
            if read_mode == "done_only_cr":
                assert selected.status == "failed" and selected.error == "response ended without a completion finish reason"
            else:
                assert selected.status == "completed" and selected.valid
                assert selected.finish_reason == "length" and selected.completion_tokens == 8
            assert evidence["status"] == evidence["cancellation"]["status"] == "inconclusive"
            assert not evidence["cancellation"]["request_ids"]

    run_virtual(monkeypatch, run())


@pytest.mark.parametrize(("reasoning_delay", "hold_slot"), [(.01, .01), (.09, .01), (.01, .3)])
def test_before_output_rechecks_generation_when_a_slot_opens(monkeypatch, reasoning_delay, hold_slot):
    import httpx
    from giraffe.client import LLMClient
    from giraffe.models import LoadTestSettings
    from giraffe.multi_user import run_window
    from giraffe.multi_user_metrics import summarize
    from tests.test_review26_phase4_accuracy import run_virtual

    class Stream(httpx.AsyncByteStream):
        def __init__(self, reasoning):
            self.reasoning = reasoning

        async def __aiter__(self):
            if self.reasoning:
                await asyncio.sleep(reasoning_delay)
                yield b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n\n'
                await asyncio.sleep(.05)
            else:
                await asyncio.sleep(.03)
            yield b'data: {"choices":[{"delta":{"content":"K7P4"}}]}\n\n'
            await asyncio.sleep(hold_slot)
            yield b'data: {"choices":[{"delta":{"content":" 42"},"finish_reason":"length"}]}\n\n'
            yield b'data: [DONE]\n\n'

    async def run():
        cfg = settings(levels=[3], duration_seconds=.25, think_time_seconds={"values": [1]},
            profile=PROFILE, scenarios=["noisy_neighbors"], noisy_neighbors={
                "heavy_user_ids": ["user-03"], "heavy_profiles": ["long_short"],
                "triggers": ["before_output"]},
            pass_criteria={"max_first_answer_ms": 70}).model_copy(update={"concurrency": 2})
        target = cfg.targets[0]
        state = runner._Run(cfg, None, None)
        client = LLMClient(target, cfg, max_connections=2)
        requests = 0

        async def respond(request):
            nonlocal requests
            requests += 1
            return httpx.Response(200, stream=Stream(reasoning=requests == 2))

        observation = _planned_observation(cfg.multi_user)
        window = observation["windows"][-1]
        calibration = {(size, "short_answer"): {"filler_chars": 100} for size in ("short", "long")}
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            client._client = transport
            await run_window(state, target, client, cfg.multi_user, LoadTestSettings(profile=cfg.multi_user.profile),
                             calibration, window)
        records = {r.workload["user_id"]: r for r in state.records
                   if r.workload["request_sequence"] == 1}
        reasoning = records["user-02"]
        assert reasoning.first_reasoning_ms is not None
        first_generation = reasoning.timing_start_ms + reasoning.first_reasoning_ms
        first_answer = reasoning.timing_start_ms + reasoning.first_output_ms
        first_slot_open = records["user-01"].completed_ms
        if hold_slot == .3:
            assert first_generation < first_answer < window["end_ms"] < first_slot_open
            assert requests == 2
            heavy = summarize(observation, state.records)["windows"][-1]["users"][-1]
            assert heavy["user_id"] == "user-03"
            assert heavy["pending_wait"] is None
            assert heavy["status"] == "inconclusive"
        elif reasoning_delay == .01:
            assert first_generation < first_slot_open <= first_answer
            assert "user-03" not in records
        else:
            heavy = records["user-03"]
            assert heavy.workload["trigger_peer_ids"] == ["user-02"]
            assert first_slot_open <= heavy.workload["trigger_ms"] < first_generation

    run_virtual(monkeypatch, run())


def matched_observation(trigger="during_stream", heavy_profile="long_short"):
    cfg = settings(levels=[2], duration_seconds=1, scenarios=["noisy_neighbors"], profile=PROFILE,
        noisy_neighbors={"heavy_user_ids": ["user-02"], "heavy_profiles": [heavy_profile],
                         "triggers": [trigger], "max_slowdown": 3})
    source = _planned_observation(cfg.multi_user)
    records = []
    for offset, window in zip((0, 1000), source["windows"]):
        window.update(start_ms=offset, end_ms=offset+1000, planned_end_ms=offset+1000,
                      duration_seconds=1, cut_short=False, reason=None)
        for user in window["users"]:
            heavy = user["user_id"] == "user-02"
            dispatch = offset + (120 if trigger == "before_output" else 170) if heavy else offset+100
            end = offset+400 if window["role"] == "probe" else offset+300
            first = 20 if heavy and heavy_profile.endswith("_long") else 100 if heavy else 50
            if not heavy and trigger == "before_output" and offset:
                first = 100
            item = record(user["user_id"], identity=f"{offset}-{user['user_id']}", dispatch=dispatch,
                end=end, first_output_ms=first, stream=True, answer_reads=5, answer_chunks=5,
                last_output_ms=end-dispatch-10, stream_end_ms=end-dispatch,
                input_tokens=256 if heavy and heavy_profile.startswith("long") else 64,
                tokens=32 if heavy and heavy_profile.endswith("long") else 16,
                requested_input_tokens=256 if heavy and heavy_profile.startswith("long") else 64,
                requested_output_tokens=32 if heavy and heavy_profile.endswith("long") else 16,
                max_stream_gap_ms=50 if offset else 20)
            item.request_body_sent_ms = dispatch
            item.workload.update(window_id=window["id"], profile=user["profile"], role=window["role"],
                                 matched_work_sequence=1, matched_work_sha256="same", think_time_seconds=0,
                                 request_sequence=1+offset//1000)
            if heavy:
                item.workload.update(trigger=trigger, trigger_ms=dispatch, trigger_peer_ids=["user-01"])
            user.update(activated_ms=offset, ready_count=1, dispatched_count=1, request_ids=[item.id])
            records.append(item)
    return source, records


@pytest.mark.parametrize("trigger", ["before_output", "during_stream"])
@pytest.mark.parametrize("profile", ["long_short", "short_long", "long_long"])
def test_matched_interference_requires_actual_trigger_and_lengths(trigger, profile):
    from giraffe.multi_user_metrics import summarize
    source, records = matched_observation(trigger, profile)
    result = summarize(source, records)
    evidence = result["windows"][1]["interference"]
    assert evidence["status"] == "pass", evidence
    user, = evidence["users"]
    assert user["matched_samples"] == 1
    assert user["slowdown"] == 1.5
    assert user["stream_gap_change_ms"] == 30
    assert user["control_request_ids"] == [records[0].id]
    assert user["probe_request_ids"] == [records[1].id]
    assert user["heavy_request_ids"] == [records[2].id]


def test_fairness_threshold_requires_an_eligible_window():
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation()
    source["settings"]["pass_criteria"]["max_throughput_ratio"] = 1
    summary = summarize(source, records)
    assert summary["windows"][1]["interference"]["status"] == "pass"
    assert summary["status"] == "inconclusive"


@pytest.mark.parametrize("field", ["first_token_ms", "first_reasoning_ms"])
def test_before_output_cannot_match_after_generation_started(field):
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation("before_output")
    setattr(records[1], field, 10)
    user, = summarize(source, records)["windows"][1]["interference"]["users"]
    assert user["status"] == "inconclusive"
    assert user["matched_samples"] == 0


def test_interference_requires_heavy_prefill_overlap_before_reasoning_starts():
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation("before_output")
    # The competitor starts reasoning in less than the minimum overlap time.
    # Waiting until its first answer would count reasoning as prefill.
    records[2].first_reasoning_ms = .5
    user, = summarize(source, records)["windows"][1]["interference"]["users"]
    assert user["status"] == "inconclusive"
    assert user["matched_samples"] == 0


@pytest.mark.parametrize("sent", [None, 1200, 1205])
def test_before_output_requires_heavy_upload_before_short_generation(sent):
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation("before_output")
    # Heavy runner admission was before the short user's first output at 1200,
    # but its connection lease or upload may have delayed the actual send.
    records[2].request_body_sent_ms = sent
    user, = summarize(source, records)["windows"][1]["interference"]["users"]
    assert user["status"] == "inconclusive"
    assert user["matched_samples"] == 0


@pytest.mark.parametrize(("completion_counts", "status"), [
    ((16, 16), "pass"), ((16, 32), "inconclusive"), ((16, None), "inconclusive"),
])
def test_interference_qualifies_total_generation_with_equal_answer_lengths(completion_counts, status):
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation()
    for request, total in zip(records[:2], completion_counts):
        request.completion_tokens = total
    user, = summarize(source, records)["windows"][1]["interference"]["users"]
    assert user["status"] == status
    assert user["matched_samples"] == (1 if status == "pass" else 0)


def test_interference_only_matches_selected_peer_and_half_open_trigger_interval():
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation("before_output")
    control, probe, heavy = records
    # The probe is eligible from dispatch at 1100 until first output at 1200.
    def competitor(identity, at, peer="user-01"):
        result = heavy.model_copy(deep=True, update={"id": identity})
        result.workload.update(trigger_ms=at, trigger_peer_ids=[peer])
        return result

    candidates = [competitor("too-late", 1200), competitor("different-peer", 1140, "user-99"),
                  heavy, competitor("at-dispatch", 1100), competitor("too-early", 1099)]
    user, = summarize(source, [control, probe, *candidates])["windows"][1]["interference"]["users"]
    assert user["status"] == "pass"
    assert user["matched_samples"] == 1
    assert user["heavy_request_ids"] == sorted([heavy.id, "at-dispatch"])


@pytest.mark.parametrize("missing", ["overlap", "trigger", "work", "length", "usage", "samples", "budget", "timing"])
def test_incomplete_interference_never_passes(missing):
    from giraffe.multi_user_metrics import summarize
    source, records = matched_observation()
    if missing == "overlap":
        records[2].first_output_ms = 0
    elif missing == "trigger":
        records[2].workload["trigger_ms"] = 1400
    elif missing == "work":
        records[1].workload["matched_work_sha256"] = "different"
    elif missing == "length":
        records[1].output_tokens = 8
    elif missing == "usage":
        records[1].input_tokens = None
    elif missing == "samples":
        source["settings"]["pass_criteria"]["min_samples"] = 2
    elif missing == "budget":
        source["windows"][0]["cut_short"] = True
    else:
        source["scheduling_delay_windows"] = [{"start_ms": 1100, "end_ms": 1400}]
    evidence = summarize(source, records)["windows"][1]["interference"]
    assert evidence["status"] == "inconclusive"
    assert not evidence["qualified"]


@pytest.mark.parametrize(("cache_counts", "status"), [
    ((0, 64, 0), "inconclusive"),
    ((0, 0, 256), "inconclusive"),
    ((0, 0, 120), "inconclusive"),
    ((0, 0, 64), "pass"),
    ((16, 16, 16), "pass"),
])
def test_interference_qualifies_cached_input_work(cache_counts, status):
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation()
    for request, cached in zip(records, cache_counts):
        request.cached_prompt_tokens = cached
    user, = summarize(source, records)["windows"][1]["interference"]["users"]
    assert user["status"] == status
    assert user["matched_samples"] == (1 if status == "pass" else 0)


def test_equal_medians_do_not_hide_incomparable_matched_pairs():
    from giraffe.multi_user_metrics import summarize

    source, records = matched_observation()
    source["settings"]["pass_criteria"]["min_samples"] = 2
    second_group = []
    for original in records:
        item = original.model_copy(deep=True)
        item.id += "-2"
        item.dispatch_ms += 500
        item.completed_ms += 500
        item.timing_start_ms += 500
        item.request_body_sent_ms += 500
        item.workload["ready_ms"] += 500
        item.workload["matched_work_sequence"] = 2
        if "trigger_ms" in item.workload:
            item.workload["trigger_ms"] += 500
        second_group.append(item)
    records[0].output_tokens = second_group[1].output_tokens = 4
    user, = summarize(source, records + second_group)["windows"][1]["interference"]["users"]
    assert user["status"] == "inconclusive"
    assert user["matched_samples"] == 0


def test_short_user_failure_survives_unqualified_comparison():
    from giraffe.multi_user_metrics import summarize
    source, records = matched_observation()
    records[1].status, records[1].valid = "failed", False
    summary = summarize(source, records)
    assert summary["status"] == "fail"
    assert summary["windows"][1]["users"][0]["status"] == "fail"
    assert summary["windows"][1]["interference"]["status"] == "inconclusive"


def test_unstarted_noisy_neighbor_plan_is_inconclusive():
    from giraffe.multi_user_metrics import summarize
    cfg = settings(levels=[2], scenarios=["noisy_neighbors"])
    summary = summarize(_planned_observation(cfg.multi_user), [])
    assert summary["status"] == "inconclusive"
    comparisons = [window["interference"] for window in summary["windows"] if "interference" in window]
    assert comparisons and all(item["status"] == "inconclusive" and not item["qualified"] for item in comparisons)


@pytest.mark.parametrize("missing", ["start_ms", "end_ms", "activated_ms"])
def test_matched_interference_with_missing_exposure_is_inconclusive(missing):
    from giraffe.multi_user_metrics import summarize
    source, records = matched_observation()
    probe = source["windows"][1]
    (probe["users"][0] if missing == "activated_ms" else probe)[missing] = None
    evidence = summarize(source, records)["windows"][1]["interference"]
    assert evidence["status"] == "inconclusive"
    assert not evidence["qualified"]


def test_http_budget_exhausted_before_probe_preserves_partial_result():
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[2], duration_seconds=.3, scenarios=["noisy_neighbors"], profile=PROFILE,
            noisy_neighbors={"triggers": ["during_stream"], "heavy_profiles": ["long_short"]}).model_copy(update={
                "max_requests": 1, "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
            await execute(state, cfg.targets[0], client)
        check = result(state, cfg.targets[0])
        assert check.status == "inconclusive"
        probe = check.metrics["multi_user"]["windows"][1]
        assert probe["start_ms"] is None
        assert probe["interference"]["status"] == "inconclusive"
        assert len(state.records) == 1 and check.evidence_ids == [state.records[0].id]
        assert state.slots.qsize() == 2

    with FakeEndpoint(first_token_delay=.005, chunk_delay=.001) as endpoint:
        asyncio.run(run(endpoint))


def test_http_partial_cancellation_cannot_pass_without_user_criteria():
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[2], duration_seconds=10, scenarios=["cancellation"], profile=PROFILE,
            pass_criteria={"max_error_percent": None, "min_samples": 1},
            cancellation={"user_id": "user-02", "cancel_after_chunks": 2}).model_copy(update={
                "max_requests": 4, "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
            await execute(state, cfg.targets[0], client)
        check = result(state, cfg.targets[0])
        window = check.metrics["multi_user"]["windows"][0]
        evidence = window["cancellation"]
        assert window["cut_short"]
        assert all(evidence[key]["status"] == "pass" for key in ("cancellation", "peer_continuation", "followup"))
        assert evidence["status"] == window["status"] == check.status == "inconclusive"

    with FakeEndpoint(first_token_delay=.02, chunk_delay=.02) as endpoint:
        asyncio.run(run(endpoint))


@pytest.mark.parametrize("delayed", [False, True])
def test_cancellation_followup_does_not_substitute_for_peer_overlap(delayed):
    from giraffe.multi_user_metrics import summarize
    from tests.test_multi_user_metrics import observation
    cancelled = record("user-01", identity="cancelled", status="cancelled", valid=False, intentional=True,
        end=100, answer_chunks=2, generation_chunks=2, termination_reason="generation_chunk_limit",
        client_cancelled_ms=90, client_cancelled_at_ns=1, finish_reason=None)
    cancelled.workload["cancellation_role"] = "selected"
    followup = record("user-01", identity="followup", dispatch=200, end=300)
    followup.workload["cancellation_role"] = "followup"
    peer = record("user-02", identity="peer", dispatch=200, end=400)
    source = observation([cancelled, followup, peer])
    source["windows"][0].update(scenario="cancellation", selected_user_id="user-01", role="cancellation")
    original = deepcopy(source)
    result = summarize(source, [cancelled, followup, peer], legacy_max_lag_ms=1000 if delayed else 0)
    evidence = result["windows"][0]["cancellation"]
    assert evidence["cancellation"]["status"] == ("inconclusive" if delayed else "pass")
    assert evidence["followup"]["status"] == "pass"
    assert evidence["peer_continuation"]["status"] == "inconclusive"
    assert result["status"] == "inconclusive"
    assert source == original


@pytest.mark.parametrize(("peer_overlap", "error_limit", "expected"), [
    (True, 50, "fail"), (False, 50, "inconclusive"), (False, 0, "fail"),
])
def test_failed_cancellation_followup_requires_peer_overlap(peer_overlap, error_limit, expected):
    from tests.test_multi_user_metrics import observation

    cancelled = record("user-01", identity="cancelled", status="cancelled", valid=False, intentional=True,
        end=100, answer_chunks=2, generation_chunks=2, termination_reason="generation_chunk_limit",
        client_cancelled_ms=90, client_cancelled_at_ns=1, finish_reason=None)
    cancelled.workload["cancellation_role"] = "selected"
    followup = record("user-01", identity="followup", dispatch=200, end=300)
    failed = record("user-01", identity="failed", dispatch=350, end=500, status="failed", valid=False)
    for item in (followup, failed):
        item.workload["cancellation_role"] = "followup"
    peer = record("user-02", identity="peer", dispatch=0 if peer_overlap else 200,
                  end=400 if peer_overlap else 600, stream=True, answer_reads=2,
                  answer_chunks=2, last_output_ms=200)
    records = [cancelled, followup, failed, peer]
    source = observation(records, criteria={"max_error_percent": error_limit})
    source["windows"][0].update(scenario="cancellation", selected_user_id="user-01", role="cancellation")
    cfg = settings(levels=[2], scenarios=["cancellation"], cancellation={"user_id": "user-01"})
    target = cfg.targets[0]
    state = runner._Run(cfg, None, None)
    state.records.extend(records)
    state.observations[target.name]["multi_user"] = source

    check = result(state, target)
    assert check.status == expected
    assert check.metrics["multi_user"]["failed_user_windows"] == (0 if error_limit else 1)
    evidence = check.metrics["multi_user"]["windows"][0]["cancellation"]
    assert evidence["status"] == ("fail" if peer_overlap else "inconclusive")
    assert evidence["cancellation"]["status"] == "pass"
    assert evidence["peer_continuation"]["status"] == ("pass" if peer_overlap else "inconclusive")
    assert evidence["followup"]["status"] == "fail"
    if peer_overlap:
        assert check.summary == "Same-user cancellation follow-up failed for user-01."


def test_http_global_stop_cancels_all_users_and_retains_partial_once():
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[2], duration_seconds=60, scenarios=["cancellation"], profile=PROFILE,
                       cancellation={"cancel_after_chunks": 1000}).model_copy(update={
            "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        events = []
        state = runner._Run(cfg, events.append, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
            task = asyncio.create_task(execute(state, cfg.targets[0], client))
            async with asyncio.timeout(3):
                while len(endpoint.requests) < 2:
                    await asyncio.sleep(.005)
            state.stop.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(state.records) == len({r.id for r in state.records}) == 2
        assert all(r.status == "cancelled" and r.termination_reason == "user_stop" for r in state.records)
        assert any(r.workload.get("cancellation_role") == "selected" for r in state.records)
        assert not any(r.intentional for r in state.records)
        completed = [event for event in events if event["event"] == "request_completed"]
        assert len(completed) == 2
        assert all(not event["intentional"] and not event["request"]["intentional"] for event in completed)
        window = result(state, cfg.targets[0]).metrics["multi_user"]["windows"][0]
        assert all(user["outcomes"]["cancelled"] == 1 and user["outcomes"]["intentional_cancelled"] == 0
                   for user in window["users"])
        assert state.slots.qsize() == 2
        assert not any(r.workload.get("cancellation_role") == "followup" for r in state.records)

    with FakeEndpoint(first_token_delay=.02, chunk_delay=.05) as endpoint:
        asyncio.run(run(endpoint))


def test_cancel_selected_request_before_admission_sends_nothing_and_peers_survive():
    from giraffe.client import LLMClient
    from giraffe.models import RequestSpec, Target
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[2], scenarios=["cancellation"]).model_copy(update={
            "concurrency": 1, "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        peer_started = asyncio.Event()
        spec = RequestSpec(fixture_id="peer", scenario="multi_user_2_cancellation", check_ids=["multi_user"],
                           messages=[{"role": "user", "content": "The box label is K7P4. Write the box label first, then continue a numbered list."}],
                           max_tokens=16, workload={"user_id": "user-01"})
        async with LLMClient(cfg.targets[0], cfg, max_connections=1) as client:
            peer = asyncio.create_task(state.request(cfg.targets[0], client, spec, event=peer_started))
            await asyncio.wait_for(peer_started.wait(), 2)
            selected = asyncio.create_task(state.request(cfg.targets[0], client,
                spec.model_copy(update={"fixture_id": "selected", "cancel_after_chunks": 2,
                                        "workload": {"user_id": "user-02"}})))
            await asyncio.sleep(0)
            selected.cancel()
            with pytest.raises(asyncio.CancelledError) as captured:
                await selected
            assert getattr(captured.value, "giraffe_record", None) is None
            assert (await peer).status == "completed"
        assert state.attempted == len(endpoint.requests) == len(state.records) == 1
        assert state.slots.qsize() == 1

    with FakeEndpoint(first_token_delay=.01, chunk_delay=.01) as endpoint:
        asyncio.run(run(endpoint))


@pytest.mark.parametrize("trigger", ["before_output", "during_stream"])
@pytest.mark.parametrize("profile", ["long_short", "short_long"])
def test_http_noisy_neighbors_replay_short_work_and_observe_injected_slowdown(trigger, profile, monkeypatch):
    from threading import local

    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint
    from giraffe import local_tokenizer

    async def tokenizer_worker(_name, **_kwargs):
        class Worker:
            returncode = None
            def __init__(self):
                self.stdin = self.stdout = self
                self.rows = [{"ready": True, "chat_template": True, "token_count_scope": "chat_template"}]
            def write(self, line):
                messages = json.loads(line)["messages"]
                self.rows.append({"input_tokens": sum(len(m["content"].split()) for m in messages)+8})
            async def drain(self):
                pass
            async def readline(self):
                return (json.dumps(self.rows.pop(0))+"\n").encode()
            def kill(self):
                self.returncode = -9
            async def wait(self):
                return self.returncode
        return Worker()

    # Preparation uses the fixture endpoint's actual whitespace tokenizer;
    # all usage and stream timing still come from the HTTP response.
    monkeypatch.setattr(local_tokenizer, "_start_worker", tokenizer_worker)

    class ContendedEndpoint(FakeEndpoint):
        response = local()

        @property
        def chunk_delay(self):
            if self.response.heavy:
                return .004
            self.response.contended |= self.active > 1
            return .012 if self.response.contended else .004

        @chunk_delay.setter
        def chunk_delay(self, value):
            pass

        def _continue_numbered_list(self, prompt, answer, budget):
            # Slow the short peer that experiences contention. Keep the heavy
            # stream moving so each phase has several chances to trigger it.
            self.response.heavy = (budget >= PROFILE["long"]["output_tokens"]
                                   or len(prompt.split()) >= PROFILE["medium"]["input_tokens"])
            self.response.contended = False
            return " ".join([answer, *(str(i) for i in range(budget))])

    async def run(endpoint):
        # The short stream stays open long enough to admit a during-stream probe.
        # Several matched pairs keep one delayed control from dominating the median.
        cfg = settings(levels=[2], duration_seconds=2.25, scenarios=["noisy_neighbors"], profile=PROFILE,
            pass_criteria={"min_samples": 3},
            noisy_neighbors={"heavy_user_ids": ["user-02"], "heavy_profiles": [profile],
                             "triggers": [trigger], "max_slowdown": 1.1}).model_copy(update={
                "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
            await asyncio.wait_for(execute(state, cfg.targets[0], client), 8)
        controls = {r.workload["matched_work_sequence"]: r for r in state.records if r.workload.get("role") == "control"}
        probes = [r for r in state.records if r.workload.get("role") == "probe" and r.workload["user_id"] == "user-01"]
        assert probes and all(r.workload["matched_work_sha256"] == controls[r.workload["matched_work_sequence"]].workload["matched_work_sha256"] for r in probes)
        assert probes[0].workload["request_sequence"] > probes[0].workload["matched_work_sequence"]
        completed_probes = [r for r in state.records if r.workload.get("role") == "probe" and r.valid]
        assert all(r.timing_start_ms <= r.request_body_sent_ms <= r.completed_ms for r in completed_probes)
        evidence = result(state, cfg.targets[0]).metrics["multi_user"]["windows"][1]["interference"]
        user, = evidence["users"]
        assert user["matched_samples"] >= 3, evidence
        assert user["slowdown"] > 1.1, evidence
        assert evidence["status"] == "fail", user["reasons"]
        assert user["heavy_request_ids"]
        assert state.slots.qsize() == 2

    with ContendedEndpoint(first_token_delay=.05) as endpoint:
        asyncio.run(run(endpoint))


@pytest.mark.parametrize("scenario", ["noisy_neighbors", "cancellation"])
def test_http_missing_scenario_opportunity_is_inconclusive(scenario):
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    async def run(endpoint):
        cfg = settings(levels=[2], duration_seconds=.15, scenarios=[scenario], profile=PROFILE,
            noisy_neighbors={"triggers": ["during_stream"], "heavy_profiles": ["long_short"]},
            cancellation={"cancel_after_chunks": 1000}).model_copy(update={
                "targets": [Target(name="service", model="fake-model", url=endpoint.url)]})
        state = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg, max_connections=2) as client:
            await execute(state, cfg.targets[0], client)
        summary = result(state, cfg.targets[0]).metrics["multi_user"]
        window = summary["windows"][-1]
        evidence = window["interference" if scenario == "noisy_neighbors" else "cancellation"]
        assert evidence["status"] == "inconclusive"
        assert summary["status"] == "inconclusive"
        assert not any(r.status == "cancelled" for r in state.records)

    with FakeEndpoint(first_token_delay=.005, chunk_delay=.001) as endpoint:
        asyncio.run(run(endpoint))


def test_warmup_and_preview_include_all_selected_scenario_windows():
    from giraffe.planning import request_plan
    cfg = settings(levels=[2], duration_seconds=2, warmup_seconds=1,
                   scenarios=["noisy_neighbors", "cancellation"],
                   noisy_neighbors={"triggers": ["during_stream"], "heavy_profiles": ["long_short"]})
    windows = _planned_observation(cfg.multi_user)["windows"]
    assert [w["phase"] for w in windows] == ["warmup", "measurement"] * 3
    assert len({w["id"] for w in windows}) == 6
    row = next(row for row in request_plan(cfg)["rows"] if row["id"] == "multi_user_2")
    assert row["window_count"] == 6 and row["configured_seconds"] == 9
