"""Timeout warning wiring with the runner's socket-free slow transport."""
import pytest

from giraffe import runner
from giraffe.models import RequestSpec
from giraffe.timeout_warning import format_timeout_warning
from tests.test_runner import FakeClient, client, config  # noqa: F401 - shared transport fixture


def spec(*, scenario="warm", checks=None, **kwargs):
    return RequestSpec(fixture_id="warning", scenario=scenario,
                       check_ids=checks or ["serving"], messages=[{"role": "user", "content": "hi"}],
                       **kwargs)


@pytest.mark.asyncio
async def test_slow_requests_warn_before_finish_and_keep_partial_answer(monkeypatch):
    monkeypatch.setattr(FakeClient, "delay", .5)
    cfg = config(request_timeout_seconds=.2, samples=3)
    events = []
    run = runner._Run(cfg, events.append, None)
    transport = FakeClient(cfg.targets[0], cfg)
    for _ in range(3):
        await run.request(cfg.targets[0], transport, spec())
    warnings = [event for event in events if event["event"] == "timeout_warning"]
    assert len(warnings) == 1
    assert warnings[0]["timeout_warning"]["answer_started"] == 3
    assert warnings[0]["timeout_warning"]["timed_out"] == 3
    assert events[-1]["event"] == "request_completed"
    assert run.observations["service"]["timeout_warnings"] == run.timeout_watch.warnings
    assert all(record.termination_reason == "request_timeout" for record in run.records)


@pytest.mark.asyncio
async def test_deadline_probes_do_not_warn(monkeypatch):
    monkeypatch.setattr(FakeClient, "delay", .5)
    cfg = config(request_timeout_seconds=.2)
    run = runner._Run(cfg, None, None)
    transport = FakeClient(cfg.targets[0], cfg)
    for _ in range(3):
        await run.request(cfg.targets[0], transport, spec(scenario="deadline_probe"), timeout_override=.01)
    assert run.timeout_watch.warnings == []


@pytest.mark.parametrize("scenario,checks,options,source,check", [
    ("initial", ["access", "serving"], {"access": {"request_timeout_seconds": 2}}, "check", "access"),
    ("warm", ["serving", "first_output"], {"serving": {"request_timeout_seconds": 2}, "first_output": {"request_timeout_seconds": 2}}, "check", "serving"),
    ("warm", ["serving", "first_output"], {"first_output": {"request_timeout_seconds": 2}}, "check", "first_output"),
    ("warm", ["serving"], {}, "run", None),
    ("generation", ["generation"], {"generation": {"request_timeout_seconds": 2}}, "check", "generation"),
    ("json_c1", ["json"], {"json": {"request_timeout_seconds": 2}}, "check", "json"),
])
def test_timeout_provenance_follows_explicit_override_even_if_equal(scenario, checks, options, source, check):
    cfg = config(checks=checks, test_options=options, request_timeout_seconds=2)
    run = runner._Run(cfg, None, None)
    assert run.timeout_origin(spec(scenario=scenario, checks=checks, request_timeout_seconds=2))[:2] == (source, check)


def test_load_timeout_provenance_and_migrated_config_path():
    cfg = config(load_test={"request_timeout_seconds": 3, "migrated_tests": [
        {"source": "arrivals", "settings": {"request_timeout_seconds": 4}}]})
    run = runner._Run(cfg, None, None)
    assert run.timeout_origin(spec(checks=["load"], request_timeout_seconds=3)) == ("load", None, "load_test.request_timeout_seconds")
    assert run.timeout_origin(spec(checks=["load"], request_timeout_seconds=4, load_point={"source": "legacy_0_arrivals"})) == (
        "load", None, "load_test.migrated_tests.0.settings.request_timeout_seconds")
    assert run.timeout_origin(spec(checks=["load"], request_timeout_seconds=2)) == ("run", None, None)
    assert run.timeout_origin(spec(checks=["load"], request_timeout_seconds=4, deadline_source="load",
                                  load_point={"source": "legacy_0_arrivals"})) == (
        "load", None, "load_test.migrated_tests.0.settings.request_timeout_seconds")


@pytest.mark.asyncio
async def test_finished_report_contains_shared_warning(monkeypatch):
    monkeypatch.setattr(FakeClient, "delay", .02)
    events = []
    report = await runner.run_suite(config(request_timeout_seconds=.005, samples=3), progress=events.append)
    warning = [event["timeout_warning"] for event in events if event["event"] == "timeout_warning"][-1]
    assert format_timeout_warning(warning) in report.warnings
    assert report.manifest["fixture_pack"]["builtin_sha256"] == (
        "d23bf06ae324964c23c19b2ffed1327d752cb7f45f47ee93d38c14d9de74e515")
    assert next(i for i, event in enumerate(events) if event["event"] == "timeout_warning") < next(
        i for i, event in enumerate(events) if event["event"] == "run_finished")


@pytest.mark.asyncio
async def test_real_stream_partial_answer_is_counted(monkeypatch):
    import httpx
    from giraffe.client import LLMClient
    from tests.test_client import Chunks, event
    from tests.test_runner import mock_inference_transport

    streams = []
    def handler(request):
        stream = Chunks([event("partial answer"), .5])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    mock_inference_transport(monkeypatch, handler)
    cfg = config(request_timeout_seconds=.02)
    run = runner._Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as transport:
        for _ in range(3):
            await run.request(cfg.targets[0], transport, spec())
    assert run.timeout_watch.warnings[0]["answer_started"] == 3
    assert all(record.answer_chars > 0 for record in run.records)
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.asyncio
async def test_socket_slow_endpoint_emits_warning_before_run_finished(monkeypatch, partial):
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    monkeypatch.setattr(runner, "LLMClient", LLMClient)
    with FakeEndpoint(first_token_delay=.005 if partial else .5, chunk_delay=.5 if partial else .002) as endpoint:
        events = []
        report = await runner.run_suite(config(targets=[Target(name="service", model="fake-model", url=endpoint.url)],
            request_timeout_seconds=.2, samples=3), progress=events.append)
    warning = [event["timeout_warning"] for event in events if event["event"] == "timeout_warning"][-1]
    assert warning["timed_out"] >= 3
    assert warning["answer_started"] >= 3 if partial else warning["no_output"] >= 3
    assert format_timeout_warning(warning) in report.warnings
    assert next(i for i, event in enumerate(events) if event["event"] == "timeout_warning") < next(
        i for i, event in enumerate(events) if event["event"] == "run_finished")


@pytest.mark.asyncio
async def test_socket_deadline_probe_timeouts_do_not_warn(monkeypatch):
    from giraffe.client import LLMClient
    from giraffe.models import Target
    from tests.fake_endpoint import FakeEndpoint

    with FakeEndpoint(first_token_delay=.5) as endpoint:
        cfg = config(targets=[Target(name="service", model="fake-model", url=endpoint.url)],
                     request_timeout_seconds=.2)
        run = runner._Run(cfg, None, None)
        async with LLMClient(cfg.targets[0], cfg) as transport:
            for _ in range(3):
                await run.request(cfg.targets[0], transport, spec(scenario="deadline_probe"), timeout_override=.05)
    assert len(run.records) == 3
    assert all(record.status == "timeout" for record in run.records)
    assert not run.timeout_watch.warnings


def test_effective_fixture_resolution_carries_provenance_without_value_comparison():
    cfg = config(checks=["access", "serving", "first_output", "json"], request_timeout_seconds=2,
                 test_options={"first_output": {"request_timeout_seconds": 2},
                               "json": {"request_timeout_seconds": 2}})
    initial = runner.builtin_fixtures(runner._response_config(cfg, initial=True))["short"][0]
    warm = runner.builtin_fixtures(runner._response_config(cfg))["short"][0]
    check = runner.builtin_fixtures(runner._check_config(cfg, "json"))["json"][0]
    assert initial.deadline_source == "run" and initial.timeout_check is None
    assert warm.deadline_source == "check" and warm.timeout_check == "first_output"
    assert check.deadline_source == "check" and check.timeout_check == "json"
    assert initial.request_timeout_seconds == warm.request_timeout_seconds == check.request_timeout_seconds == 2


@pytest.mark.asyncio
async def test_real_reasoning_only_stream_is_not_answer_started(monkeypatch):
    import httpx
    from giraffe.client import LLMClient
    from tests.test_client import Chunks, event
    from tests.test_runner import mock_inference_transport

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            stream=Chunks([event(reasoning="still thinking"), .5]))

    mock_inference_transport(monkeypatch, handler)
    cfg = config(request_timeout_seconds=.02)
    run = runner._Run(cfg, None, None)
    async with LLMClient(cfg.targets[0], cfg) as transport:
        for _ in range(3):
            await run.request(cfg.targets[0], transport, spec())
    payload = run.timeout_watch.warnings[0]
    assert payload["reasoning_only"] == 3 and payload["answer_started"] == 0
    assert all(record.first_token_ms is not None and record.first_output_ms is None
               for record in run.records)


@pytest.mark.asyncio
async def test_custom_fixture_timeout_names_file_and_field(monkeypatch, tmp_path):
    import json

    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"fixture_id": "slow-case", "scorer": "none",
        "messages": [{"role": "user", "content": "hi"}], "request_timeout_seconds": .001}]))
    monkeypatch.setattr(FakeClient, "delay", .02)
    report = await runner.run_suite(config(checks=["correctness"], samples=30, concurrency=1,
                                          custom_fixtures=str(path)))
    payload = report.observations["targets"]["service"]["timeout_warnings"][0]
    assert payload["cli_flag"] is None
    assert str(path) in payload["setting"] and "slow-case" in payload["setting"]
    assert payload["setting_field"] == "custom_fixtures"
    assert format_timeout_warning(payload) in report.warnings


def test_custom_fixture_provenance_for_other_consumers():
    run = runner._Run(config(custom_fixtures="cases.json"), None, None)
    source, check, setting = run.timeout_origin(spec(checks=["scale_down"],
        request_timeout_seconds=1, workload={"request_source": "custom_fixtures"}))
    assert (source, check) == ("run", None)
    assert setting == "custom_fixtures (cases.json, fixture warning).request_timeout_seconds"


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("reason", ["request_deadline", "read_timeout"])
async def test_real_client_own_timeout_emits_warning(monkeypatch, partial, reason):
    import httpx
    from giraffe.client import LLMClient
    from tests.test_client import event
    from tests.test_runner import mock_inference_transport

    class TimeoutStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if partial:
                yield event("partial answer")
            if reason == "read_timeout":
                raise httpx.ReadTimeout("read stalled")
            import asyncio
            await asyncio.sleep(.5)

    class EarlierClientDeadline(LLMClient):
        async def execute(self, request, **kwargs):
            # Give the real client's deadline time to fire before the runner's guard.
            return await super().execute(request.model_copy(update={"request_timeout_seconds": .2}), **kwargs)

    mock_inference_transport(monkeypatch, lambda request: httpx.Response(200,
        headers={"content-type": "text/event-stream"}, stream=TimeoutStream()))
    cfg = config(request_timeout_seconds=2)
    events = []
    run = runner._Run(cfg, events.append, None)
    async with EarlierClientDeadline(cfg.targets[0], cfg) as transport:
        for _ in range(3):
            await run.request(cfg.targets[0], transport, spec())
    assert [record.termination_reason for record in run.records] == [reason] * 3
    warning = next(event["timeout_warning"] for event in events if event["event"] == "timeout_warning")
    assert warning["timed_out"] == 3
    assert warning["answer_started"] == (3 if partial else 0)
    assert warning["no_output"] == (0 if partial else 3)


def test_provenance_does_not_serialize_or_mutate_cached_fixtures(monkeypatch):
    from giraffe.deployment import fixture_hash
    cfg = config(test_options={"serving": {"request_timeout_seconds": 2}})
    cached = runner._builtin_fixtures(cfg)
    before = fixture_hash({key: [spec.model_dump() for spec in rows] for key, rows in cached.items()})
    monkeypatch.setattr(runner, "_builtin_fixtures", lambda config: cached)
    stamped = runner.builtin_fixtures(runner._check_config(cfg, "serving"))
    assert stamped["short"][0] is not cached["short"][0]
    assert cached["short"][0].deadline_source is None
    assert fixture_hash({key: [spec.model_dump() for spec in rows] for key, rows in stamped.items()}) == before
    assert not {"deadline_source", "timeout_check", "timeout_setting"}.intersection(stamped["short"][0].model_dump())


def test_unstamped_timeout_matches_actual_config_source():
    cfg = config(request_timeout_seconds=2, checks=["recovery", "generation"],
                 test_options={"recovery": {"request_timeout_seconds": 5},
                               "generation": {"request_timeout_seconds": 7}})
    run = runner._Run(cfg, None, None)
    assert run.timeout_origin(spec(scenario="sustained", checks=["recovery"], request_timeout_seconds=2)) == ("run", None, None)
    assert run.timeout_origin(spec(scenario="sustained", checks=["recovery"], request_timeout_seconds=5)) == ("check", "recovery", None)
    assert run.timeout_origin(spec(scenario="other", checks=["recovery", "generation"], request_timeout_seconds=7)) == ("check", "generation", None)
    assert run.timeout_origin(spec(scenario="sustained", checks=["recovery"])) == ("run", None, None)
    disabled = runner._Run(config(test_options={"recovery": {"request_timeout_seconds": 2}}), None, None)
    assert disabled.timeout_origin(spec(scenario="sustained", checks=["recovery"], request_timeout_seconds=2)) == ("run", None, None)


@pytest.mark.parametrize("consumer,source", [("correctness", "run"), ("capacity", "load"), ("load", "load")])
def test_pack_producer_carries_explicit_timeout_owner(consumer, source):
    from giraffe.request_pack import pack_specs
    cfg = config(checks=[consumer], request_source="request_pack",
        request_pack={"version": 1, "cases": [{"fixture_id": "one", "scorer": "none",
                      "messages": [{"role": "user", "content": "hi"}]}]})
    request = pack_specs(cfg)[0]
    assert request.deadline_source == source
    assert request.timeout_check is None
    assert "deadline_source" not in request.model_dump()


async def test_recovery_traffic_keeps_explicit_recovery_timeout_owner(monkeypatch):
    monkeypatch.setattr(FakeClient, "delay", .005)
    report = await runner.run_suite(config(checks=["recovery"],
        test_options={"recovery": {"request_timeout_seconds": .001}}, stop_after_errors=100))
    traffic = [record for record in report.requests if record.scenario == "sustained"]
    assert traffic and all(record.deadline_source == "check" and record.timeout_check == "recovery"
                           for record in traffic)


async def test_report_warning_refreshes_after_successes(monkeypatch):
    execute = FakeClient.execute
    count = 0

    async def first_three_timeout(self, spec, **kwargs):
        nonlocal count
        record = await execute(self, spec, **kwargs)
        count += 1
        if count <= 3:
            record.status = "timeout"
            record.termination_reason = "request_timeout"
            record.valid = False
        return record

    monkeypatch.setattr(FakeClient, "execute", first_three_timeout)
    events = []
    report = await runner.run_suite(config(samples=22, concurrency=1), progress=events.append)
    payload = report.observations["targets"]["service"]["timeout_warnings"][0]
    assert payload["timed_out"] == 3 and payload["finished"] == 23
    assert payload["update"] == 21
    assert payload == [event["timeout_warning"] for event in events if event["event"] == "timeout_warning"][-1]
    assert format_timeout_warning(payload) in report.warnings
    assert any("3 of 23 requests" in message for message in report.warnings)
