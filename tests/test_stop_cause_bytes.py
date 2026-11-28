"""Stable outcome bytes stay equal to the original PR for unaffected real suites."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from giraffe import judging, resources, runner
from tests.test_runner import (FakeClient, client as client, config, judge_summary,
                               judged_pack_config, load_config)


def outcome_bytes(report, events):
    """Exclude IDs, host resources and timing; retain all outcome prose and attribution."""
    value = report.model_dump(mode="json")
    observation = value["observations"]
    payload = {
        "overall": value["overall"], "abort_reason": value["abort_reason"],
        "limits": value["limits_reached"], "stop_cause": observation.get("stop_cause"),
        "judging_stop": (observation.get("judging") or {}).get("stop_reason"),
        "checks": sorted((check["target"], check["id"], check["status"], check.get("summary"),
                          check.get("finding_summary"), json.dumps(check.get("remediation"), sort_keys=True),
                          (check.get("metrics") or {}).get("reason_code"))
                         for check in value["checks"]),
        "records": sorted(Counter((record["status"], record.get("error"), record.get("termination_reason"))
                                  for record in value["requests"]).items()),
        "conditions": sorted(Counter((row.get("status"), row.get("reason"))
                                     for row in value.get("conditions", [])).items()),
        "events": [(event["event"], event.get("stop_kind", "MISSING"), event.get("abort_reason", "MISSING"))
                   for event in events if event["event"] in {"run_stopping", "run_finished"}],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


SCENARIOS = [
    "none", "user", "budget", "budget_user", "duration", "load_user", "memory_infer",
    "judged", "judged_memory_early",
]


async def scenario_outcome(implementation, monkeypatch, scenario):
    monkeypatch.setattr(implementation, "LLMClient", FakeClient)
    events, stop = [], asyncio.Event()
    memory = {"large": False, "samples": 0}

    def rss():
        memory["samples"] += 1
        large = memory["large"] or scenario == "memory_infer" and memory["samples"] > 1
        return 10 * 1024**3 if large else 1

    monkeypatch.setattr(resources, "current_rss_bytes", rss)
    completed = 0

    def progress(event):
        nonlocal completed
        events.append(event)
        if event["event"] == "request_completed":
            completed += 1
            if scenario in {"user", "budget_user", "load_user"} and completed == (2 if scenario == "load_user" else 1):
                if scenario == "load_user":
                    # Let the completed arrival release its slot before stopping
                    # between offers, rather than racing its drain callback.
                    asyncio.get_running_loop().call_later(.001, stop.set)
                else:
                    stop.set()

    FakeClient.delay = .4 if scenario == "duration" else .02
    settings = config(checks=["serving"], samples=20 if scenario == "duration" else 4,
                      concurrency=1, max_requests={"budget": 1, "budget_user": 2}.get(scenario),
                      stream=False, max_duration_seconds=1 if scenario == "duration" else 5,
                      max_memory_mb=1 if scenario == "memory_infer" else 0)
    if scenario == "load_user":
        settings = load_config(rate=10, duration=1)

        async def cooperative_monitor(self, tasks):
            # Pin cooperative stopping: the periodic monitor's competing task
            # cancellation otherwise races the scheduler and changes its prose.
            await asyncio.Future()

        monkeypatch.setattr(implementation._Run, "monitor", cooperative_monitor)
    if scenario.startswith("judged"):
        async def evaluate(config, records, specs, *, stop_event, progress=None, **kwargs):
            progress({"event": "judging_started"})
            if scenario == "judged":
                return judge_summary(records)
            if scenario == "judged_memory_early":
                memory["large"] = True
                await asyncio.wait_for(stop_event.wait(), 3)
            else:
                stop_event.set()
            return judge_summary(records, stopped="cancelled")

        monkeypatch.setattr(judging, "judge_records", evaluate)
        settings = judged_pack_config(checks=["correctness"],
            max_requests=1 if "budget" in scenario else None,
            **({"max_memory_mb": 1, "max_duration_seconds": 30} if "memory" in scenario else {}))
        settings.load_test.schedule.duration_seconds = 1
    report = await implementation.run_suite(settings, progress=progress, stop_event=stop)
    assert report.requests or scenario == "memory_infer"
    return outcome_bytes(report, events)


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_real_suite_outcome_bytes_match_original_pr(client, monkeypatch, scenario):
    expected = json.loads((Path(__file__).parent / "fixtures" / "stop_cause_outcomes.json").read_text())
    actual = await scenario_outcome(runner, monkeypatch, scenario)
    assert actual == json.dumps(expected[scenario], sort_keys=True, separators=(",", ":")).encode(), (
        {key: (value, expected[scenario][key]) for key, value in json.loads(actual).items()
         if value != expected[scenario][key]})
