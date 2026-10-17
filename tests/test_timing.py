from giraffe.models import Limits, RequestRecord
from giraffe.timing import TimingHealth, qualify, qualified_violations


def request(name="answer", start=1000):
    return RequestRecord(id=name, target="local", fixture_id="answer", scenario="generation",
        check_ids=["generation"], started_at="2026-10-13T00:00:00Z", status="completed",
        valid=True, dispatch_ms=start, timing_start_ms=start, completed_ms=start + 1000,
        first_output_ms=200, last_output_ms=900, elapsed_ms=1000, answer_chunks=2,
        output_tokens=10, max_stream_gap_ms=100)


def health(start, end, lag=200):
    return {"schema_version": 1, "windows": [{"start_ms": start, "end_ms": end,
                                             "lag_ms": lag}], "overflow_window": None}


def test_health_records_counts_and_conservative_windows_without_clock_drift():
    monitor = TimingHealth(10)
    monitor.sample(10.06)
    monitor.sample(10.31)
    monitor.sample(10.37)
    result = monitor.snapshot()
    assert result["sample_count"] == 3 and result["delayed_sample_count"] == 1
    window, = result["windows"]
    assert round(window["start_ms"]) == 60
    assert round(window["end_ms"]) == 310
    assert round(window["lag_ms"]) == 200
    assert round(monitor.max_lag_ms) == 200


def test_delay_after_first_output_affects_stream_and_completion_only():
    record = request()
    quality = qualify([record], health(1400, 1700), 100)
    assert quality["affected_metrics"]["first_output_ms"] == []
    assert quality["affected_metrics"]["latency_ms"] == [record.id]
    assert quality["affected_metrics"]["generation_tokens_per_second"] == [record.id]
    assert record.elapsed_ms == 1000  # Never subtract or discard raw measurements.


def test_delay_before_first_read_may_bias_generation_pace_through_buffering():
    quality = qualify([request()], health(1000, 1150), 100)
    assert quality["affected_metrics"]["first_output_ms"] == ["answer"]
    assert quality["affected_metrics"]["generation_tokens_per_second"] == ["answer"]
    assert quality["affected_metrics"]["max_stream_gap_ms"] == ["answer"]


def test_protocol_trailer_delay_affects_completion_and_gap_but_not_answer_rate():
    quality = qualify([request()], health(1950, 2100), 100)
    assert quality["affected_metrics"]["generation_tokens_per_second"] == []
    assert quality["affected_metrics"]["max_stream_gap_ms"] == ["answer"]


def test_preparation_before_dispatch_does_not_affect_first_output():
    record = request()
    record.timing_start_ms = 1500
    assert qualify([record], health(1000, 1400), 100, ("first_output_ms",))["status"] == "qualified"


def test_post_response_hashing_does_not_taint_finished_inference_timer():
    record = request()
    record.completed_ms = 2500
    assert qualify([record], health(2100, 2400), 100)["status"] == "qualified"
    # An offered traffic stage includes client work after the response as well.
    assert qualify([record], health(2100, 2400), 100,
                   phase_intervals=((1000, 2500),))["phase_affected"]


def test_arrival_dispatch_delay_and_phase_without_requests_are_qualified():
    record = request()
    record.dispatch_lag_ms = 400
    assert qualify([record], health(600, 850), 100, ("first_output_ms",))["status"] == "client_delayed"
    quality = qualify([], health(600, 850), 100, phase_intervals=((500, 1000),))
    assert quality["phase_affected"] and quality["status"] == "client_delayed"
    assert qualify([], health(600, 850), 100, phase_intervals=((1000, 2000),))["status"] == "qualified"


def test_missing_intervals_and_legacy_reports_stay_conservative():
    record = request()
    record.timing_start_ms = record.dispatch_ms = record.completed_ms = None
    assert qualify([record], health(0, 200), 100)["status"] == "client_delayed"
    legacy = qualify([request()], None, 100, legacy_max_lag_ms=500)
    assert legacy["status"] == "client_delayed" and legacy["scope"] == "legacy_run_maximum"
    assert qualify([request()], None, 1000, legacy_max_lag_ms=500)["status"] == "qualified"
    malformed = {"schema_version": 1, "windows": [{"lag_ms": "unknown"}]}
    result = qualify([request()], malformed, 100, legacy_max_lag_ms=500)
    assert result["status"] == "client_delayed" and result["scope"] == "legacy_run_maximum"


def test_retention_limit_preserves_conservative_overflow_evidence(monkeypatch):
    monkeypatch.setattr("giraffe.timing.MAX_WINDOWS", 1)
    monitor = TimingHealth(0)
    for now in [.25, .5, .75]:
        monitor.sample(now)
    snapshot = monitor.snapshot()
    assert len(snapshot["windows"]) == 1 and snapshot["delayed_sample_count"] == 3
    assert snapshot["overflow_window"]["start_ms"] == 250
    assert snapshot["overflow_window"]["end_ms"] == 750
    assert qualify([request(start=650)], snapshot, 100)["status"] == "client_delayed"


def test_delay_cannot_hide_independent_timing_violation():
    records = [request("delayed"), request("independent", start=4000)]
    quality = qualify(records, health(1000, 1300), 100)
    assert qualified_violations(records, Limits(latency_ms=500), quality) == ["independent"]
    # The first output is unaffected by a later stall, even in the same request.
    quality = qualify(records[:1], health(1500, 1800), 100)
    assert qualified_violations(records[:1], Limits(first_output_ms=100), quality) == ["delayed"]
