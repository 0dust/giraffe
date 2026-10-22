"""Unified load: workload × traffic points, sequential bounded windows.

Order is combinations, mixed, stress, then controlled interference. Within each
workload: increasing concurrency, constant rates, Poisson rates, bursts-only.
Imported legacy schedules follow the primary schedule without sharing overlays.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from decimal import Decimal
import math
import random
import time
from statistics import median

from giraffe.concurrent_load import (
    OutputProgress,
    _distribution,
    _percentile,
    aggregate,
    calibrate,
    context_window,
    inflight,
    make_spec,
    later_reservation,
    window_cause,
)
from giraffe.fixtures import deterministic_options, reasoning_budget
from giraffe.models import (
    CheckResult,
    MAX_PLANNED_ARRIVALS,
    MAX_SIMULTANEOUS_REQUESTS,
    TrafficConfig,
    load_shape_label,
    load_validation_error,
)
from giraffe.overlap import (
    _delays,
    _overlap,
    answer_window,
    peak_evidenced_concurrency,
    scheduling_delay_windows,
)
from giraffe.timing import configured_metrics, qualified_violations, run_quality
from giraffe.traffic import ScheduleObservation, arrival_schedule, run_stage

LOAD_TITLES = {
    "combinations": "Input → output combinations",
    "mixed": "Mixed traffic",
    "stress": "Token stress · large input and output",
    "interference": "Long input during an active short stream",
}
SCHEDULER_LAG_MS = 100  # Same delivery-evidence policy as traffic.py, not an acceptance criterion.


def settings_sets(settings):
    return [
        ("primary", settings),
        *[
            (f"legacy_{i}_{v.source}", v.settings)
            for i, v in enumerate(getattr(settings, "migrated_tests", []))
        ],
    ]


def running_shapes(settings):
    w = settings.workloads
    return list(
        dict.fromkeys(
            [
                *(w.combinations.selected if w.combinations.enabled else []),
                *(w.mixed.selected if w.mixed.enabled else []),
                *(w.stress.cases if w.stress.enabled else []),
                *(["short_short", "long_short"] if w.interference.enabled else []),
            ]
        )
    )


def input_class_bounds(settings, size, *, floors=None):
    """Adjacent profile targets define inclusive lower/exclusive upper classes."""
    names = ("short", "medium", "long")
    configured = [getattr(settings.profile, name).input_tokens for name in names]
    targets = [max(target, (floors or {}).get(name, target))
               for name, target in zip(names, configured)]
    # A disclosed built-in prompt floor can exceed several configured sizes.
    # Keep the higher classes distinct using the profile's relative spacing.
    for i in range(1, len(targets)):
        if targets[i] <= targets[i - 1]:
            targets[i] = targets[i - 1] * configured[i] / configured[i - 1]
    index = names.index(size)
    return (
        math.sqrt(targets[index - 1] * targets[index]) if index else None,
        math.sqrt(targets[index] * targets[index + 1]) if index < 2 else None,
    )


def _in_input_class(settings, size, tokens, *, floors=None):
    lower, upper = input_class_bounds(settings, size, floors=floors)
    return tokens is not None and (lower is None or tokens >= lower) and (upper is None or tokens < upper)


def _class_label(settings, size, *, floors=None):
    lower, upper = input_class_bounds(settings, size, floors=floors)
    if lower is None:
        return f"{size.title()} < {upper:g}"
    if upper is None:
        return f"{size.title()} ≥ {lower:g}"
    return f"{size.title()} {lower:g}–{upper:g}"


def input_qualification(settings, size, records, *, floors=None):
    # Selection estimates intentionally never enter this measurement evidence.
    valid = [r for r in records if r.valid and r.status == "completed" and not reasoning_budget(r)]
    known = [r.input_tokens for r in valid if r.input_tokens is not None]
    count = sum(_in_input_class(settings, size, n, floors=floors) for n in known)
    outside = len(known) - count
    percentage = outside / len(known) * 100 if known else None
    qualified = count >= settings.pass_criteria.min_valid_responses and percentage <= 10
    target = getattr(settings.profile, size).input_tokens
    actual = median(known) if known else None
    lower, upper = input_class_bounds(settings, size, floors=floors)
    reason = None
    if not known:
        reason = "Input token counts not reported by the endpoint; timing and correctness are shown but capacity is not established"
    elif not qualified:
        reason = f"{size.title()} input not exercised: actual median {actual:g} tokens, target {target} ({_class_label(settings, size, floors=floors)})"
    return dict(qualified=qualified, size=size, target_tokens=target, lower_bound=lower,
                upper_bound=upper, known_valid=len(known), in_class=count,
                out_of_class=outside, out_of_class_percent=percentage,
                unknown_valid=len(valid) - len(known), actual_median=actual, reason=reason)


def _source_tokens(source, records=()):
    measured = [r.input_tokens for r in records
                if r.fixture_id == source.fixture_id and r.valid and r.input_tokens is not None]
    if measured:
        return median(measured)
    # Used only to choose unchanged saved questions, never to qualify results.
    chars = source.input_chars or sum(len(str(m.get("content") or "")) for m in source.messages)
    return chars / 4


def _shape_sources(settings, size, sources, records=()):
    matching = [source for source in sources if _in_input_class(settings, size, _source_tokens(source, records))]
    return matching or sources


def stress_output_floor(settings):
    return math.ceil(settings.profile.long.output_tokens * settings.workloads.stress.min_output_percent / 100)


def stress_budget_reason(settings, budget):
    return (f"Saved question budget {budget} tokens cannot reach the required long-output stress coverage "
            f"of {stress_output_floor(settings)} tokens ({settings.workloads.stress.min_output_percent:g}% "
            f"of Long output cap {settings.profile.long.output_tokens})")


def saved_shape_warnings(config):
    from giraffe.fixtures import load_custom_fixtures

    if "load" not in config.checks:
        return []
    estimates = []
    budgets = []
    if config.request_source == "request_pack" and config.request_pack is not None:
        estimates = [sum(len(message.content) for message in case.messages) / 4
                     for case in config.request_pack.cases]
        cap = config.effective_check("load").get("max_output_tokens", config.max_output_tokens)
        budgets = [case.max_tokens or cap for case in config.request_pack.cases]
    if not estimates and config.custom_fixtures:
        try:
            sources = load_custom_fixtures(config.custom_fixtures,
                legacy_max_output_tokens=config.legacy_custom_fixture_max_output_tokens)
        except (OSError, ValueError):
            # Planning may inspect a portable configuration before its local
            # source is available. Execution retains source-file validation.
            return []
        estimates = [_source_tokens(source) for source in sources]
        budgets = [source.max_tokens or config.max_output_tokens for source in sources]
    if not estimates:
        return []
    warnings = []
    for _, settings in settings_sets(config.load_test):
        for size in dict.fromkeys(shape.split("_")[0] for shape in running_shapes(settings)):
            if any(_in_input_class(settings, size, tokens) for tokens in estimates):
                continue
            shapes = [" → ".join(part.title() for part in s.split("_")) for s in running_shapes(settings) if s.startswith(size + "_")]
            phrase = "long enough" if size == "long" else "in the length class"
            warnings.append(f"No saved question is {phrase} for {size.title()} input; {', '.join(shapes)} results will be inconclusive")
        if settings.workloads.stress.enabled:
            for shape in settings.workloads.stress.cases:
                if not shape.endswith("_long"):
                    continue
                eligible = [i for i, tokens in enumerate(estimates)
                            if _in_input_class(settings, shape.split("_")[0], tokens)] or list(range(len(estimates)))
                for budget in sorted({budgets[i] for i in eligible if budgets[i] < stress_output_floor(settings)}):
                    warnings.append(f"{load_shape_label(shape)}: {stress_budget_reason(settings, budget)}. Results will be inconclusive; saved budgets are preserved.")
    return list(dict.fromkeys(warnings))


def calibration_keys(settings):
    return list(
        dict.fromkeys(
            (s.split("_")[0], "short_answer" if s.split("_")[1] == "short" else "long_output")
            for s in running_shapes(settings)
        )
    )


def point_shapes(settings, point):
    return (
        list(settings.workloads.mixed.selected)
        if point["type"] == "mixed"
        else ["short_short", "long_short"]
        if point["type"] == "interference"
        else [point["shape"]]
    )


def burst_count(settings):
    b, duration = settings.patterns.bursts, settings.schedule.duration_seconds
    if not b.enabled or b.gap_seconds <= 0 or b.first_seconds >= duration:
        return 0
    first, gap, end = map(lambda v: Decimal(str(v)), (b.first_seconds, b.gap_seconds, duration))
    return int(((end - first) / gap).to_integral_value(rounding="ROUND_CEILING"))


def burst_times(settings):
    b, duration = settings.patterns.bursts, settings.schedule.duration_seconds
    if not b.enabled or b.gap_seconds <= 0 or b.first_seconds >= duration:
        return []
    # Decimal boundaries prevent an exact endpoint being admitted by float drift.
    first, gap, end = map(lambda v: Decimal(str(v)), (b.first_seconds, b.gap_seconds, duration))
    count = int(((end - first) / gap).to_integral_value(rounding="ROUND_CEILING"))
    return [float(first + i * gap) for i in range(min(count, MAX_PLANNED_ARRIVALS + 1))]


def traffic_points(settings):
    p = settings.patterns
    points = []
    if p.concurrent.enabled:
        points += [
            {"pattern": "concurrent", "level": n, "rate": None, "bursts": None}
            for n in p.concurrent.levels
        ]
    for name in ("constant", "poisson"):
        if getattr(p, name).enabled:
            points += [
                {
                    "pattern": name,
                    "rate": r,
                    "level": None,
                    "bursts": p.bursts.model_dump() if p.bursts.enabled else None,
                }
                for r in p.rates
            ]
    if p.bursts.enabled and not (p.constant.enabled or p.poisson.enabled):
        points.append(
            {"pattern": "bursts", "rate": None, "level": None, "bursts": p.bursts.model_dump()}
        )
    return points


def test_points(settings):
    points = []
    for source, own in settings_sets(settings):
        w = own.workloads
        workloads = (
            [("combinations", s, s) for s in w.combinations.selected]
            if w.combinations.enabled
            else []
        )
        if w.mixed.enabled:
            workloads.append(("mixed", "mixed", None))
        if w.stress.enabled:
            workloads += [("stress", s, s) for s in w.stress.cases]
        for kind, workload, shape in workloads:
            for traffic in traffic_points(own):
                suffix = (
                    f"c{traffic['level']}"
                    if traffic["level"]
                    else f"r{traffic['rate']:g}"
                    if traffic["rate"]
                    else "only"
                )
                pattern = traffic["pattern"] + (
                    "_bursts" if traffic["bursts"] and traffic["pattern"] != "bursts" else ""
                )
                points.append(
                    {
                        "id": f"load_{source}_{kind}_{workload}_{pattern}_{suffix}",
                        "source": source,
                        "type": kind,
                        "workload": workload,
                        "shape": shape,
                        **traffic,
                        "configured_window_seconds": own.schedule.duration_seconds,
                        "windows": 1,
                    }
                )
        if w.interference.enabled:
            points += [
                {
                    "id": f"load_{source}_interference_c{n}",
                    "source": source,
                    "type": "interference",
                    "workload": "interference",
                    "shape": None,
                    "pattern": "interference",
                    "level": n,
                    "rate": None,
                    "bursts": None,
                    "windows": 2,
                    "configured_window_seconds": own.schedule.duration_seconds,
                }
                for n in own.patterns.concurrent.levels
                if n >= 2
            ]
    return points


def scheduled_count(settings, point):
    if point["pattern"] in {"concurrent", "interference"}:
        return None
    n = burst_count(settings) * settings.patterns.bursts.size if point["bursts"] else 0
    if point["pattern"] == "constant":
        n += math.ceil(point["rate"] * settings.schedule.duration_seconds)
    elif point["pattern"] == "poisson":
        n += point["rate"] * settings.schedule.duration_seconds
    return n


def load_time_plan(settings):
    own = dict(settings_sets(settings))
    points = test_points(settings)
    expected = sum(scheduled_count(own[p["source"]], p) or 0 for p in points)
    unknown = any(p["pattern"] in {"concurrent", "interference"} for p in points)
    approximate = any(p["pattern"] == "poisson" for p in points)
    def known(point):
        config = own[point["source"]]
        bursts = burst_count(config) * config.patterns.bursts.size if point["bursts"] else 0
        constant = math.ceil(point["rate"] * config.schedule.duration_seconds) if point["pattern"] == "constant" else 0
        return bursts + constant
    windows = sum(p["windows"] for p in points)
    sending = sum(p["windows"] * own[p["source"]].schedule.duration_seconds for p in points)
    allowances = sum(p["windows"] * own[p["source"]].schedule.finish_seconds for p in points)
    rows = {}
    for kind in LOAD_TITLES:
        selected = [p for p in points if p["type"] == kind]
        if selected:
            rows[kind] = {
                "title": LOAD_TITLES[kind],
                "test_points": len(selected),
                "windows": sum(p["windows"] for p in selected),
                "configured_load_seconds": sum(
                    p["windows"] * own[p["source"]].schedule.duration_seconds for p in selected
                ),
                "finish_allowance_seconds": sum(
                    p["windows"] * own[p["source"]].schedule.finish_seconds for p in selected
                ),
                "expected_requests": sum(
                    scheduled_count(own[p["source"]], p) or 0 for p in selected
                ),
                "known_scheduled_requests": sum(known(p) for p in selected),
                "unknown_requests": any(
                    scheduled_count(own[p["source"]], p) is None for p in selected
                ),
                "approximate": any(p["pattern"] == "poisson" for p in selected),
            }
    execution_summary = []
    for source, value in own.items():
        source_points = [p for p in points if p["source"] == source]
        workloads = value.workloads
        per_workload = len(traffic_points(value))
        suffix = "" if source == "primary" else f" Imported {source} schedule."
        if workloads.combinations.enabled:
            execution_summary.append(f"{len(workloads.combinations.selected)} individual size combinations × {per_workload} traffic points." + suffix)
        if workloads.mixed.enabled:
            execution_summary.append(f"One random mix of {len(workloads.mixed.selected)} request types × {per_workload} traffic points." + suffix)
        if workloads.stress.enabled:
            execution_summary.append(f"{len(workloads.stress.cases)} token-stress cases × {per_workload} traffic points." + suffix)
            if any(shape.endswith("_long") for shape in workloads.stress.cases):
                execution_summary.append(f"Required long-output stress coverage: {stress_output_floor(value)} tokens ({workloads.stress.min_output_percent:g}% of Long output cap {value.profile.long.output_tokens})." + suffix)
        comparison = [p for p in source_points if p["type"] == "interference"]
        if comparison:
            control = sum(p["windows"] * (value.schedule.duration_seconds + value.schedule.finish_seconds) for p in comparison)
            execution_summary.append(f"{len(comparison)} short-answer checks at concurrency {', '.join(str(p['level']) for p in comparison)}: short answers alone, then with a long request. {int(control // 60)}m {round(control % 60)}s including finish allowances." + suffix)
    return {
        "scheduled_tests": sum(p["type"] != "interference" for p in points),
        "comparison_checks": sum(p["type"] == "interference" for p in points),
        "windows": windows,
        "configured_load_seconds": sending,
        "finish_allowance_seconds": allowances,
        "time_with_allowances_seconds": sending + allowances,
        "expected_scheduled_requests": expected,
        "known_scheduled_requests": sum(known(p) for p in points),
        "unknown_requests": unknown,
        "approximate": approximate,
        "expected_requests_label": (
            "Unknown + " if unknown and expected else "Unknown" if unknown else ""
        )
        + (f"{'≈ ' if approximate else ''}{expected:,.0f}" if expected or not unknown else ""),
        "execution_order": [point_label(p) for p in points],
        "execution_summary": execution_summary,
        "execution_order_notes": list(dict.fromkeys(
            "Level 1: not applicable to the short-answer comparison (needs at least 2 concurrent requests)"
            for value in own.values()
            if value.workloads.interference.enabled and 1 in value.patterns.concurrent.levels
        )),
        "minimum_initial_requests_per_target": minimum_reservation(settings),
        "points": points,
        "workloads": rows,
        "burst_previews": [
            {
                "source": name,
                "times_seconds": burst_times(v),
                "requests": len(burst_times(v)) * v.patterns.bursts.size,
                "text": f"{len(burst_times(v))} bursts per test: at {', '.join(f'{t:g}' for t in burst_times(v)[:6])}{', …' if len(burst_times(v)) > 6 else ''} seconds. "
                f"{len(burst_times(v)) * v.patterns.bursts.size} "
                + ("additional requests per arrival-pattern test" if v.patterns.constant.enabled or v.patterns.poisson.enabled else "scheduled requests in this burst-only test")
                + f". No burst starts at or after {v.schedule.duration_seconds:g}s.",
            }
            for name, v in own.items()
            if v.patterns.bursts.enabled
        ],
        "note": "Configured upper allowance before setup and other checks; completion-driven counts are unknown. Run limits may stop work early.",
    }


def point_label(point):
    shape = load_shape_label(point["shape"]) if point["shape"] else LOAD_TITLES[point["type"]]
    traffic = (
        "Maintain concurrent requests"
        if point["pattern"] == "concurrent"
        else "Short answers alone, then with a long request"
        if point["pattern"] == "interference"
        else point["pattern"].capitalize()
    )
    return (
        f"{shape} · {traffic}"
        + (" + bursts" if point["bursts"] and point["pattern"] != "bursts" else "")
        + (
            f" · concurrency {point['level']}"
            if point["level"]
            else f" · {point['rate']:g} requests/second"
            if point["rate"]
            else ""
        )
    )


def admission_limit(config, settings):
    if config.concurrency is not None:
        return config.concurrency
    p = settings.patterns
    rate = max(p.rates) if p.constant.enabled or p.poisson.enabled else 0
    return min(
        MAX_SIMULTANEOUS_REQUESTS,
        max(
            1,
            math.ceil(rate * settings.request_timeout_seconds)
            + (p.bursts.size if p.bursts.enabled else 0),
        ),
    )


def required_slots(config):
    settings = dict(settings_sets(config.load_test))
    return max(
        [
            1,
            *[
                point["level"]
                if point["level"] is not None
                else admission_limit(config, settings[point["source"]])
                for point in test_points(config.load_test)
            ],
        ]
    )


def minimum_reservation(settings):
    return sum(
        (p["level"] * 2 - 1 if p["type"] == "interference" else p["level"] or 0)
        for p in test_points(settings)
    )


def validate_config(config):
    for source, own in settings_sets(config.load_test):
        prefix = (
            "load_test."
            if source == "primary"
            else f"load_test.migrated_tests.{int(source.split('_')[1])}.settings."
        )
        levels = own.patterns.concurrent.levels
        if own.workloads.interference.enabled and not any(n >= 2 for n in levels):
            load_validation_error(
                [prefix + "patterns.concurrent.levels", "concurrency"],
                "The long-input / short-answer test needs an eligible concurrency level of at least 2 under the run ceiling.",
            )
        fields = [prefix + "patterns.concurrent.levels", "concurrency"]
        if (
            (own.patterns.concurrent.enabled or own.workloads.interference.enabled)
            and config.concurrency is not None
            and max(levels) * (len(config.targets) if config.overlap_models else 1)
            > config.concurrency
        ):
            load_validation_error(
                fields,
                f"Concurrency levels exceed the run ceiling: {max(levels)}" + (f" × {len(config.targets)} endpoints" if config.overlap_models else "") + f" > {config.concurrency}. Update Run limits or lower the levels.",
                values={prefix + "patterns.concurrent.levels": levels, "concurrency": config.concurrency},
            )
        standard = any(
            getattr(own.workloads, k).enabled for k in ("combinations", "mixed", "stress")
        )
        if standard and own.patterns.bursts.enabled:
            if config.concurrency is not None and own.patterns.bursts.size > config.concurrency and not getattr(own, "legacy_source", None):
                load_validation_error(
                    [prefix + "patterns.bursts.size", "concurrency"],
                    "One burst exceeds the concurrency ceiling and cannot be dispatched in full.",
                    values={prefix + "patterns.bursts.size": own.patterns.bursts.size, "concurrency": config.concurrency},
                )
        for point in traffic_points(own) if standard else []:
            count = scheduled_count(own, point) or 0
            if count > MAX_PLANNED_ARRIVALS:
                probe = own.model_copy(deep=True)
                low, high = 0, own.schedule.duration_seconds
                while low < high:
                    middle = (low + high + 1) // 2
                    probe.schedule.duration_seconds = middle
                    if all((scheduled_count(probe, other) or 0) <= MAX_PLANNED_ARRIVALS for other in traffic_points(probe)):
                        low = middle
                    else:
                        high = middle - 1
                rate_field = prefix + ("patterns.rates" if point["rate"] else "patterns.bursts.gap_seconds")
                duration_field = prefix + "schedule.duration_seconds"
                minutes, seconds = divmod(low, 60)
                rate_text = f"{point['rate']:g} requests/second × {own.schedule.duration_seconds:,} seconds" if point["rate"] else ""
                if point["bursts"]:
                    rate_text += (" + " if rate_text else "") + f"{burst_count(own) * own.patterns.bursts.size:,} burst offers"
                rate_text += " = " if rate_text else ""
                minimum_window = max(1, math.floor(own.patterns.bursts.first_seconds) + 1) if point["bursts"] else 1
                feasible = low >= minimum_window
                boundary = (f"At this rate, the longest stage is {low} seconds ({minutes} minutes {seconds} seconds). "
                    if feasible else f"Even the minimum valid sending window ({minimum_window} seconds) exceeds this bound; reduce the sending rate or burst size/frequency. ")
                message = (rate_text + f"{count:,.0f} planned arrivals. This traffic plan allows at most {MAX_PLANNED_ARRIVALS:,} per endpoint. "
                    + boundary + "This bounds scheduled traffic only; run budgets are separate.")
                actions = ([{"label": f"Use {low} seconds", "field": duration_field, "value": low}] if feasible else [])
                if point["rate"]:
                    actions.append({"label": "Edit Rates (requests/second)", "field": prefix + "patterns.rates"})
                if point["bursts"]:
                    actions.append({"label": "Edit Time between burst starts (seconds)", "field": prefix + "patterns.bursts.gap_seconds"})
                load_validation_error(
                    [rate_field, duration_field], message,
                    values={rate_field: own.patterns.rates if point["rate"] else own.patterns.bursts.gap_seconds, duration_field: own.schedule.duration_seconds,
                            "planned_arrivals": count, "maximum_planned_arrivals": MAX_PLANNED_ARRIVALS,
                            "longest_stage_seconds": low},
                    actions=actions,
                )
        if own.workloads.interference.enabled and not config.stream:
            load_validation_error(
                [prefix + "workloads.interference.enabled", "stream"],
                "Long input during an active short stream needs streaming responses.",
                values={prefix + "workloads.interference.enabled": True, "stream": config.stream},
            )
        for target in config.targets:
            context, _ = context_window(target)
            for shape in running_shapes(own):
                a, b = shape.split("_")
                if (
                    context is not None
                    and not getattr(own, "legacy_source", None)
                    and getattr(own.profile, a).input_tokens + getattr(own.profile, b).output_tokens
                    > context
                ):
                    load_validation_error(
                        [
                            prefix + f"profile.{a}.input_tokens",
                            prefix + f"profile.{b}.output_tokens",
                        ],
                        f"{load_shape_label(shape)} exceeds the known endpoint context window ({context} tokens).",
                    )
    settings = dict(settings_sets(config.load_test))
    required = sum((p["level"] * 2 - 1 if p["type"] == "interference" else p["level"] or 0) for p in test_points(config.load_test) if not getattr(settings[p["source"]], "legacy_source", None)) * len(config.targets)
    if config.max_requests is not None and required > config.max_requests:
        load_validation_error(
            ["max_requests", "load_test.patterns.concurrent.levels"],
            "The request budget cannot cover even the initial concurrent groups and comparison windows.",
            values={"max_requests": config.max_requests, "required_requests": required},
        )


def planned_arrivals(settings, point):
    """Independent seeded interval and shape RNGs, also used for burst offers."""
    schedule = []
    overflow = False
    if point["pattern"] in {"constant", "poisson"}:
        traffic = TrafficConfig(
            duration_seconds=settings.schedule.duration_seconds,
            arrival="poisson" if point["pattern"] == "poisson" else "steady",
            seed=settings.patterns.poisson.seed,
            rates=[point["rate"]],
        )
        base, overflow = arrival_schedule(traffic, point["rate"], poisson_unconditioned=True)
        schedule += [(t, "arrival", None) for t, _ in base]
    if point["bursts"]:
        schedule += [
            (t, "burst", i)
            for i, t in enumerate(burst_times(settings))
            for _ in range(settings.patterns.bursts.size)
        ]
    schedule.sort(key=lambda row: row[0])
    rng = random.Random(settings.patterns.poisson.seed ^ 0x6A09E667)
    weights = (
        settings.workloads.mixed.weights() if point["type"] == "mixed" else {point["shape"]: 100}
    )
    offers = [
        {
            "offset_seconds": t,
            "origin": origin,
            "burst_index": burst,
            "shape": rng.choices(list(weights), weights=list(weights.values()))[0],
        }
        for t, origin, burst in schedule[:MAX_PLANNED_ARRIVALS]
    ]
    return offers, overflow or len(schedule) > MAX_PLANNED_ARRIVALS


def build_spec(
    run, settings, point, shape, calibration, *, role="request", phase=None, progress=None
):
    size, output = shape.split("_")
    key = size, "short_answer" if output == "short" else "long_output"
    details = {
        **{
            k: point[k]
            for k in ("id", "type", "workload", "pattern", "level", "rate", "bursts", "source")
        },
        "point_id": point["id"],
        "shape": shape,
        "role": role,
        "phase": phase,
    }
    scenario = point["id"] + ("." + phase if phase else "")
    # Never swap a saved question for a synthetic label prompt. Shape targets
    # remain requested targets; the report exposes actual lengths and shortfall.
    sources = getattr(run, "pack_specs", []) or getattr(run, "load_custom_specs", [])
    if sources:
        sources = _shape_sources(settings, size, sources, run.records)
        index = point.get("_source_index", 0)
        point["_source_index"] = index + 1
        if getattr(run, "pack_specs", []):
            cases = {case.fixture_id: case for case in run.config.request_pack.cases}
            rng = random.Random(settings.patterns.poisson.seed + index)
            source = rng.choices(
                sources, weights=[cases[v.workload["case_id"]].weight for v in sources]
            )[0]
        else:
            source = sources[index % len(sources)]
        spec = source.model_copy(deep=True)
        spec.scenario, spec.condition_id, spec.check_ids = scenario, scenario, ["load"]
        spec.load_point = details
        spec.requested_input_tokens = getattr(settings.profile, size).input_tokens
        spec.requested_output_tokens = spec.max_tokens if spec.max_tokens is not None else run.config.max_output_tokens
        # User-provided question budgets and scoring are part of its identity.
    else:
        spec = make_spec(
            settings,
            shape,
            scenario,
            details,
            calibration[key]["filler_chars"],
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        spec.condition_id = scenario
    spec.stream = run.config.stream
    spec.request_timeout_seconds = settings.request_timeout_seconds
    spec.options = deterministic_options(run.config, spec.options)
    return spec


async def concurrent_window(run, target, client, settings, point, calibration, *, phase=None):
    """Refill each slot independently; a minimum sample count never ends a window."""
    begin = time.monotonic()
    end = begin + settings.schedule.duration_seconds
    records, active = [], {}
    changed = asyncio.Event()
    rng = random.Random(settings.patterns.poisson.seed)
    reason = None
    cutoff = None
    triggered = 0
    scenario = point["id"] + ("." + phase if phase else "")
    run.start_condition(target.name, scenario, planned=None, planned_kind="duration")
    intent = ScheduleObservation(
        run.observations[target.name].setdefault("schedule_transitions", []),
        start=begin, origin=run.start, stage_id=scenario, check_id="load",
        kind="concurrency", label=point_label(point) + (f" · {phase}" if phase else ""),
        pattern=point["pattern"],
        concurrency=point["level"] - (1 if phase == "control" else 0),
    )

    def ended():
        nonlocal reason, cutoff
        if time.monotonic() >= end:
            return True
        if not run.allowed(target.name):
            reason = run.condition_reason(target.name)
        elif point.get("request_allowance") is not None and run.budget_counts[target.name] - point["budget_start"] >= point["request_allowance"]:
            reason = "point request allowance used"
        else:
            return False
        cutoff = time.monotonic() if cutoff is None else cutoff
        return True

    async def send(shape, role, progress=None):
        if ended():
            return None
        async with run.slot(False, min(end, run.deadline)) as admitted:
            if not admitted or ended():
                return None
            if role == "probe" and (
                len(active) != point["level"] - 1
                or not all(
                    p.first
                    if settings.workloads.interference.trigger == "streaming_started"
                    else p.chunks >= 5
                    for p in active.values()
                )
            ):
                return None
            spec = build_spec(run, settings, point, shape, calibration, role=role, phase=phase)
            record = await run.request(target, client, spec, event=progress, slot_reserved=True)
            if record is not None:
                records.append(record)
            return record

    async def worker(index):
        while not ended():
            progress = OutputProgress(changed) if phase else None
            if phase:
                active[index] = progress
                shape = "short_short"
            elif point["type"] == "mixed":
                weights = settings.workloads.mixed.weights()
                shape = rng.choices(list(weights), weights=list(weights.values()))[0]
            else:
                shape = point["shape"]
            try:
                record = await send(shape, "short_stream" if phase else "request", progress)
            finally:
                active.pop(index, None)
                changed.set()
            if record is None or asyncio.current_task().cancelling():
                break
            await asyncio.sleep(0)

    async def probe():
        nonlocal triggered
        while not ended():
            changed.clear()
            streams = list(active.values())
            if len(streams) == point["level"] - 1 and all(
                p.first
                if settings.workloads.interference.trigger == "streaming_started"
                else p.chunks >= 5
                for p in streams
            ):
                sent = await send("long_short", "probe")
                triggered += sent is not None
                await asyncio.sleep(0)
                continue  # At most one long request alongside the N-1 shorts.
            try:
                await asyncio.wait_for(
                    changed.wait(), max(0.0001, min(end, run.deadline) - time.monotonic())
                )
            except TimeoutError:
                return

    tasks = [
        asyncio.create_task(worker(i))
        for i in range(point["level"] - 1 if phase else point["level"])
    ]
    if phase == "probe":
        tasks.append(asyncio.create_task(probe()))
    cancelled = None
    sending_end = None
    try:
        # Do not await workers directly: requests may outlive the sending window.
        while time.monotonic() < end and not ended():
            changed.clear()
            try:
                await asyncio.wait_for(
                    changed.wait(), min(0.02, max(0.0001, end - time.monotonic()))
                )
            except TimeoutError:
                pass
        sending_end = min(end, cutoff if cutoff is not None else time.monotonic())
        intent.begin_drain(sending_end)
        finish_end = min(run.deadline, sending_end + settings.schedule.finish_seconds)
        while any(not t.done() for t in tasks) and time.monotonic() < finish_end:
            stop = run.stop_reason(target.name)
            if stop and stop != "maximum request budget reached":
                reason = stop
                break
            await asyncio.wait(tasks, timeout=min(0.02, max(0, finish_end - time.monotonic())))
    except asyncio.CancelledError as exc:
        sending_end = min(end, time.monotonic())
        reason = run.stop_reason(target.name) or "execution cancelled"
        cancelled = exc
    finally:
        if sending_end is None:
            sending_end = min(end, time.monotonic())
        intent.begin_drain(sending_end)
        for task in tasks:
            if not task.done():
                task.cancel()
        cleanup = asyncio.gather(*tasks, return_exceptions=True)
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancelled = exc
                reason = run.stop_reason(target.name) or "execution cancelled"
        intent.finish(time.monotonic())
    now = time.monotonic()
    for record in records:
        if record.status == "cancelled":
            record.error = reason or "finish allowance expired"
            record.load_point["cancellation_reason"] = record.error
    if any(r.status == "cancelled" for r in records) and reason is None:
        reason = "finish allowance expired"
    window = {
        "phase": phase,
        "sending_start_ms": (begin - run.start) * 1000,
        "sending_end_ms": (sending_end - run.start) * 1000,
        "delivered_window_seconds": max(0, sending_end - begin),
        "observation_seconds": now - begin,
        "drain_seconds": max(0, now - sending_end),
        "cut_short": sending_end + 1e-6 < end,
        "window_reason": reason,
        "triggered": triggered,
        "request_ids": [r.id for r in records],
    }
    run.finish_condition(target.name, scenario, reason=reason)
    windows = point.setdefault("measurement_windows", [])
    windows.append(window)
    if phase is None:
        point.update(window)
    else:
        point.update(
            sending_start_ms=windows[0]["sending_start_ms"],
            sending_end_ms=windows[-1]["sending_end_ms"],
            delivered_window_seconds=sum(w["delivered_window_seconds"] for w in windows),
            observation_seconds=sum(w["observation_seconds"] for w in windows),
            drain_seconds=sum(w["drain_seconds"] for w in windows),
            cut_short=any(w["cut_short"] for w in windows),
            window_reason=reason,
        )
    if cancelled:
        raise cancelled
    return records, window


async def arrival_window(run, target, client, settings, point, calibration):
    offers, overflow = planned_arrivals(settings, point)
    point["offers"] = offers
    begin = time.monotonic()
    traffic = TrafficConfig(
        duration_seconds=settings.schedule.duration_seconds,
        rates=[point["rate"] or 1],
        arrival="poisson" if point["pattern"] == "poisson" else "steady",
        max_in_flight=admission_limit(run.config, settings),
        drain_timeout_seconds=settings.schedule.finish_seconds,
        seed=settings.patterns.poisson.seed,
        scheduler_lag_tolerance_ms=SCHEDULER_LAG_MS,
    )

    async def send(spec):
        return await run.request(target, client, spec, slot_reserved=True, budget_reserved=True)

    def stop():
        why = run.stop_reason(target.name)
        return None if why == "maximum request budget reached" else why

    run.start_condition(target.name, point["id"], planned=len(offers))
    evidence = await run_stage(
        traffic=traffic,
        rate_rps=point["rate"] or 1,
        stage_index=0,
        fixtures={},
        execute=send,
        reserve_offer=lambda: (point.get("request_allowance") is None or run.budget_counts[target.name] - point["budget_start"] < point["request_allowance"]) and run.reserve_offer(target.name),
        try_acquire=run.try_acquire_slot,
        release=run.release_slot,
        stop_reason=stop,
        deadline=run.deadline,
        limits=settings.pass_criteria.timing_limits(),
        schedule_override=[(o["offset_seconds"], o["shape"]) for o in offers],
        spec_factory=lambda i: build_spec(run, settings, point, offers[i]["shape"], calibration),
        check_id="load",
        scenario=point["id"],
        run_start=run.start,
        return_evidence=True,
        on_drop=lambda: run.drop_offer(target.name, point["id"]),
        schedule_observations=run.observations[target.name].setdefault("schedule_transitions", []),
        schedule_stage_id=point["id"],
        schedule_label=point_label(point),
        schedule_pattern=point["pattern"],
    )
    begin = evidence.started_monotonic
    for index, (offer, outcome) in enumerate(zip(offers, evidence.outcomes)):
        offer["outcome"] = outcome
        offer["scheduler_lag_ms"] = evidence.offer_lags_ms[index]
    for index, record in evidence.records:
        if record.status == "cancelled":
            record.error = (
                "finish allowance expired"
                if evidence.drain_expired
                else "; ".join(evidence.reasons) or "run stopped"
            )
            record.load_point["cancellation_reason"] = record.error
        record.load_point["origin"] = offers[index]["origin"]
        record.load_point["burst_index"] = offers[index]["burst_index"]
        offers[index]["request_id"] = record.id
        offers[index]["dispatch_delay_ms"] = record.dispatch_lag_ms
    point.update(
        schedule_overflow=overflow,
        admission_limit=traffic.max_in_flight,
        window_reason="; ".join(evidence.reasons) or None,
        cut_short=evidence.interrupted,
        sending_start_ms=(begin - run.start) * 1000,
        sending_end_ms=(begin - run.start + evidence.sending_seconds) * 1000,
        delivered_window_seconds=evidence.sending_seconds,
        observation_seconds=evidence.observation_seconds,
        drain_seconds=max(0, evidence.observation_seconds - evidence.sending_seconds),
    )
    run.finish_condition(target.name, point["id"], reason=point["window_reason"])
    return [r for _, r in evidence.records]


def interference_measurements(records, windows, delay_windows=()):
    """Match only short streams overlapping the actual long-request prefill.

    Baseline uses the same relative interval in its independent full window.
    This reuses #48's answer-window matching, rather than diluting with all shorts.
    """
    delays = _delays(delay_windows)
    baseline = next((w for w in windows if w["phase"] == "baseline"), {})
    probe_window = next((w for w in windows if w["phase"] == "probe"), {})
    probes = [r for r in records if r.load_point.get("role") == "probe"]
    shorts = [
        r
        for r in records
        if r.load_point.get("role") == "short_stream"
        and r.valid
        and r.status == "completed"
        and r.answer_chunks >= 2
    ]
    matched = {"baseline": [], "probe": []}
    intervals = []
    for probe in probes:
        if probe.timing_start_ms is None or probe.first_output_ms is None:
            continue
        a, b = probe.timing_start_ms, probe.timing_start_ms + probe.first_output_ms
        offset = baseline.get("sending_start_ms", 0) - probe_window.get("sending_start_ms", 0)
        intervals.append({"probe_ms": [a, b], "baseline_ms": [a + offset, b + offset]})
        for phase, shift in (("probe", 0), ("baseline", offset)):
            for r in shorts:
                window = answer_window(r)
                if (
                    r.load_point.get("phase") == phase
                    and window
                    and _overlap(window, (a + shift, b + shift), delays)
                ):
                    if r not in matched[phase]:
                        matched[phase].append(r)
    result = {}
    for phase, rows in matched.items():
        result[phase] = {
            "matched_count": len(rows),
            "request_ids": [r.id for r in rows],
            "completion_median_ms": median([r.elapsed_ms for r in rows]) if rows else None,
            "longest_stream_pause_ms": max(
                (r.max_stream_gap_ms for r in rows if r.max_stream_gap_ms is not None), default=None
            ),
        }
    a, b = result["baseline"]["completion_median_ms"], result["probe"]["completion_median_ms"]
    pauses = [result[k]["longest_stream_pause_ms"] for k in ("baseline", "probe")]
    valid_shorts = [r for r in records if r.load_point.get("role") == "short_stream" and r.valid and r.status == "completed"]
    buffered = sum(r.answer_chunks < 2 for r in valid_shorts)
    result.update(
        buffered_stream=bool(valid_shorts) and buffered > len(valid_shorts) / 2,
        buffered_short_streams=buffered,
        valid_short_streams=len(valid_shorts),
        slowdown=b / a if a and b is not None else None,
        matched_measurement_windows=intervals,
        actual_overlap_request_ids=result["probe"]["request_ids"],
        extra_pause_ms=pauses[1] - pauses[0] if all(v is not None for v in pauses) else None,
        triggered=probe_window.get("triggered", 0),
        comparison_metric="short-answer completion time",
    )
    return result


def measurements(run, target, settings, point, records, shape):
    from giraffe.runner import _missing_timing, _timing_violations
    from giraffe.traffic import _timing

    criteria = settings.pass_criteria
    limits = criteria.timing_limits()
    rows = [r for r in records if r.load_point.get("shape") == shape]
    valid = [r for r in rows if r.valid and r.status == "completed" and not reasoning_budget(r)]
    completed = [r for r in rows if r.status == "completed"]
    failed = [
        r
        for r in rows
        if not reasoning_budget(r)
        and (r.status == "failed" or r.status == "completed" and not r.valid)
    ]
    timed_out = [r for r in rows if r.status == "timeout"]
    cancelled = [r for r in rows if r.status == "cancelled"]
    scored = [r for r in valid if r.score is not None]
    offers = [o for o in point.get("offers", []) if o["shape"] == shape]
    local = sum(o.get("outcome") == "dropped_local" for o in offers)
    late = sum(o.get("outcome") == "dropped_late" for o in offers)
    not_offered = sum(o.get("outcome") in {"not_offered", "dispatching"} for o in offers)
    arrival = point["pattern"] not in {"concurrent", "interference"}
    unfinished = [r for r in rows if r.status in {"timeout", "failed"}]
    quality = run_quality(run, [*valid, *unfinished], target.name, configured_metrics(limits))
    qualified = qualified_violations(valid, limits, quality, arrival=arrival)
    raw = (
        [r.id for r in valid if _timing(r, limits)[0]]
        if arrival
        else _timing_violations(valid, limits)
    )
    unfinished_timing = [
        r.model_copy(
            update={
                "elapsed_ms": r.arrival_elapsed_ms
                if r.arrival_elapsed_ms is not None
                else r.elapsed_ms,
                "first_output_ms": r.arrival_first_output_ms,
            }
        )
        if arrival
        else r
        for r in unfinished
    ]
    raw += _timing_violations(unfinished_timing, limits, unfinished=True)
    qualified += qualified_violations(unfinished_timing, limits, quality, unfinished=True)
    missing = _missing_timing(valid, limits)
    if arrival:
        missing += ["arrival_timing"] if any(_timing(r, limits)[1] for r in valid) else []
    durations = [r.arrival_elapsed_ms if arrival else r.elapsed_ms for r in valid]
    first = [r.arrival_first_output_ms if arrival else r.first_output_ms for r in valid]
    # Throughput includes finish-allowance completions in numerator AND elapsed
    # observation time in denominator. Sending-only throughput is also explicit.
    observation = point.get("observation_seconds", 0)
    sending = point.get("delivered_window_seconds", 0)
    finish_count = sum(
        r.completed_ms is not None
        and r.completed_ms
        > (point.get("sending_end_ms") if point.get("sending_end_ms") is not None else math.inf)
        for r in completed
    )
    tokens = [r.output_tokens for r in completed]
    good = [
        r
        for r in valid
        if r.score is True
        and r.id not in raw
        and not (_timing(r, limits)[1] if arrival else _missing_timing([r], limits))
    ]
    a, b = shape.split("_")
    target_tokens, cap = (
        getattr(settings.profile, a).input_tokens,
        getattr(settings.profile, b).output_tokens,
    )
    lengths = {
        "input_target": target_tokens,
        "output_cap": cap,
        "input_tokens": _distribution([r.input_tokens for r in rows if r.input_tokens is not None]),
        "output_tokens": _distribution(
            [r.output_tokens for r in rows if r.output_tokens is not None]
        ),
        "unknown_input_count": sum(r.input_tokens is None for r in rows),
        "unknown_output_count": sum(r.output_tokens is None for r in rows),
        "input_shortfall_tokens": [
            max(0, target_tokens - r.input_tokens) if r.input_tokens is not None else None
            for r in rows
        ],
        "input_method": "endpoint-reported prompt tokens; unknown when absent",
        "output_method": "endpoint-reported completion tokens; unknown when absent",
        "generation_speed_method": "client-observed estimate after first answer read; tokens apportioned by answer characters",
    }
    denominator = len(rows) - len(cancelled)
    group = {
        k: point[k]
        for k in ("id", "type", "workload", "pattern", "level", "rate", "bursts", "source")
    }
    average, peak = inflight(rows, point.get("sending_start_ms"), point.get("sending_end_ms"))
    group.update(
        client_mean_inflight=average,
        client_peak_inflight=peak,
        server_evidenced_concurrency=peak_evidenced_concurrency(
            rows, scheduling_delay_windows(run)
        ),
        requested_share_percent=settings.workloads.mixed.weights()[shape]
        if point["type"] == "mixed"
        else 100,
        observed_request_share_percent=len(rows) / len(records) * 100 if records else None,
    )
    group.update(
        id=point["id"] + "." + shape,
        point_id=point["id"],
        shape=shape,
        requested=scheduled_count(settings, point) if point["type"] != "mixed" else None,
        scheduled=len(offers) if arrival else len(rows),
        dispatched=len(rows),
        not_sent=local + late + not_offered,
        local_drops=local,
        late_arrivals=late,
        not_offered=not_offered,
        completed=len(completed),
        valid=len(valid),
        reasoning_budget_exhausted=sum(reasoning_budget(r) for r in rows),
        failed=len(failed),
        timed_out=len(timed_out),
        cancelled=len(cancelled),
        error_denominator=denominator,
        error_denominator_description="Dispatched responses excluding cancellations; local drops are not sent and are excluded.",
        error_percent=(len(failed) + len(timed_out)) / denominator * 100 if denominator else 0,
        scored=len(scored),
        correct=sum(r.score is True for r in scored),
        unscored=len(valid) - len(scored),
        correct_percent=sum(r.score is True for r in scored) / len(scored) * 100
        if scored
        else None,
        required=criteria.min_valid_responses,
        first_answer_p50_ms=_percentile([v for v in first if v is not None], 0.5),
        first_answer_p95_ms=_percentile([v for v in first if v is not None], 0.95),
        completion_p50_ms=_percentile([v for v in durations if v is not None], 0.5),
        completion_p95_ms=_percentile([v for v in durations if v is not None], 0.95),
        longest_stream_pause_ms=max(
            (r.max_stream_gap_ms for r in valid if r.max_stream_gap_ms is not None), default=None
        ),
        generation_tokens_per_second=_distribution(
            [
                r.generation_tokens_per_second
                for r in valid
                if r.generation_tokens_per_second is not None
            ]
        ),
        timing_clock="planned arrival (includes dispatch delay)" if arrival else "dispatch",
        dispatch_delay_ms=_distribution(
            [r.dispatch_lag_ms for r in rows if r.dispatch_lag_ms is not None]
        ),
        timing_quality=quality,
        timing_violations=qualified,
        unattributed_timing_violations=list(set(raw) - set(qualified)),
        missing_timing=sorted(set(missing)),
        lengths=lengths,
        input_qualification=input_qualification(settings, a, rows, floors=point.get("input_class_floors", {}).get("short_answer" if b == "short" else "long_output")),
        completed_during_finish=finish_count,
        throughput_window_seconds=observation,
        sending_window_seconds=sending,
        throughput_description="Completions through finish allowance / sending plus actual finishing time; cancelled work excluded.",
        completed_requests_per_second=len(completed) / observation if observation else None,
        aggregate_output_tokens_per_second=sum(tokens) / observation
        if observation and tokens and all(n is not None for n in tokens)
        else None,
        completed_in_sending_window=len(completed) - finish_count,
        correct_work_meeting_limits=len(good),
        correct_work_per_second=len(good) / observation if observation else None,
        request_ids=[r.id for r in rows],
    )
    if getattr(run, "pack_specs", []):
        eligible = _shape_sources(settings, a, run.pack_specs, run.records)
        group["case_coverage"] = {
            spec.workload["case_id"]: sum(
                r.workload.get("case_id") == spec.workload["case_id"] and r.score is not None
                for r in valid
            )
            for spec in eligible
        }
    if getattr(run, "pack_specs", []) or getattr(run, "load_custom_specs", []):
        group["saved_question_budgets"] = []
        seen = set()
        for record in rows:
            if record.requested_max_tokens == cap:
                continue
            key = (record.workload.get("case_id", record.fixture_id), record.requested_max_tokens)
            if key in seen:
                continue
            seen.add(key)
            group["saved_question_budgets"].append({
                "case_id": key[0], "profile_cap": cap, "sent_cap": record.requested_max_tokens,
                "note": f"Saved question budget {record.requested_max_tokens} tokens used instead of profile {b.title()} output cap {cap}",
            })
    if point["type"] == "stress":
        threshold = stress_output_floor(settings) if b == "long" else None
        group["coverage"] = {
            "threshold_tokens": threshold,
            "profile_output_cap": settings.profile.long.output_tokens,
            "min_output_percent": settings.workloads.stress.min_output_percent,
            "sent_budgets": sorted({r.requested_max_tokens for r in rows}),
            "output_covered": sum(
                r.output_tokens is not None and r.output_tokens >= threshold for r in valid
            )
            if b == "long"
            else None,
            "input_verified": sum(
                r.input_tokens is not None
                and abs(r.input_tokens - target_tokens) <= target_tokens * settings.input_token_tolerance
                for r in valid
            ),
            "required": criteria.min_valid_responses,
        }
    return group


def score_group(settings, point, group, records):
    c = settings.pass_criteria

    def result(status, code, reason):
        group.update(status=status, reason_code=code, reason=reason)
        return group

    if point.get("blocked"):
        return result("blocked", "context_exceeded", point["blocked"])
    if point.get("calibration_failure"):
        return result("fail", "calibration_failed", point["calibration_failure"])
    if group["error_percent"] is not None and group["error_percent"] > c.max_error_percent:
        return result("fail", "error_rate", "Endpoint errors/timeouts exceed Errors allowed (%).")
    rows = [r for r in records if r.load_point.get("shape") == group["shape"]]
    if any(
        r.completion_tokens is not None
        and r.requested_max_tokens
        and r.completion_tokens > r.requested_max_tokens
        for r in rows
    ):
        return result("fail", "output_cap", "Endpoint exceeded the requested output cap.")
    if group["correct_percent"] is not None and group["correct_percent"] < c.min_correct_percent:
        return result(
            "fail",
            "correctness",
            "Correct answers among scored completions are below the requirement; missing judgments remain unknown.",
        )
    if group["timing_violations"]:
        return result(
            "fail", "timing", "A response exceeded a per-response timing or generation-speed limit."
        )
    if group["unattributed_timing_violations"]:
        return result(
            "inconclusive",
            "client_delay",
            "Client scheduling delay overlapped a response limit violation.",
        )
    if point.get("cut_short"):
        return result(
            "inconclusive",
            window_cause(point.get("window_reason")),
            "Test truncated: " + (point.get("window_reason") or "run limit"),
        )
    if point.get("schedule_overflow") or group["not_sent"]:
        return result(
            "inconclusive",
            "incomplete_delivery",
            "Scheduled requests were not sent; local drops/lateness are client delivery evidence, not endpoint errors.",
        )
    if group["cancelled"]:
        return result(
            "inconclusive",
            "cancelled",
            "Unfinished requests were cancelled after the finish allowance or run stop.",
        )
    if point.get("input_not_calibrated"):
        return result("inconclusive", "input_not_calibrated", "Input target calibration was incomplete.")
    if group["valid"] < c.min_valid_responses:
        return result(
            "inconclusive",
            "insufficient_responses",
            "Too few valid responses in this result group.",
        )
    if c.min_correct_percent > 0 and (group["scored"] < c.min_valid_responses or group["unscored"]):
        return result(
            "inconclusive",
            "missing_judgments",
            "Insufficient scored responses or missing judgments.",
        )
    if group.get("case_coverage") and any(count == 0 for count in group["case_coverage"].values()):
        return result(
            "inconclusive",
            "missing_case_coverage",
            "An active saved question lacks scored evidence in this result group.",
        )
    if group["missing_timing"]:
        return result(
            "inconclusive",
            "missing_timing",
            "Required response timing/speed measurements are missing.",
        )
    if group["shape"].startswith("long_") and any(r.input_tokens and r.cached_prompt_tokens is not None and r.cached_prompt_tokens > r.input_tokens / 2 for r in rows):
        return result("inconclusive", "cached_input", "More than half the long input was cached.")
    coverage = group.get("coverage")
    if coverage and coverage["threshold_tokens"] is not None:
        insufficient = [budget for budget in coverage.get("sent_budgets", []) if budget < coverage["threshold_tokens"]]
        if insufficient:
            return result("inconclusive", "insufficient_coverage", stress_budget_reason(settings, min(insufficient)))
    qualification = group.get("input_qualification")
    if qualification and not qualification["qualified"]:
        return result("inconclusive", "input_not_verified", qualification["reason"])
    if coverage:
        if group["shape"].startswith("long_") and coverage["input_verified"] < group["valid"]:
            return result(
                "inconclusive", "input_not_verified",
                "Large-input token coverage was not verified for every valid response.",
            )
        if coverage["output_covered"] is not None and coverage["output_covered"] < group["valid"]:
            return result(
                "inconclusive",
                "insufficient_coverage",
                "An answer ended before the required long-output coverage; length alone is not endpoint failure.",
            )
    if point["pattern"] == "concurrent" and not point.get("load_qualified"):
        return result(
            "inconclusive",
            "load_not_delivered",
            "Requested maintained concurrency was not exercised with sufficient occupancy and streamed overlap.",
        )
    if (
        point["type"] != "interference"
        and c.max_first_answer_ms is None
        and c.max_completion_ms is None
    ):
        return result(
            "inconclusive",
            "measure_only",
            "Measure only: set a first-answer or completion limit to establish accepted capacity.",
        )
    return result("pass", "criteria_met", "Per-response criteria and result-group evidence met.")


def assess_point(run, target, settings, point, records):
    point.update(
        request_ids=[r.id for r in records],
        dispatched=len(records),
        timing_clock="dispatch"
        if point["pattern"] in {"concurrent", "interference"}
        else "planned arrival (includes dispatch delay)",
    )
    average, peak = inflight(records, point.get("sending_start_ms"), point.get("sending_end_ms"))
    evidenced = peak_evidenced_concurrency(records, scheduling_delay_windows(run))
    point.update(
        client_mean_inflight=average,
        client_peak_inflight=peak,
        server_evidenced_concurrency=evidenced,
        load_qualified=point["level"] == 1
        or bool(
            run.config.stream
            and point["level"] is not None
            and evidenced >= point["level"]
            and average >= 0.9 * point["level"]
        ),
    )
    weights = settings.workloads.mixed.weights() if point["type"] == "mixed" else {}
    point["observed_mix"] = {
        s: {
            "requested_percent": v,
            "dispatched": sum(r.load_point["shape"] == s for r in records),
            "observed_percent": sum(r.load_point["shape"] == s for r in records)
            / len(records)
            * 100
            if records
            else None,
        }
        for s, v in weights.items()
    }
    point["groups"] = [
        score_group(
            settings, point, measurements(run, target, settings, point, records, s), records
        )
        for s in point_shapes(settings, point)
    ]
    point["scheduled"] = len(point["offers"]) if "offers" in point else None
    for field in (
        "valid",
        "failed",
        "timed_out",
        "cancelled",
        "reasoning_budget_exhausted",
        "local_drops",
        "late_arrivals",
        "not_sent",
    ):
        point[field] = sum(g[field] for g in point["groups"])
    status = aggregate([g["status"] for g in point["groups"]])
    reason = next((g["reason"] for g in point["groups"] if g["status"] == status), "No evidence.")
    if point["type"] == "interference":
        evidence = interference_measurements(
            records, point.get("measurement_windows", []), scheduling_delay_windows(run)
        )
        point["interference"] = evidence
        matched_ids = set(evidence["baseline"]["request_ids"] + evidence["probe"]["request_ids"])
        evidence["timing_quality"] = run_quality(
            run,
            [r for r in records if r.id in matched_ids],
            target.name,
            ("latency_ms", "max_stream_gap_ms"),
        )
        n = settings.pass_criteria.min_valid_responses
        exercised = evidence["triggered"] and all(
            evidence[k]["matched_count"] >= n for k in ("baseline", "probe")
        )
        window_load = []
        for window in point.get("measurement_windows", []):
            shorts = [
                r
                for r in records
                if r.load_point.get("phase") == window["phase"]
                and r.load_point.get("role") == "short_stream"
            ]
            average, _ = inflight(shorts, window["sending_start_ms"], window["sending_end_ms"])
            observed = peak_evidenced_concurrency(shorts, scheduling_delay_windows(run))
            window_load.append(
                {
                    "phase": window["phase"],
                    "mean_inflight": average,
                    "evidenced_concurrency": observed,
                }
            )
        evidence["short_stream_load"] = window_load
        evidence["input_qualification"] = {
            phase: input_qualification(settings, "short", [
                r for r in records if r.load_point.get("phase") == phase
                and r.load_point.get("role") == "short_stream"
            ], floors=point.get("input_class_floors", {}).get("short_answer")) for phase in ("baseline", "probe")
        }
        evidence["input_qualification"]["long_probe"] = input_qualification(settings, "long", [
            r for r in records if r.load_point.get("role") == "probe"
        ], floors=point.get("input_class_floors", {}).get("short_answer"))
        exercised = (
            exercised
            and len(window_load) == 2
            and all(
                v["mean_inflight"] >= 0.9 * (point["level"] - 1)
                and v["evidenced_concurrency"] >= point["level"] - 1
                for v in window_load
            )
        )
        if status not in {"fail", "blocked"}:
            if not evidence["input_qualification"]["long_probe"]["qualified"]:
                status, reason = "inconclusive", "long-input probe not exercised: " + evidence["input_qualification"]["long_probe"]["reason"]
            elif not all(evidence["input_qualification"][phase]["qualified"] for phase in ("baseline", "probe")):
                status, reason = "inconclusive", "Short input not exercised in both short-stream comparison windows."
            elif not exercised or evidence["slowdown"] is None:
                status, reason = (
                    "inconclusive",
                    "Trigger or matched overlap was not exercised sufficiently in both full comparison windows.",
                )
            elif evidence["slowdown"] > settings.workloads.interference.max_slowdown:
                if evidence["timing_quality"]["affected_request_ids"]:
                    status, reason = (
                        "inconclusive",
                        "Client scheduling delay overlapped the matched short-answer slowdown.",
                    )
                else:
                    status, reason = (
                        "fail",
                        "Short-answer completion slowdown exceeded the workload limit.",
                    )
    code = next((g["reason_code"] for g in point["groups"] if g["status"] == status), "comparison")
    point.update(status=status, reason=reason, reason_code=code)
    bursts = {o["burst_index"]: [] for o in point.get("offers", []) if o.get("origin") == "burst"}
    for r in records:
        if r.load_point.get("origin") == "burst":
            bursts.setdefault(r.load_point["burst_index"], []).append(r)
    point["burst_delivery"] = [
        {
            "index": i,
            "scheduled": sum(o.get("burst_index") == i for o in point.get("offers", [])),
            "dispatched": len(rows),
            "not_sent": sum(
                o.get("burst_index") == i
                and o.get("outcome")
                in {"dropped_local", "dropped_late", "not_offered", "dispatching"}
                for o in point.get("offers", [])
            ),
            "spread_ms": max(r.dispatch_ms for r in rows) - min(r.dispatch_ms for r in rows)
            if rows and all(r.dispatch_ms is not None for r in rows)
            else None,
            "completion_p50_ms": _percentile(
                [r.arrival_elapsed_ms for r in rows if r.arrival_elapsed_ms is not None], 0.5
            ),
        }
        for i, rows in bursts.items()
    ]
    point["response_by_origin"] = {
        origin: {
            "dispatched": len(rows),
            "completion_p50_ms": _percentile(
                [r.arrival_elapsed_ms for r in rows if r.arrival_elapsed_ms is not None], 0.5
            ),
            "failed": sum(r.status in {"failed", "timeout"} for r in rows),
        }
        for origin in ("arrival", "burst")
        if (rows := [r for r in records if r.load_point.get("origin") == origin])
    }
    point.pop("_source_index", None)
    point.pop("budget_start", None)
    return point


def publish_point(run, target, point):
    # Live cards use aggregate delivery evidence; the complete offered schedule
    # remains in the report rather than being repeated in every progress event.
    payload = {key: value for key, value in point.items() if key != "offers"}
    for phase in ("baseline", "probe") if point["type"] == "interference" else (None,):
        scenario = point["id"] + ("." + phase if phase else "")
        run.finish_condition(
            target.name, scenario, reason=point.get("window_reason") or ("dropped: all request slots busy" if point.get("local_drops") else None) or point["reason"], load_point_result=payload
        )


async def execute(run, target, client):
    settings = run.config.load_test
    own = dict(settings_sets(settings))
    points = test_points(settings)
    observation = {
        "points": points,
        "plan": load_time_plan(settings),
        "migration_notes": list(run.config._migration_notes),
        "calibrations": [],
        "workload_source": "request_pack"
        if getattr(run, "pack_specs", [])
        else "custom_fixtures"
        if run.config.custom_fixtures
        else "built-in token-target prompts",
        "effective_settings": settings.model_dump(mode="json"),
        "effective_concurrency_ceiling": run.config.concurrency,
        "seed": settings.patterns.poisson.seed,
        "context_windows": {target.name: context_window(target, run.observations[target.name])[0]},
        "context_note": "Unknown endpoint context limits are not inferred from client Context length settings.",
    }
    run.observations[target.name]["load_test"] = observation
    if run.config.custom_fixtures:
        from giraffe.fixtures import load_custom_fixtures

        run.load_custom_specs = load_custom_fixtures(
            run.config.custom_fixtures,
            legacy_max_output_tokens=run.config.legacy_custom_fixture_max_output_tokens,
        )
    calibration = {}
    for point in points:
        for phase in ("baseline", "probe") if point["type"] == "interference" else (None,):
            scenario = point["id"] + ("." + phase if phase else "")
            run.plan_condition(target.name, scenario, planned=None, planned_kind="duration")
    cancellation = None
    reserved_checks = later_reservation(run.config)
    for index, point in enumerate(points):
        cfg = own[point["source"]]
        records = []
        point.update(
            cut_short=False,
            not_run=True,
            delivered_window_seconds=0,
            observation_seconds=0,
            drain_seconds=0,
            window_reason=None,
            sending_start_ms=None,
            sending_end_ms=None,
        )
        shapes = point_shapes(cfg, point)
        context, source = context_window(target, run.observations[target.name])
        if context is not None and any(
            getattr(cfg.profile, s.split("_")[0]).input_tokens
            + getattr(cfg.profile, s.split("_")[1]).output_tokens
            > context
            for s in shapes
        ):
            point["blocked"] = f"Request targets exceed known context window {context} ({source})."
        elif point["source"] in calibration and any(v["status"] == "fail" for v in calibration[point["source"]].values()):
            point["calibration_failure"] = next(v["reason"] for v in calibration[point["source"]].values() if v["status"] == "fail")
        elif run.allowed(target.name) and cancellation is None:
            if point["source"] not in calibration:
                calibration[point["source"]] = {}
            cache = calibration[point["source"]]
            if not (getattr(run, "pack_specs", []) or getattr(run, "load_custom_specs", [])):
                for shape in shapes:
                    a, b = shape.split("_")
                    key = a, "short_answer" if b == "short" else "long_output"
                    if key not in cache:
                        try:
                            cache[key] = await calibrate(run, target, client, cfg, *key)
                        except asyncio.CancelledError as exc:
                            cancellation = exc
                            point.update(cut_short=True, window_reason=run.stop_reason(target.name) or "execution cancelled")
                            break
                        observation["calibrations"].append(
                            {"source": point["source"], **cache[key]}
                        )
                if any(v["status"] == "fail" for v in cache.values()):
                    point["calibration_failure"] = next(
                        v["reason"] for v in cache.values() if v["status"] == "fail"
                    )
                elif any(v["status"] == "inconclusive" for v in cache.values()):
                    point["input_not_calibrated"] = True
                point["input_class_floors"] = {
                    variant: {size: value["input_tokens"] for (size, kind), value in cache.items()
                              if kind == variant and value["status"] == "calibrated"
                              and value["reason_code"] == "minimum_prompt_length"
                              and value["input_tokens"] is not None}
                    for variant in ("short_answer", "long_output")
                }
            if cancellation is None and not point.get("calibration_failure") and not point.get("input_not_calibrated"):
                remaining = run.remaining(target.name)
                reserved = reserved_checks + sum((p["level"] * 2 - 1 if p["type"] == "interference" else p["level"] or 0) for p in points[index + 1:])
                point.update(request_allowance=max(0, remaining - reserved) if remaining is not None else None, reserved_later_requests=reserved, budget_start=run.budget_counts[target.name])
                try:
                    if point["type"] == "interference":
                        for phase in ("baseline", "probe"):
                            rows, window = await concurrent_window(
                                run, target, client, cfg, point, cache, phase=phase
                            )
                            records += rows
                            point["cut_short"] |= window["cut_short"]
                            point["window_reason"] = point["window_reason"] or window["window_reason"]
                        windows = point["measurement_windows"]
                        point.update(
                            sending_start_ms=windows[0]["sending_start_ms"],
                            sending_end_ms=windows[-1]["sending_end_ms"],
                            delivered_window_seconds=sum(
                                w["delivered_window_seconds"] for w in windows
                            ),
                            observation_seconds=sum(w["observation_seconds"] for w in windows),
                            drain_seconds=sum(w["drain_seconds"] for w in windows),
                        )
                    elif point["pattern"] == "concurrent":
                        records, window = await concurrent_window(
                            run, target, client, cfg, point, cache
                        )
                        point.update(window)
                    else:
                        records = await arrival_window(run, target, client, cfg, point, cache)
                    point["not_run"] = False
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    point.update(cut_short=True, not_run=False, window_reason=run.stop_reason(target.name) or "execution cancelled")
                    records = [r for r in run.records if r.target == target.name and (r.load_point or {}).get("point_id") == point["id"]]
        else:
            point.update(cut_short=True, window_reason=run.condition_reason(target.name))
        assess_point(run, target, cfg, point, records)
        publish_point(run, target, point)
    if cancellation is not None and run.stop_reason(target.name) is None:
        raise cancellation
    return observation


def result(run, target):
    observation = run.observations[target.name].get("load_test")
    if observation is None:
        observation = {
            "points": test_points(run.config.load_test),
            "migration_notes": list(run.config._migration_notes),
            "plan": load_time_plan(run.config.load_test),
        }
    settings = dict(settings_sets(run.config.load_test))
    for point in observation["points"]:
        # Recompute after deferred AI judgments, retaining inference measurements.
        rows = [
            r
            for r in run.records
            if r.target == target.name
            and r.load_point
            and r.load_point.get("point_id") == point["id"]
        ]
        if "status" not in point:
            point.update(
                cut_short=True,
                window_reason=run.condition_reason(target.name),
                delivered_window_seconds=point.get("delivered_window_seconds", 0),
                observation_seconds=point.get("observation_seconds", 0),
            )
        assess_point(run, target, settings[point["source"]], point, rows)
        publish_point(run, target, point)
    accepted = {}
    for point in observation["points"]:
        if point["status"] != "pass" or point["pattern"] == "interference":
            continue
        key = (
            point["source"],
            point["type"],
            point["workload"],
            point["pattern"],
            bool(point["bursts"]),
        )
        value = point["level"] if point["pattern"] == "concurrent" else point["rate"]
        if value is not None:
            row = accepted.setdefault(
                key,
                {
                    "source": point["source"],
                    "workload": point["workload"],
                    "type": point["type"],
                    "pattern": point["pattern"],
                    "bursts": point["bursts"],
                    "qualification": "highest tested, not an absolute capacity limit",
                    "label": "Highest accepted concurrency"
                    if point["pattern"] == "concurrent"
                    else "Highest accepted request rate",
                    "value": value,
                },
            )
            row["value"] = max(row["value"], value)
    observation["highest_accepted"] = list(accepted.values())
    status = aggregate([p["status"] for p in observation["points"]])
    summary = (
        f"{len(observation['points'])} sequential load tests: "
        + "; ".join(
            f"{k}: {v}" for k, v in Counter(p["status"] for p in observation["points"]).items()
        )
        + "."
    )
    if status != "pass":
        deciding = [p for p in observation["points"] if p["status"] == status]
        summary += " " + "; ".join(f"{point_label(p)}: {p['reason']}" for p in deciding[:3])
        if len(deciding) > 3:
            summary += f"; and {len(deciding) - 3} more"
        summary += "."
    return CheckResult(
        id="load",
        title="Load test",
        target=target.name,
        status=status,
        required=True,
        summary=summary,
        metrics=observation,
        evidence_ids=[r.id for r in run.records if r.target == target.name and r.load_point],
    )
