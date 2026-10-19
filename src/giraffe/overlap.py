"""Server-active streamed response windows, excluding client scheduling stalls.

Concurrency needs a positive intersection from first answer output to response
completion. Fairness and mixed decoding use the narrower first-to-last answer
window instead. Client in-flight counts alone cannot establish either claim.
"""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import groupby

NEEDS_STREAM = "server-side concurrency needs streamed output timing"


def _window(record, end_field):
    start, first, end = (getattr(record, name, None) for name in ("timing_start_ms", "first_output_ms", end_field))
    if any(value is None or not math.isfinite(value) for value in (start, first, end)) or end < first:
        return None
    return (start + first, start + end)


def output_window(record):
    """Absolute first answer to response completion; complete responses lack evidence."""
    return _window(record, "elapsed_ms") if getattr(record, "stream", True) else None


def answer_window(record):
    """Absolute first-to-last answer output, for claims about active decoding."""
    return _window(record, "last_output_ms")


def scheduling_delay_windows(run):
    health = getattr(run, "timing_health", None)
    if health is None:
        return []
    snapshot = health.snapshot()
    return snapshot["windows"] + ([snapshot["overflow_window"]] if snapshot["overflow_window"] else [])


def _overlap(a, b, delay_windows):
    if a is None or b is None:
        return False
    start, end = max(a[0], b[0]), min(a[1], b[1])
    if start >= end:
        return False
    for delay in sorted(delay_windows, key=lambda w: w["start_ms"]):
        if delay["start_ms"] > start:
            break
        start = max(start, delay["end_ms"])
        if start >= end:
            return False
    return True


def windows_overlap(a, b, delay_windows=()):
    return a.target == b.target and _overlap(output_window(a), output_window(b), delay_windows)


def evidenced_overlap(record, others, delay_windows=()):
    """1 + other same-target responses with a positive, unstalled intersection."""
    window = output_window(record)
    if window is None:
        return 0
    return 1 + sum(other is not record and other.target == record.target
                   and _overlap(window, output_window(other), delay_windows) for other in others)


def peak_evidenced_concurrency(records, delay_windows=()):
    """Peak simultaneously active responses outside scheduling-delay windows."""
    events = defaultdict(list)
    for record in records:
        window = output_window(record)
        if window is not None and window[0] < window[1]:
            events[record.target].extend(((window[0], 0, 1), (window[1], 0, -1)))
    peak = 0
    for target_events in events.values():
        for delay in delay_windows:
            target_events.extend(((delay["start_ms"], 1, 1), (delay["end_ms"], 1, -1)))
        active = delayed = 0
        # Apply all equal-time ends/starts together: touching windows do not overlap.
        for _, batch in groupby(sorted(target_events), key=lambda event: event[0]):
            for _, kind, delta in batch:
                if kind:
                    delayed += delta
                else:
                    active += delta
            if not delayed:
                peak = max(peak, active)
    return peak
