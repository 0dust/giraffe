"""Target inference accounting follows transport hand-off, not record order."""

import asyncio
import time

import httpx
import pytest

from giraffe import runner
from giraffe.client import LLMClient
from giraffe.models import RequestSpec, RunConfig, Target


def configuration(*names, **updates):
    return RunConfig(targets=[Target(name=name, model="model", url=f"http://{name}/v1")
                              for name in names or ("local",)],
                     checks=["access"], max_requests=20, stream=False, **updates)


def request_spec(scenario="initial"):
    return RequestSpec(fixture_id="answer", scenario=scenario, check_ids=["access"],
                       messages=[{"role": "user", "content": "Return 42"}], stream=False)


@pytest.fixture
def inference_posts(monkeypatch):
    posts = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(404)
        assert request.method == "POST"
        posts.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        })

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)))
    return posts


def assert_reconciles(run, posts):
    counts = run.request_counts()
    charged_sent = sum(run.charged_sent_counts.values())
    assert counts["sent"] == len(posts) == charged_sent + counts["budget_exempt"]
    assert counts["budget_counted"] >= charged_sent
    assert counts["sent"] == counts["budget_counted"] - counts["budget_counted_unsent"] + counts["budget_exempt"]
    for field in ("sent", "budget_counted", "budget_counted_unsent", "budget_exempt", "saved_observations"):
        assert counts[field] == sum(target[field] for target in counts["targets"].values())
    return counts


async def test_sent_counts_charged_and_exempt_at_real_transport(inference_posts):
    config = configuration()
    events = []
    run = runner._Run(config, events.append, None)
    target = config.targets[0]
    async with LLMClient(target, config) as client:
        await run.request(target, client, request_spec("readiness"), budget_exemption="restart_readiness")
        await run.request(target, client, request_spec("performance_evidence_probe"))
    counts = assert_reconciles(run, inference_posts)
    assert counts == {
        "sent": 2, "budget_counted": 1, "budget_exempt": 1, "saved_observations": 0,
        "budget_counted_unsent": 0, "complete": True, "available": True,
        "budget_exempt_by_reason": {"restart_readiness": 1},
        "targets": {"local": {"sent": 2, "budget_counted": 1, "budget_exempt": 1,
                              "saved_observations": 0, "budget_counted_unsent": 0,
                              "budget_exempt_by_reason": {"restart_readiness": 1}}},
    }
    completed = [event for event in events if event["event"] == "request_completed"]
    assert [(event["request_counts"]["sent"], event["request_counts"]["budget_counted"],
             event["request_counts"]["budget_exempt"]) for event in completed] == [(1, 0, 1), (2, 1, 1)]
    assert all(not {"budget_used", "sent", "budget_exempt"} & event.keys() for event in events)


async def test_shared_client_sent_observer_keeps_concurrent_exempt_reasons(inference_posts, monkeypatch):
    config = configuration(concurrency=2)
    run = runner._Run(config, None, None)
    target = config.targets[0]
    release_readiness = asyncio.Event()
    async with LLMClient(target, config) as client:
        execute = client.execute

        async def delayed_execute(spec, *, first_output_event=None, lifecycle_observer=None,
                                  _before_send=None):
            if spec.scenario == "readiness":
                await release_readiness.wait()
            record = await execute(spec, first_output_event=first_output_event,
                                   lifecycle_observer=lifecycle_observer, _before_send=_before_send)
            if spec.scenario == "initial":
                release_readiness.set()
            return record

        monkeypatch.setattr(client, "execute", delayed_execute)
        # Dispatch directly with scheduler-owned slots so both tasks share the client.
        exempt, charged = await asyncio.gather(
            run.request(target, client, request_spec("readiness"), slot_reserved=True,
                        budget_exemption="restart_readiness"),
            run.request(target, client, request_spec(), slot_reserved=True),
        )
    counts = assert_reconciles(run, inference_posts)
    assert exempt.sent and charged.sent
    assert (counts["sent"], counts["budget_counted"], counts["budget_exempt"]) == (2, 1, 1)
    assert counts["budget_exempt_by_reason"] == {"restart_readiness": 1}


async def test_restart_initial_remains_charged_and_a_saved_measurement(inference_posts):
    # restart_target alone marks an initial request as readiness internally, but
    # only the explicit budget_exempt path exempts a transport dispatch.
    config = configuration(restart_target="local")
    run = runner._Run(config, None, None)
    target = config.targets[0]
    async with LLMClient(target, config) as client:
        await run.request(target, client, request_spec())
    counts = assert_reconciles(run, inference_posts)
    assert (counts["sent"], counts["budget_counted"], counts["budget_exempt"],
            counts["saved_observations"]) == (1, 1, 0, 1)
    assert counts["budget_exempt_by_reason"] == {}


async def test_preparation_failure_refund_is_neither_sent_nor_counted(inference_posts, monkeypatch):
    config = configuration()
    run = runner._Run(config, None, None)
    target = config.targets[0]
    assert run.reserve_offer(target.name)
    assert run.budget_used == 1
    async with LLMClient(target, config) as client:
        def fail_preparation(*args, **kwargs):
            raise ValueError("local preparation failed")
        monkeypatch.setattr(client._client, "build_request", fail_preparation)
        record = await run.request(target, client, request_spec(), budget_reserved=True,
                                   admission_deadline=time.monotonic() + 10)
    assert record.status == "failed"
    assert record.sent is False and record._budget_request_slot is None
    counts = assert_reconciles(run, inference_posts)
    assert counts["sent"] == counts["budget_counted"] == counts["budget_exempt"] == 0


async def test_multi_target_totals_include_preflight_and_saved_measurements(inference_posts):
    config = configuration("first", "second")
    run = runner._Run(config, None, None)
    # Preflight uses its own client before the workload runner dispatches.
    async with LLMClient(config.targets[0], config) as client:
        await client.execute(request_spec())
    run.note_sent("first", exempt_reason="preflight")
    for target in config.targets:
        async with LLMClient(target, config) as client:
            await run.request(target, client, request_spec())
    counts = assert_reconciles(run, inference_posts)
    assert (counts["sent"], counts["budget_counted"], counts["budget_exempt"],
            counts["saved_observations"]) == (3, 2, 1, 2)
    assert counts["budget_exempt_by_reason"] == {"preflight": 1}
    assert counts["targets"]["first"]["sent"] == 2
    assert counts["targets"]["second"]["sent"] == 1


async def test_saved_observations_only_include_retained_measurements(inference_posts):
    config = configuration()
    run = runner._Run(config, None, None)
    target = config.targets[0]
    async with LLMClient(target, config) as client:
        await run.request(target, client, request_spec("readiness"), budget_exemption="restart_readiness")
        await run.request(target, client, request_spec("performance_evidence_probe"))
        kept = await run.request(target, client, request_spec())
        dropped = await run.request(target, client, request_spec("warm"))
    assert run.request_counts()["saved_observations"] == 2
    # Evidence/memory guardrails may discard completed records. Sent counters
    # remain historical; saved observations follow the final retained records.
    run.records.remove(dropped)
    counts = assert_reconciles(run, inference_posts)
    assert counts["sent"] == 4 and counts["saved_observations"] == 1
    assert kept in run.records


@pytest.mark.parametrize("command,budget,skip_preflight,expected,reason", [
    ("cold-start", 2, True, (2, 1, 1, 0), "restart_readiness"),
    ("cold-start", 1, True, (1, 0, 1, 0), "restart_readiness"),
    ("run", 1, False, (2, 1, 1, 1), "preflight"),
    ("run", 1, True, (1, 1, 0, 1), None),
])
def test_cli_request_counts_without_sockets(monkeypatch, tmp_path, command, budget,
                                           skip_preflight, expected, reason):
    import json
    from giraffe.cli import main

    posts = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        posts.append(request)
        payload = json.loads(request.content)
        if payload.get("stream"):
            chunk = {"choices": [{"delta": {"content": "42"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "42"}, "finish_reason": "stop"}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "targets": [{"name": "local", "url": "http://local/v1", "model": "model",
                     "restart_command": ["true"]}],
        "checks": ["access"], "stream": True, "request_timeout_seconds": 3,
        "max_duration_seconds": 20,
    }))
    output = tmp_path / "run"
    args = [command, "--config", str(config_path), "--checks", "access",
            "--max-requests", str(budget), "--output", str(output),
            "--min-free-disk", "0", "--quiet"]
    if command == "cold-start":
        args.extend(["--restart", "local"])
    if skip_preflight:
        args.append("--skip-preflight")
    main(args)
    report = json.loads((output / "report.json").read_text())
    counts = report["observations"]["request_counts"]
    assert counts["sent"] == len(posts)
    assert counts["complete"] is True and counts["budget_counted_unsent"] == 0
    assert counts["sent"] == counts["budget_counted"] - counts["budget_counted_unsent"] + counts["budget_exempt"]
    assert tuple(counts[key] for key in ("sent", "budget_counted", "budget_exempt",
                                          "saved_observations")) == expected
    assert counts["budget_exempt_by_reason"] == ({reason: 1} if reason else {})
    assert counts["targets"]["local"] == {key: value for key, value in counts.items()
                                           if key not in {"targets", "complete", "available"}}


def test_cli_attributes_preflight_to_each_target(inference_posts, tmp_path):
    import json
    from collections import Counter
    from giraffe.cli import main

    config = configuration("first", "second").model_copy(update={"max_requests": 2})
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json())
    output = tmp_path / "run"
    main(["run", "--config", str(config_path), "--output", str(output),
          "--min-free-disk", "0", "--quiet"])
    counts = json.loads((output / "report.json").read_text())["observations"]["request_counts"]
    assert counts["sent"] == len(inference_posts) == 4
    assert counts["budget_counted"] == counts["budget_exempt"] == 2
    assert counts["saved_observations"] == 2
    assert Counter(request.url.host for request in inference_posts) == {"first": 2, "second": 2}
    for target in ("first", "second"):
        assert counts["targets"][target] == {
            "sent": 2, "budget_counted": 1, "budget_exempt": 1, "saved_observations": 1,
            "budget_counted_unsent": 0,
            "budget_exempt_by_reason": {"preflight": 1},
        }


async def test_counted_unsent_tracks_drops_refunds_and_connection_failures(inference_posts, monkeypatch):
    config = configuration()
    run = runner._Run(config, None, None)
    target = config.targets[0]
    assert run.reserve_offer(target.name)
    run.drop_offer(target.name, "load")
    assert run.request_counts()["budget_counted_unsent"] == 1
    assert run.reserve_offer(target.name)
    run.refund_offer(target.name)
    async with LLMClient(target, config) as client:
        await run.request(target, client, request_spec())
    counts = assert_reconciles(run, inference_posts)
    assert counts["sent"] == 1 and counts["budget_counted"] == 2
    assert counts["budget_counted_unsent"] == 1
    assert counts["targets"][target.name]["budget_counted_unsent"] == 1

    async def cannot_send(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    async with LLMClient(target, config) as client:
        monkeypatch.setattr(client._client._first, "send", cannot_send)
        record = await run.request(target, client, request_spec())
    assert record.sent is True and record.status == "failed"
    counts = run.request_counts()
    assert counts["sent"] == 2 and counts["budget_counted"] == 3
    assert counts["budget_counted_unsent"] == 1
    assert counts["sent"] == counts["budget_counted"] - counts["budget_counted_unsent"] + counts["budget_exempt"]


async def test_request_exemption_reason_is_validated(inference_posts):
    config = configuration()
    run = runner._Run(config, None, None)
    target = config.targets[0]
    async with LLMClient(target, config) as client:
        with pytest.raises(ValueError, match="Unknown request budget exemption"):
            await run.request(target, client, request_spec(), budget_exemption="scenario_name")
    assert run.request_counts()["sent"] == run.request_counts()["budget_counted"] == 0


@pytest.mark.usefixtures("local_tokenizer")
async def test_shared_gateway_per_target_counts_sum_to_total(monkeypatch):
    from tests.test_gateway_access import config as gateway_config

    config = gateway_config().model_copy(deep=True)
    config.checks = ["access", "gateway_access"]
    config.stream = False
    config.targets.append(config.targets[0].model_copy(update={"name": "second"}, deep=True))
    for target in config.targets:
        for identity in target.test_identities.values():
            identity.api_key_env = None
    posts = []

    def respond(request):
        if request.method != "POST":
            return httpx.Response(404)
        posts.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 1}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        **kwargs, transport=httpx.MockTransport(respond)))
    report = await runner.run_suite(config)
    counts = report.observations["request_counts"]
    assert counts["sent"] == len(posts) > 0
    for key in ("sent", "budget_counted", "budget_counted_unsent", "budget_exempt", "saved_observations"):
        assert counts[key] == sum(value[key] for value in counts["targets"].values())
    assert counts["sent"] == counts["budget_counted"] - counts["budget_counted_unsent"] + counts["budget_exempt"]
    assert all(value["sent"] > 0 for value in counts["targets"].values())


async def test_explicit_preflight_exemption_is_not_mapped_to_readiness(inference_posts):
    config = configuration()
    run = runner._Run(config, None, None)
    target = config.targets[0]
    async with LLMClient(target, config) as client:
        await run.request(target, client, request_spec(), budget_exemption="preflight")
    counts = assert_reconciles(run, inference_posts)
    assert counts["budget_exempt_by_reason"] == {"preflight": 1}
    assert counts["budget_counted"] == 0


async def test_counter_sink_observes_transport_before_request_finishes(inference_posts):
    snapshots = []
    token = runner.request_counts_sink.set(snapshots.append)
    try:
        config = configuration()
        run = runner._Run(config, None, None)
        target = config.targets[0]
        async with LLMClient(target, config) as client:
            await run.request(target, client, request_spec())
        assert snapshots[0]["sent"] == 0
        assert any(row["sent"] == 1 and row["saved_observations"] == 0 for row in snapshots)
        assert snapshots[-1]["saved_observations"] == 1
        for row in snapshots:
            assert row["sent"] == row["budget_counted"] - row["budget_counted_unsent"] + row["budget_exempt"]
    finally:
        runner.request_counts_sink.reset(token)


def assert_terminal_slot(events, record, status):
    from collections import OrderedDict
    from giraffe.web import _track_live_request

    start, = [event for event in events if event["event"] == "request_started"]
    terminal, = [event for event in events if event["event"] == "request_completed"]
    assert start["budget_request_slot"] == terminal["budget_request_slot"] == record._budget_request_slot == 0
    assert record.sent is False and terminal["request"]["sent"] is False
    assert record.status == status
    job = {"requests": OrderedDict(), "live_request_count": 0, "evicted_running": set(),
           "live_request_summary": {"through_slot": None, "evicted": 0, "statuses": {}}}
    _track_live_request(job, start, 2)
    assert job["requests"][0]["status"] == "running"
    _track_live_request(job, terminal, 2)
    assert job["requests"][0]["status"] == status and job["requests"][0]["sent"] is False


async def test_non_guard_preparation_failure_keeps_charged_terminal_slot(inference_posts, monkeypatch):
    config = configuration()
    events = []
    run = runner._Run(config, events.append, None)
    async with LLMClient(config.targets[0], config) as client:
        def fail(*args, **kwargs):
            raise ValueError("local preparation failed")
        monkeypatch.setattr(client._client, "build_request", fail)
        record = await run.request(config.targets[0], client, request_spec())
    assert_terminal_slot(events, record, "failed")
    counts = assert_reconciles(run, inference_posts)
    assert counts["budget_counted"] == counts["budget_counted_unsent"] == run.counted_attempted == 1


@pytest.mark.parametrize("cause", ["timeout", "deadline", "user", "memory", "cancel"])
async def test_unsent_pool_lane_wait_ends_its_original_tile(inference_posts, cause):
    config = configuration(request_timeout_seconds=0.03 if cause == "timeout" else 10)
    events = []
    started = asyncio.Event()

    def progress(event):
        events.append(event)
        if event["event"] == "request_started":
            started.set()

    run = runner._Run(config, progress, None)
    if cause == "deadline":
        run.deadline = time.monotonic() + 0.03
    async with LLMClient(config.targets[0], config, max_connections=1) as client:
        await client._client._slots.acquire()
        try:
            task = asyncio.create_task(run.request(config.targets[0], client, request_spec()))
            await asyncio.wait_for(started.wait(), 1)
            if cause in {"user", "memory", "cancel"}:
                if cause == "user":
                    run.stop.set()
                elif cause == "memory":
                    run.memory_limit_reached = True
                    run.memory_stop_reason = run.reason = "memory limit reached"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                record = run.records[-1]
            else:
                record = await task
        finally:
            client._client._slots.release()
    assert_terminal_slot(events, record, "timeout" if cause == "timeout" else "cancelled")
    counts = assert_reconciles(run, inference_posts)
    assert counts["budget_counted"] == counts["budget_counted_unsent"] == 1


async def test_refund_rejects_a_request_that_already_holds_a_slot(inference_posts):
    config = configuration()
    events = []
    run = runner._Run(config, events.append, None)
    # Leave a second unassigned offer: the task-local check must reject a refund
    # of the running request even while another offer could legally be refunded.
    assert run.reserve_offer("local")
    async with LLMClient(config.targets[0], config) as client:
        with pytest.raises(RuntimeError, match="after begin_attempt"):
            await run.request(config.targets[0], client, request_spec(),
                              on_start=lambda: run.refund_offer("local"))
    assert run.counted_attempted == 1 and run.budget_used == 2
    assert run.records[0]._budget_request_slot == 0
    assert_terminal_slot(events, run.records[0], "failed")


async def test_nested_concurrency_slot_cannot_hide_assigned_request_from_refund(inference_posts, monkeypatch):
    config = configuration()
    run = runner._Run(config, None, None)
    assert run.reserve_offer("local")
    async with LLMClient(config.targets[0], config) as client:
        execute = client.execute
        async def nested(spec, **kwargs):
            async with run.slot(True):
                with pytest.raises(RuntimeError, match="after begin_attempt"):
                    run.refund_offer("local")
            return await execute(spec, **kwargs)
        monkeypatch.setattr(client, "execute", nested)
        record = await run.request(config.targets[0], client, request_spec())
    assert record.sent and run.counted_attempted == 1 and run.budget_used == 2


async def test_dense_budget_slots_survive_exempt_refund_drop_and_unsent_attempt(inference_posts, monkeypatch):
    config = configuration().model_copy(update={"max_requests": 3})
    events = []
    run = runner._Run(config, events.append, None)
    target = config.targets[0]
    async with LLMClient(target, config) as client:
        await run.request(target, client, request_spec("readiness"), budget_exemption="restart_readiness")
        build = client._client.build_request
        def fail(*args, **kwargs):
            raise ValueError("local preparation failed")
        monkeypatch.setattr(client._client, "build_request", fail)
        assert run.reserve_offer("local")
        refunded = await run.request(target, client, request_spec(), budget_reserved=True,
                                     admission_deadline=time.monotonic() + 10)
        assert refunded._budget_request_slot is None and run.counted_attempted == run.budget_used == 0
        assert not [event for event in events if event["event"] == "request_started" and not event["request_budget_exempt"]]
        assert run.reserve_offer("local")
        run.drop_offer("local", "load")
        unsent = await run.request(target, client, request_spec())
        monkeypatch.setattr(client._client, "build_request", build)
        sent = await run.request(target, client, request_spec())
    assert [unsent._budget_request_slot, sent._budget_request_slot] == [0, 1]
    starts = [event["budget_request_slot"] for event in events if event["event"] == "request_started"
              and event["budget_request_slot"] is not None]
    ends = [event["budget_request_slot"] for event in events if event["event"] == "request_completed"
            and event["budget_request_slot"] is not None]
    assert starts == ends == [0, 1]
    assert run.counted_attempted <= run.budget_used <= config.max_requests
    assert max(starts) < config.max_requests


@pytest.mark.parametrize("guarded", [False, True])
async def test_endpoint_attempt_failure_refunds_without_committing_a_slot(inference_posts, monkeypatch, guarded):
    config = configuration()
    events = []
    run = runner._Run(config, events.append, None)
    target = config.targets[0]

    def fail_endpoint_attempt(target_name):
        raise ValueError("endpoint attempt failed")

    monkeypatch.setattr(run, "endpoint_attempt", fail_endpoint_attempt)
    options = {}
    if guarded:
        assert run.reserve_offer(target.name)
        options = {"budget_reserved": True, "admission_deadline": time.monotonic() + 10}
    async with LLMClient(target, config) as client:
        record = await run.request(target, client, request_spec(), **options)
    assert record.status == "failed"
    assert record.error == "request could not be prepared (ValueError)"
    assert record.sent is False and record._budget_request_slot is None
    assert not [event for event in events if event["event"] == "request_started"]
    assert run.counted_attempted == run.budget_used == len(run.inflight) == 0
    assert run.counts[target.name] == run.active[(target.name, "initial")] == 0
    condition = next(iter(run.conditions.values()))
    assert condition["started"] == condition["running"] == 0
    assert_reconciles(run, inference_posts)


@pytest.mark.usefixtures("local_tokenizer")
async def test_arrival_stop_observer_preserves_partial_accounting_without_flat_counters(inference_posts, monkeypatch):
    from types import SimpleNamespace

    from giraffe.models import TrafficConfig
    from tests.test_arrival_end_to_end import capacity, traffic_config, traffic_records

    stop = asyncio.Event()
    started = 0

    def progress(event):
        nonlocal started
        assert "attempted" not in event
        if event["event"] == "request_started":
            started += 1
            if event["scenario"].startswith("load_primary_") and started >= 4:
                stop.set()

    handle = httpx.MockTransport.handle_async_request

    async def slow_response(transport, request):
        if request.method == "POST":
            await asyncio.sleep(.3)
        return await handle(transport, request)

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", slow_response)
    config = traffic_config(SimpleNamespace(url="http://local/v1"), traffic=TrafficConfig(
        rates=[20], duration_seconds=2, max_in_flight=32,
        mix={"short": 1, "long_input": 0, "long_output": 0}))
    report = await asyncio.wait_for(runner.run_suite(config, progress=progress, stop_event=stop), 3)
    point = capacity(report).metrics["points"][0]
    assert report.abort_reason == "cancelled by user"
    assert point["scheduled"] == point["dispatched"] + point["not_sent"]
    assert point["not_sent"] > 0 and point["status"] == "inconclusive"
    assert len(traffic_records(report)) == point["dispatched"] <= 3
    counts = report.observations["request_counts"]
    assert counts["sent"] == counts["budget_counted"] - counts["budget_counted_unsent"] + counts["budget_exempt"]
