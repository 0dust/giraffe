"""Invariants of the explicitly SYNTHETIC six-stage design fixture."""

import heapq

from tests.performance_fixture import synthetic_report_1000


def test_dispatches_obey_current_target_even_after_downshifts():
    report = synthetic_report_1000()
    stages = report.observations['targets']['SYNTHETIC']['schedule_transitions']
    active = []
    counts = []
    for stage, budget in zip(stages[:-1], (80, 140, 200, 300, 180, 100), strict=True):
        rows = [row for row in report.requests if row.condition_id == stage['stage_id']]
        counts.append(len(rows))
        assert len(rows) == budget
        for row in rows:
            assert stage['start_ms'] <= row.dispatch_ms < stage['end_ms']
            while active and active[0] <= row.dispatch_ms:
                heapq.heappop(active)
            # A lower target may leave existing requests above target, but no
            # new request can refill the pool until enough of them finish.
            assert len(active) < stage['intended_concurrency'], row.id
            heapq.heappush(active, row.completed_ms)
    assert sum(counts) == len(report.requests) == 1000
    assert [stage['intended_concurrency'] for stage in stages] == [4, 8, 16, 32, 16, 8, 0]
    assert stages[-1]['kind'] == 'drain'
    assert max(row.dispatch_ms for row in report.requests) < stages[-1]['start_ms']
    assert stages[-1]['end_ms'] == max(row.completed_ms for row in report.requests)


def test_fixture_is_deterministic_and_preserves_synthetic_evidence():
    report = synthetic_report_1000()
    assert report.model_dump() == synthetic_report_1000().model_dump()
    assert report.manifest['synthetic'] is True
    assert all(row.id.startswith('SYNTHETIC-') for row in report.requests)
    assert any(row.first_token_ms > 7000 for row in report.requests)
    assert {row.cached_prompt_tokens for row in report.requests} == {0, 384}
    pauses = report.observations['client_timing_health']['windows']
    assert len(pauses) == 3
    assert [pause['end_ms'] for pause in pauses] == [report.requests[n - 1].dispatch_ms for n in (233, 601, 888)]
