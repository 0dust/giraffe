"""Bounded client scheduling evidence; never a correction to observed latency."""

from __future__ import annotations

import math

from .models import Limits, RequestRecord

INTERVAL_MS = 50
MIN_DELAY_MS = 100
MAX_WINDOWS = 4096
TIMING_METRICS = ("latency_ms", "first_output_ms", "max_stream_gap_ms",
                  "output_tokens_per_second", "generation_tokens_per_second")


class TimingHealth:
    def __init__(self, start: float):
        self.start = self.last = start
        self.samples = self.delayed_samples = 0
        self.max_lag_ms = 0.0
        self.windows: list[dict] = []
        self.overflow: dict | None = None

    def sample(self, now: float) -> None:
        if now - self.last < INTERVAL_MS / 1000:
            return
        lag = max(0, (now - self.last) * 1000 - INTERVAL_MS)
        self.samples += 1
        self.max_lag_ms = max(self.max_lag_ms, lag)
        if lag > MIN_DELAY_MS:
            self.delayed_samples += 1
            # We cannot locate the start of a stall between ticks. Conservatively
            # use the last responsive tick, including the normal 50 ms interval.
            window = {"start_ms": (self.last - self.start) * 1000,
                      "end_ms": (now - self.start) * 1000, "lag_ms": lag}
            if len(self.windows) < MAX_WINDOWS:
                self.windows.append(window)
            elif self.overflow is None:
                self.overflow = window
            else:
                self.overflow["end_ms"] = window["end_ms"]
                self.overflow["lag_ms"] = max(self.overflow["lag_ms"], lag)
        self.last = now

    def snapshot(self) -> dict:
        return {"schema_version": 1, "interval_ms": INTERVAL_MS,
                "sample_count": self.samples, "delayed_sample_count": self.delayed_samples,
                "max_lag_ms": self.max_lag_ms, "delay_floor_ms": MIN_DELAY_MS,
                "windows": [dict(window) for window in self.windows],
                "overflow_window": dict(self.overflow) if self.overflow else None,
                "scope": "Client event-loop responsiveness; CPU, OS and other causes are not distinguished."}


def threshold(records: list[RequestRecord]) -> float:
    from .stats import median

    warm = [r.first_output_ms for r in records if r.scenario == "warm"
            and r.status == "completed" and r.valid and r.first_output_ms is not None]
    return max(MIN_DELAY_MS, median(warm) * .1 if warm else 0)


def configured_metrics(limits: Limits, check: str | None = None) -> tuple[str, ...]:
    if check == "first_output":
        return ("first_output_ms",) if limits.first_output_ms is not None else ()
    values = {"latency_ms": limits.latency_ms,
              "first_output_ms": None if check == "generation" else limits.first_output_ms,
              "max_stream_gap_ms": limits.stream_gap_ms,
              "generation_tokens_per_second": limits.min_output_tokens_per_second}
    metrics = [key for key, value in values.items() if value is not None]
    if check in {"fairness", "mixed"} and limits.fairness_max_ratio is not None:
        metrics.append("latency_ms")
    if check == "recovery" and limits.recovery_max_ratio is not None:
        metrics.extend(("latency_ms", "first_output_ms"))
    return tuple(dict.fromkeys(metrics))


def exceeded_unfinished(record: RequestRecord, limits: Limits) -> list[str]:
    """Limits an accepted request that never finished its answer is known to exceed.

    A timed-out (or accepted-then-failed) request without first output waited at
    least elapsed_ms for it; a timed-out request took at least elapsed_ms.
    """
    accepted = (record.status in {"timeout", "failed"} and record.http_status is not None
                and 200 <= record.http_status < 300)
    if not accepted:
        return []
    metrics = []
    first = record.first_output_ms if record.first_output_ms is not None else record.elapsed_ms
    if limits.first_output_ms and first > limits.first_output_ms:
        metrics.append("first_output_ms")
    if record.status == "timeout" and limits.latency_ms and record.elapsed_ms > limits.latency_ms:
        metrics.append("latency_ms")
    return metrics


def qualified_violations(records: list[RequestRecord], limits: Limits, quality: dict,
                         *, arrival: bool = False, unfinished: bool = False) -> list[str]:
    """A delay in another sample/metric must not hide an independent violation.

    With ``unfinished``, accepted requests that timed out or failed count when
    they are known to exceed a limit (see exceeded_unfinished).
    """
    failed = []
    for record in records:
        if not record.valid or record.status != "completed":
            if unfinished and any(record.id not in quality["affected_metrics"].get(metric, [])
                                  for metric in exceeded_unfinished(record, limits)):
                failed.append(record.id)
            continue
        values = (("latency_ms", limits.latency_ms,
                   record.arrival_elapsed_ms if arrival else record.elapsed_ms, False),
                  ("first_output_ms", limits.first_output_ms,
                   record.arrival_first_output_ms if arrival else record.first_output_ms, False),
                  ("max_stream_gap_ms", limits.stream_gap_ms, record.max_stream_gap_ms, False),
                  ("generation_tokens_per_second", limits.min_output_tokens_per_second,
                   record.generation_tokens_per_second, True))
        if any(limit is not None and value is not None
               and (value < limit if minimum else value > limit)
               and record.id not in quality["affected_metrics"].get(metric, [])
               for metric, limit, value, minimum in values):
            failed.append(record.id)
    return failed


def _interval(record: RequestRecord, metric: str) -> tuple[float, float] | None:
    start = record.timing_start_ms if record.timing_start_ms is not None else record.dispatch_ms
    end = record.completed_ms
    if start is None or end is None:
        return None
    if record.elapsed_ms > 0:
        end = min(end, start + record.elapsed_ms)
    if metric == "first_output_ms" and record.first_output_ms is not None:
        end = min(end, start + record.first_output_ms)
    elif metric in {"max_stream_gap_ms", "generation_tokens_per_second"}:
        # A pause before the first read may buffer tokens and compress observed
        # gaps or the first-to-last answer interval. Arrival time is unknown, so
        # include dispatch through the last relevant observation conservatively.
        if metric == "generation_tokens_per_second" and record.last_output_ms is not None:
            end = min(end, (record.timing_start_ms if record.timing_start_ms is not None
                            else record.dispatch_ms) + record.last_output_ms)
    if record.scheduled_ms is not None and metric in {"latency_ms", "first_output_ms"}:
        start = min(start, record.scheduled_ms)
    elif record.dispatch_lag_ms is not None and metric in {"latency_ms", "first_output_ms"}:
        start -= record.dispatch_lag_ms
    return start, end


def qualify(records: list[RequestRecord], health: dict | None, limit_ms: float,
            metrics: tuple[str, ...] = TIMING_METRICS, *, legacy_max_lag_ms: float = 0,
            phase_intervals: tuple[tuple[float, float], ...] = ()) -> dict:
    """Keep every raw sample; qualify only overlapping metric intervals.

    Legacy reports lack intervals. Their old run-wide maximum remains a
    conservative fallback, explicitly labelled rather than treated as scoped.
    """
    scoped = (isinstance(health, dict) and health.get("schema_version") == 1
              and isinstance(health.get("windows"), list))
    windows = (health["windows"] + ([health["overflow_window"]]
               if health.get("overflow_window") else [])) if scoped else []
    if not all(isinstance(w, dict) and all(type(w.get(key)) in {int, float}
               and math.isfinite(w[key]) for key in ("start_ms", "end_ms", "lag_ms"))
               and 0 <= w["start_ms"] <= w["end_ms"] and w["lag_ms"] >= 0 for w in windows):
        scoped, windows = False, []
    windows = [window for window in windows if window["lag_ms"] > limit_ms]
    affected = {metric: [] for metric in metrics}
    maximum = 0.0
    for record in records:
        for metric in metrics:
            interval = _interval(record, metric)
            matching = [w for w in windows if interval is None or
                        interval[0] < w["end_ms"] and w["start_ms"] < interval[1]]
            if matching or (not scoped and legacy_max_lag_ms > limit_ms):
                affected[metric].append(record.id)
                maximum = max(maximum, max((w["lag_ms"] for w in matching),
                                          default=legacy_max_lag_ms))
    phase_windows = [w for w in windows if any(
        start < w["end_ms"] and w["start_ms"] < end for start, end in phase_intervals)]
    affected_ids = list(dict.fromkeys(item for ids in affected.values() for item in ids))
    delayed = bool(affected_ids or phase_windows or
                   not scoped and metrics and legacy_max_lag_ms > limit_ms)
    return {"status": "client_delayed" if delayed else "qualified",
            "scope": "request_intervals" if scoped else "legacy_run_maximum",
            "threshold_ms": limit_ms, "affected_request_ids": affected_ids,
            "affected_requests": len(affected_ids), "total_requests": len(records),
            "affected_metrics": affected, "phase_affected": bool(phase_windows),
            "max_overlapping_lag_ms": max(maximum, max((w["lag_ms"] for w in phase_windows), default=0)),
            "raw_samples_retained": True}


def run_quality(run, records: list[RequestRecord], target: str, metrics: tuple[str, ...],
                *, phase_intervals: tuple[tuple[float, float], ...] = ()) -> dict:
    health = getattr(run, "timing_health", None)
    return qualify(records, health.snapshot() if health is not None else None,
                   threshold([r for r in run.records if r.target == target]), metrics,
                   legacy_max_lag_ms=run.max_lag_ms, phase_intervals=phase_intervals)
