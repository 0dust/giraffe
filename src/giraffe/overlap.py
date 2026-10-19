"""Server-active streamed response windows, excluding client scheduling stalls.

Concurrency needs a positive intersection from first answer output to response
completion. Fairness and mixed decoding use the narrower first-to-last answer
window instead. Client in-flight counts alone cannot establish either claim.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from itertools import groupby

NEEDS_STREAM = "server-side concurrency needs streamed output timing"
MIN_OVERLAP_MS = 1


class _Delays:
    def __init__(self, windows):
        merged = []
        for window in sorted(windows, key=lambda w: w["start_ms"]):
            start, end = window["start_ms"], window["end_ms"]
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        self.windows = merged
        self.ends = [end for _, end in merged]


def _delays(windows):
    return windows if isinstance(windows, _Delays) else _Delays(windows)


def _unstalled(window, delays):
    start, end = window
    index = bisect_right(delays.ends, start)
    for index in range(index, len(delays.windows)):
        a, b = delays.windows[index]
        if a >= end:
            break
        if a - start >= MIN_OVERLAP_MS:
            yield start, a
        start = max(start, b)
        if end - start < MIN_OVERLAP_MS:
            return
    if end - start >= MIN_OVERLAP_MS:
        yield start, end


def _window(record, end_field):
    start, first, end = (getattr(record, name, None) for name in ("timing_start_ms", "first_output_ms", end_field))
    if any(value is None or not math.isfinite(value) for value in (start, first, end)) or end < first:
        return None
    return (start + first, start + end)


def output_window(record):
    """Absolute first answer to response completion; complete responses lack evidence."""
    return _window(record, "stream_end_ms") if getattr(record, "stream", True) else None


def answer_window(record):
    """Absolute first-to-last answer output, for claims about active decoding."""
    return _window(record, "last_output_ms")


def scheduling_delay_windows(run):
    health = getattr(run, "timing_health", None)
    if health is None:
        return _delays(())
    snapshot = health.snapshot()
    return _delays(snapshot["windows"] + ([snapshot["overflow_window"]] if snapshot["overflow_window"] else []))


def _overlap(a, b, delay_windows):
    if a is None or b is None:
        return False
    start, end = max(a[0], b[0]), min(a[1], b[1])
    if end - start < MIN_OVERLAP_MS:
        return False
    return next(_unstalled((start, end), delay_windows), None) is not None


def windows_overlap(a, b, delay_windows=()):
    return a.target == b.target and _overlap(output_window(a), output_window(b), _delays(delay_windows))


def evidenced_overlap(record, others, delay_windows=(), *, limit=None):
    """1 + same-target responses with >= 1 ms unstalled intersection; optionally capped."""
    window = output_window(record)
    if window is None:
        return 0
    delays, count = _delays(delay_windows), 1
    for other in others:
        if (other is not record and other.target == record.target
                and _overlap(window, output_window(other), delays)):
            count += 1
            if limit is not None and count >= limit:
                break
    return count


def max_evidenced_overlap(records, others, delay_windows=()):
    """Capped at two: a sweep avoids pairwise scans for concurrency gates."""
    selected = {id(record): record for record in records}
    candidates = {id(record): record for record in others}
    delays, events, maximum = _delays(delay_windows), defaultdict(list), 0
    for key, record in (candidates | selected).items():
        window = output_window(record)
        if window is None:
            continue
        if key in selected:
            maximum = 1
        for start, end in _unstalled(window, delays):
            flags = (key in selected, key in candidates)
            events[record.target].extend(((start, 1, *flags), (end - MIN_OVERLAP_MS, -1, *flags)))
    for target_events in events.values():
        active = chosen = other = 0
        for _, batch in groupby(sorted(target_events), key=lambda event: event[0]):
            batch = list(batch)
            for _, delta, is_selected, is_other in batch:
                if delta == 1:
                    active, chosen, other = active + 1, chosen + is_selected, other + is_other
            if active >= 2 and chosen and other:
                return 2
            for _, delta, is_selected, is_other in batch:
                if delta == -1:
                    active, chosen, other = active - 1, chosen - is_selected, other - is_other
    return maximum


def peak_evidenced_concurrency(records, delay_windows=()):
    """Peak simultaneously active responses outside scheduling-delay windows."""
    events = defaultdict(list)
    delays = _delays(delay_windows)
    for record in records:
        window = output_window(record)
        if window is not None:
            for start, end in _unstalled(window, delays):
                events[record.target].extend(((start, 1), (end - MIN_OVERLAP_MS, -1)))
    peak = 0
    for target_events in events.values():
        active = 0
        # Trim ends by the margin; equal-time starts then qualify exactly 1 ms.
        for _, batch in groupby(sorted(target_events), key=lambda event: event[0]):
            batch = list(batch)
            active += sum(delta == 1 for _, delta in batch)
            peak = max(peak, active)
            active -= sum(delta == -1 for _, delta in batch)
    return peak
