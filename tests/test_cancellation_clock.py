"""Cancellation timing must not mix runtime-converted and local clocks."""

import pytest

from giraffe.backend_cancellation import evaluate
from tests.test_sglang_cancellation import cancelled, trace


@pytest.mark.parametrize("offset", [-18_000_000, 18_000_000, -5_000_000_000, 5_000_000_000])
def test_sglang_abort_receipt_bounds_termination_despite_native_clock_offset(offset):
    record = cancelled(client_cancelled_at_ns=10_200_000_000,
                       client_cancelled_monotonic_ns=1_200_000_000)
    row = trace(started_at_ns=10_100_000_000 + offset,
                finished_at_ns=10_300_000_000 + offset,
                received_at_ns=10_400_000_000,
                received_monotonic_ns=1_400_000_000)
    result = evaluate(record, row, 10_090_000_000, 1)
    assert result is not None and result["status"] == "pass"
    assert result["termination_ms"] is None
    assert result["termination_upper_bound_ms"] == 200
    assert result["timing_basis"] == "local_monotonic_receipt"
    assert result["trace"] == row  # Preserve native timestamps, without silently correcting them.


@pytest.mark.parametrize("reason", ["abort", "stop", "length"])
def test_sglang_late_export_does_not_prove_late_termination(reason):
    result = evaluate(cancelled(client_cancelled_monotonic_ns=1_000_000_000),
                      trace(finish_reasons=[reason], received_monotonic_ns=3_000_000_000), 90, 1)
    assert result["status"] == "inconclusive"
    assert result["termination_ms"] is None
    assert result["termination_upper_bound_ms"] == 2000


@pytest.mark.parametrize("received", [999_999_999, 0, -1, "1200000000", True])
def test_sglang_stale_or_invalid_local_receipt_cannot_pass(received):
    assert evaluate(cancelled(client_cancelled_monotonic_ns=1_000_000_000),
                    trace(received_monotonic_ns=received), 90, 1) is None


@pytest.mark.parametrize("cancel_mono,receive_mono", [(None, None), (1_000_000_000, None), (None, 1_200_000_000)])
def test_sglang_legacy_trace_without_local_timing_remains_unqualified(cancel_mono, receive_mono):
    result = evaluate(cancelled(client_cancelled_monotonic_ns=cancel_mono),
                      trace(received_monotonic_ns=receive_mono), 90, 1)
    assert result["status"] == "inconclusive"
    assert result["termination_ms"] is None
    assert result["timing_basis"] == "unavailable"


def test_vllm_native_late_termination_still_fails():
    row = trace(source="vllm::scheduler", finished_at_ns=2_000_000_300,
                received_at_ns=3_000_000_000)
    result = evaluate(cancelled(), row, 90, 1, "vllm")
    assert result["status"] == "fail"
    assert result["termination_ms"] > 1000
