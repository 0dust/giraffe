"""Real HTTP cross-layer verification, not real deployment qualification."""
import asyncio
import json

import pytest

from giraffe.models import RunConfig, ScaleDownLifecycleExport
from giraffe.runner import run_suite
from tests.fake_replica_router import FakeReplicaRouter


def config_for(router, tmp_path, **overrides):
    fixtures = tmp_path / "scale-down-fixtures.json"
    fixtures.write_text(json.dumps([
        dict(fixture_id=name, messages=[dict(role="user", content=prompt)],
             scorer="exact", expected="OK", max_tokens=256, check_ids=["scale_down"])
        for name, prompt in [("long", "LONG"), ("short", "SHORT")]
    ]))
    settings = dict(target="service", long_fixture="long", followup_fixture="short",
                    requests_per_second=5, concurrency=3, long_cohort_size=1,
                    min_baseline_samples=1, min_overlap_samples=1, min_post_samples=2,
                    baseline_timeout_seconds=2, event_timeout_seconds=1,
                    grace_seconds=2, post_seconds=.65, completion_seconds=.5)
    settings.update(overrides.pop("scale_down", {}))
    return RunConfig.model_validate(dict(
        targets=[dict(name="service", url=router.url, model="fake-model",
                      deployment=dict(collector_url=router.collector_url))],
        checks=["scale_down"], scale_down=settings, custom_fixtures=str(fixtures),
        metrics_interval_seconds=.1, metrics_timeout_seconds=.5,
        request_timeout_seconds=4, max_duration_seconds=10, max_requests=80,
        retention="all") | overrides)


async def exercise(router, config, *, marker="valid", mode="graceful", request_delay=0):
    controls = []
    task = asyncio.create_task(run_suite(config, on_scale_down_control=controls.append))
    ack = None
    try:
        async with asyncio.timeout(8):
            while not task.done() and (not controls or controls[0].status()["phase"] != "armed"):
                await asyncio.sleep(.01)
            if not task.done():
                # Capture AFTER arming while the original answer is active. A
                # pre-arming snapshot cannot establish the full polling bracket.
                reads = router.collector_reads
                while router.collector_reads <= reads and not task.done():
                    await asyncio.sleep(.01)
                if marker != "absent" and not task.done():
                    if request_delay:
                        router.request_removal()
                        await asyncio.sleep(request_delay)
                    status = controls[0].status()
                    ack = controls[0].accept_marker(dict(event_id="external-removal", nonce=status["nonce"],
                        target="wrong-target" if marker == "wrong" else status["target"],
                        replica_id=router.b, source="test-operator"))
                    if ack["accepted"]:
                        router.remove(mode)
            report = await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    check = next(check for check in report.checks if check.id == "scale_down")
    return report, check, ack


def statuses(check):
    return {key: value["status"] for key, value in check.metrics["results"].items()}


@pytest.mark.asyncio
async def test_graceful_http_removal_preserves_original_and_serves_survivors(tmp_path):
    with FakeReplicaRouter() as router:
        report, check, ack = await exercise(router, config_for(router, tmp_path))
        assert ack["accepted"]
        assert statuses(check) == dict(continuity="pass", drain="pass", capacity="pass"), check.metrics
        long = next(r for r in report.requests if r.fixture_id == "long")
        assert long.backend_id == router.b and long.server_request_id
        assert long.stream_terminated and long.score is True
        post_ids = check.metrics["results"]["capacity"]["request_ids"]
        assert all(r.backend_id == router.a for r in report.requests if r.id in post_ids)
        export = ScaleDownLifecycleExport.model_validate(router.export()["scale_down_lifecycle"])
        assert export.coverage[0].complete and export.coverage[0].scope == "all"
        assert any(e.request_id == long.server_request_id for e in export.events)
        assert max(e.sequence for e in [*export.events, *export.inventories]) == export.through_sequence


@pytest.mark.asyncio
async def test_abrupt_http_removal_keeps_original_failure_despite_successful_survivor(tmp_path):
    with FakeReplicaRouter() as router:
        report, check, _ = await exercise(router, config_for(router, tmp_path), mode="abrupt")
        assert statuses(check) == dict(continuity="fail", drain="fail", capacity="pass"), check.metrics
        long = next(r for r in report.requests if r.fixture_id == "long")
        assert not long.stream_terminated
        assert long.id in check.metrics["results"]["continuity"]["request_ids"]


@pytest.mark.asyncio
async def test_late_admission_fails_drain_independently_of_successful_responses(tmp_path):
    with FakeReplicaRouter() as router:
        report, check, _ = await exercise(router, config_for(router, tmp_path), mode="late_admission")
        assert router.late_admitted and router.late_admission_seen.is_set()
        long = next(r for r in report.requests if r.fixture_id == "long")
        late = next(event for event in router.events if event["kind"] == "request_admitted"
                    and event["replica_id"] == router.b and event["request_id"] != long.server_request_id)
        completed = next(event for event in router.events if event["kind"] == "request_completed"
                         and event["request_id"] == long.server_request_id)
        assert late["sequence"] < completed["sequence"]
        assert statuses(check) == dict(continuity="pass", drain="fail", capacity="pass"), check.metrics
        assert "admitted new work" in check.metrics["results"]["drain"]["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["sse_error", "eof", "abort"])
async def test_http_200_invalid_terminal_response_cannot_pass_continuity(tmp_path, terminal):
    with FakeReplicaRouter(terminal=terminal) as router:
        report, check, _ = await exercise(router, config_for(router, tmp_path))
        assert statuses(check)["continuity"] == "fail", check.metrics
        assert statuses(check)["capacity"] == "pass", check.metrics
        long = next(r for r in report.requests if r.fixture_id == "long")
        assert long.http_status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["absent", "wrong"])
async def test_missing_or_rejected_marker_cannot_create_transition_pass(tmp_path, marker):
    with FakeReplicaRouter() as router:
        _, check, ack = await exercise(router, config_for(router, tmp_path), marker=marker)
        assert set(statuses(check).values()) == {"inconclusive"}
        assert router.mode is None
        assert ack is None if marker == "absent" else not ack["accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("attribution,long_replica", [(False, "replica-b-boot-1"), (True, "replica-a-boot-1")])
async def test_missing_removed_replica_overlap_never_passes_drain(tmp_path, attribution, long_replica):
    with FakeReplicaRouter(attribution=attribution, long_replica=long_replica) as router:
        _, check, _ = await exercise(router, config_for(router, tmp_path))
        assert statuses(check)["drain"] == "inconclusive", check.metrics
        assert statuses(check)["capacity"] == "pass", check.metrics


@pytest.mark.asyncio
async def test_grace_expiry_is_a_backend_failure_with_observed_late_termination(tmp_path):
    with FakeReplicaRouter(release_delay=.35) as router:
        _, check, _ = await exercise(router, config_for(router, tmp_path,
            scale_down=dict(grace_seconds=.5, grace_anchor="removal_requested")), request_delay=.3)
        assert router.terminated
        assert statuses(check)["drain"] == "fail", check.metrics
        assert "exceeded the declared grace" in check.metrics["results"]["drain"]["summary"]


@pytest.mark.asyncio
async def test_request_budget_exhaustion_cannot_pass_post_removal_capacity(tmp_path):
    with FakeReplicaRouter() as router:
        report, check, _ = await exercise(router, config_for(router, tmp_path, max_requests=4))
        assert len(report.requests) <= 4
        assert statuses(check)["capacity"] == "inconclusive", check.metrics
        assert "pass" not in statuses(check).values(), check.metrics


@pytest.mark.asyncio
async def test_time_budget_cancellation_is_not_an_endpoint_interruption(tmp_path):
    with FakeReplicaRouter(release_delay=2) as router:
        report, check, _ = await exercise(router, config_for(router, tmp_path, max_duration_seconds=.6))
        long = next(r for r in report.requests if r.fixture_id == "long")
        assert long.status == "cancelled", long
        assert statuses(check)["continuity"] == "inconclusive", check.metrics
        assert statuses(check)["capacity"] == "inconclusive", check.metrics


@pytest.mark.asyncio
async def test_final_collection_retains_termination_after_grace_expires(tmp_path):
    with FakeReplicaRouter(release_delay=.35) as router:
        _, check, _ = await exercise(router, config_for(router, tmp_path,
            scale_down=dict(grace_seconds=.15, completion_seconds=.6)))
        assert router.terminated
        assert statuses(check)["drain"] == "fail", check.metrics
        assert "exceeded the declared grace" in check.metrics["results"]["drain"]["summary"]


@pytest.mark.asyncio
async def test_successful_survivor_attempt_cannot_erase_failed_backend_original(tmp_path):
    with FakeReplicaRouter() as router:
        report, check, _ = await exercise(router, config_for(router, tmp_path), mode="retry_survivor")
        # The router keeps the HTTP stream valid across the backend retry, so
        # client continuity passes while the failed original means B did not drain.
        assert statuses(check) == dict(continuity="pass", drain="fail", capacity="pass"), check.metrics
        long = next(r for r in report.requests if r.fixture_id == "long")
        assert long.valid and long.stream_terminated and long.score is True
        assert long.id in check.metrics["results"]["continuity"]["request_ids"]
        snapshots = report.observations["targets"]["service"]["scale_down"]["snapshots"]
        events = {event["event_id"]: event for snapshot in snapshots
                  for event in snapshot["export"]["events"]
                  if event.get("request_id") == long.server_request_id}
        admissions = [event for event in events.values() if event["kind"] == "request_admitted"]
        completions = [event for event in events.values() if event["kind"] == "request_completed"]
        assert len(admissions) == len(completions) == 2
        assert len({event["attempt_id"] for event in admissions}) == 2
        assert {event["attempt_id"] for event in admissions} == {event["attempt_id"] for event in completions}
        assert {(event["replica_id"], event["outcome"]) for event in completions} == {
            (router.b, "failed"), (router.a, "completed")}
        failed = next(event for event in completions if event["outcome"] == "failed")
        assert failed["event_id"] in check.metrics["results"]["drain"]["evidence_ids"]
