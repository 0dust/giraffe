"""SYNTHETIC design/QA fixture. No inference endpoint is called."""
from datetime import datetime, timedelta, timezone
import heapq
import random

from giraffe.models import RequestRecord, RunReport


def synthetic_report_1000():
    """Six sustained stages 4→8→16→32→16→8, budgets 80/140/200/300/180/100.

    Synthetic successful transport is not a correctness/capacity verdict. Long
    responses and outliers survive stage downshifts; new dispatches wait until
    the surviving lifetimes leave room under the current target. Pauses, cache
    reuse and timing are invented, explicitly labelled.
    """
    rng = random.Random(52)
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records, stages, active = [], [], []
    now = 0.0
    for index, (level, budget) in enumerate(zip((4, 8, 16, 32, 16, 8), (80, 140, 200, 300, 180, 100), strict=True)):
        left = now
        for slot in range(budget):
            while active and active[0] <= now:
                heapq.heappop(active)
            while len(active) >= level:
                now = heapq.heappop(active)
                while active and active[0] <= now:
                    heapq.heappop(active)
            number = len(records) + 1
            if number in (233, 601, 888):
                now += .35
            answer = rng.randrange(32, 160)
            first = .15 + rng.random() * .4
            streaming = answer / (25+rng.random()*25)
            if number % 137 == 0:
                first += 7
            elapsed = first + streaming + .08
            finish = now + elapsed
            heapq.heappush(active, finish)
            condition = f"synthetic_stage_{index}"
            times = [first*1000, (first+streaming)*1000]
            records.append(RequestRecord(id=f"SYNTHETIC-{number:04d}", target="SYNTHETIC", fixture_id="SYNTHETIC",
                scenario=condition, condition_id=condition, check_ids=["load"],
                started_at=(epoch+timedelta(seconds=now)).isoformat(), completed_at=(epoch+timedelta(seconds=finish)).isoformat(),
                status="completed", valid=True, score=None, timing_schema=1, first_token_reason=None,
                first_token_ms=first*1000, first_output_ms=first*1000, last_output_ms=(first+streaming)*1000,
                stream_end_ms=elapsed*1000, dispatch_ms=now*1000, completed_ms=finish*1000,
                timing_start_ms=now*1000+1, elapsed_ms=elapsed*1000, answer_reads=2, answer_chunks=2,
                answer_chars=answer*4, first_read_answer_chars=4, input_tokens=512+number%13,
                output_tokens=answer, completion_tokens=answer, reasoning_tokens=0,
                cached_prompt_tokens=384 if number%3 else 0, cache_source="SYNTHETIC usage",
                finish_reason="stop", answer_read_timestamps_ms=times,
                output_events=[dict(sequence=i, read_id=i, at_ms=t, kind="answer", chars=4 if i == 0 else (answer-1)*4,
                    token_count=None, token_count_source=None) for i,t in enumerate(times)]))
        # Next target begins immediately; previous requests can carry across.
        now += .001
        stages.append(dict(stage_id=condition, check_id="load", kind="concurrency", label=f"C{level}",
            start_ms=left*1000, end_ms=now*1000, intended_concurrency=level, intended_rate_rps=None))
    end = max(r.completed_ms for r in records)
    stages.append(dict(stage_id="synthetic_drain", check_id="load", kind="drain", label="Drain",
        start_ms=now*1000, end_ms=end, intended_concurrency=0, intended_rate_rps=None))
    conditions = [dict(target="SYNTHETIC", condition={"id": f"synthetic_stage_{i}", "check": "load", "label": f"SYNTHETIC short input / variable output · {level} at once", "level": level},
        planned=budget, planned_kind="exact", status="finished", started=budget, completed=budget, failed=0, running=0)
        for i,(level,budget) in enumerate(zip((4,8,16,32,16,8),(80,140,200,300,180,100),strict=True))]
    pauses = [{"start_ms": records[n-1].dispatch_ms-350, "end_ms": records[n-1].dispatch_ms, "lag_ms": 300} for n in (233,601,888)]
    return RunReport(run_id="SYNTHETIC-1000-NOT-A-BENCHMARK", started_at=epoch.isoformat(),
        finished_at=(epoch+timedelta(milliseconds=end)).isoformat(), overall="inconclusive",
        manifest={"synthetic": True, "config": {"targets": [{"name": "SYNTHETIC", "model": "SYNTHETIC", "url": "http://synthetic.invalid"}], "retention": "none"}},
        requests=records, warnings=["SYNTHETIC DATA: generated for tests and design QA; no endpoint, correctness or capacity claim."],
        observations={"targets": {"SYNTHETIC": {"schedule_transitions": stages, "conditions": conditions}},
            "client_timing_health": {"schema_version": 1, "windows": pauses, "delay_floor_ms": 100, "sample_count": 1000}})
