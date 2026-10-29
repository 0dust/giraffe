"""Shared sample statistics for measured requests and saved reports.

Percentiles use linear interpolation between closest ranks, at (n - 1) * p.
Missing, boolean and non-finite values are not observations.
"""
from __future__ import annotations

import math
from statistics import fmean

PERCENTILES = "Linear interpolation between closest ranks: position = (n - 1) × p."


def _values(values):
    return [value for value in values if isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value)]


def percentile(values, p):
    """Return the interpolated percentile at fraction p, or None if empty."""
    if not 0 <= p <= 1:
        raise ValueError("percentile fraction must be between 0 and 1")
    ordered = sorted(_values(values))
    if not ordered:
        return None
    position = (len(ordered) - 1) * p
    lower, upper = math.floor(position), math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def median(values):
    """Return the sample median, or None if empty."""
    return percentile(values, .5)


def distribution(values):
    """Return the common distribution fields, including empty-sample counts."""
    values = _values(values)
    return dict(n=len(values), mean=fmean(values) if values else None,
                min=min(values, default=None), median=median(values),
                p90=percentile(values, .9), p95=percentile(values, .95),
                p99=percentile(values, .99), max=max(values, default=None))
