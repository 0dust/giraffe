"""Duration-based concurrent load, with individual replenishment and shared budgets."""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from statistics import median

from giraffe.fixtures import deterministic_options, reasoning_budget
from giraffe.models import RequestSpec, load_shape_label
from giraffe.overlap import answer_window, peak_evidenced_concurrency, scheduling_delay_windows
from giraffe.timing import run_quality

LOAD_TITLES = {"combinations": "Input → output combinations", "mixed": "Mixed load",
               "interference": "Long input during an active short stream", "stress": "Large-input / large-output stress"}


def running_shapes(settings):
    shapes = list(settings.combinations.selected) if settings.combinations.enabled else []
    if settings.mixed.enabled:
        shapes += [shape for shape, share in settings.mixed.shares.items() if share > 0]
    if settings.interference.enabled:
        shapes += ["short_short", "long_short"]
    if settings.stress.enabled:
        shapes += (["long_short"] if settings.stress.large_input else []) + (["short_long"] if settings.stress.large_output else [])
    return list(dict.fromkeys(shapes))


def context_window(target, observation=None):
    deployment = (observation or {}).get("deployment") or {}
    facts = deployment.get("fields", {}).get("serving.context_limit", {})
    for kind in ("reported", "configured"):
        fact = facts.get(kind, {})
        value = fact.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value), f"deployment.fields.serving.context_limit.{kind} ({fact.get('provenance', kind)})"
    serving = target.deployment.serving
    value = serving.get("context_limit")
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value), "deployment.serving.context_limit"
    return None, None


def test_points(settings):
    points = []
    for kind in LOAD_TITLES:
        if not getattr(settings, kind).enabled:
            continue
        workloads = (settings.combinations.selected if kind == "combinations" else ["mixed"] if kind == "mixed"
                     else ["interference"] if kind == "interference" else
                     (["input"] if settings.stress.large_input else []) + (["output"] if settings.stress.large_output else []))
        for workload in workloads:
            for level in settings.schedule.levels:
                shape = workload if kind == "combinations" else {"input": "long_short", "output": "short_long"}.get(workload)
                identifier = (f"load_combo_{workload}_c{level}" if kind == "combinations" else
                              f"load_mixed_c{level}" if kind == "mixed" else
                              f"load_interference_probe_c{level}" if kind == "interference" else f"load_stress_{workload}_c{level}")
                points.append({"id": identifier, "type": kind, "workload": workload, "level": level,
                    "shape": shape, "configured_window_seconds": settings.schedule.duration_seconds * (2 if kind == "interference" else 1)})
    return points


def load_time_plan(settings):
    points = test_points(settings)
    return {kind: {"title": LOAD_TITLES[kind], "test_points": len(rows),
                  "workloads": len({p["workload"] for p in rows}) * (2 if kind == "interference" else 1),
                  "levels": list(settings.schedule.levels), "duration_seconds": settings.schedule.duration_seconds,
                  "configured_load_seconds": sum(p["configured_window_seconds"] for p in rows)}
            for kind in LOAD_TITLES if (rows := [p for p in points if p["type"] == kind])}


def calibration_keys(settings):
    return list(dict.fromkeys((shape.split("_")[0], "short_answer" if shape.split("_")[1] == "short" else "long_output")
                             for shape in running_shapes(settings)))


def minimum_reservation(settings):
    return len(test_points(settings)) + len(calibration_keys(settings))


def prompt(filler_chars, variant):
    instruction = ("Write the box label first, with no introduction." if variant == "short_answer" else
                   "Write the box label first, then continue a numbered list from 1 to 1000000, well beyond the output cap.")
    filler = ("The team reviewed the neutral delivery notes. " * (filler_chars // 44 + 1))[:filler_chars]
    return f"Request {uuid.uuid4().hex}. The box label is K7P4.\n{filler}\n{instruction}"


def prompt_overhead(variant):
    return math.ceil(len(prompt(0, variant)) / 4)


def window_cause(reason):
    reason = (reason or "").lower()
    if any(word in reason for word in ("budget", "allowance")):
        return "budget_limited"
    if any(word in reason for word in ("time limit", "duration", "deadline")):
        return "time_limited"
    return "run_stopped"


def make_spec(settings, shape, scenario, point, filler_chars, *, calibration=False, request_timeout_seconds=None):
    input_size, output_size = shape.split("_")
    variant = "short_answer" if output_size == "short" else "long_output"
    text = prompt(filler_chars, variant)
    cap = 1 if calibration else getattr(settings.profile, output_size).output_tokens
    condition_id = None
    if point.get("type") == "interference":
        condition_id = f"load_interference_{'probe' if (point.get('sub_window') or 0) % 2 else 'control'}_c{point['level']}"
    return RequestSpec(condition_id=condition_id, fixture_id=f"load.{shape}.{variant}", scenario=scenario, check_ids=["load"],
        messages=[{"role": "user", "content": text}], scorer="none" if calibration else "label", expected=None if calibration else "K7P4",
        input_chars=len(text), max_tokens=cap, requested_input_tokens=getattr(settings.profile, input_size).input_tokens,
        requested_output_tokens=cap, request_timeout_seconds=request_timeout_seconds, load_point=point)


async def calibrate(run, target, client, settings, size, variant):
    config = run.config.model_copy(update={"request_timeout_seconds":settings.request_timeout_seconds})
    target_tokens = getattr(settings.profile, size).input_tokens
    overhead = len(prompt(0, variant))
    filler = max(0, target_tokens * 4 - overhead)
    scenario = f"load_calibration_v45_{size}_{variant}"
    run.plan_condition(target.name, scenario, planned=settings.calibration_attempts, planned_kind="upper_bound")
    run.start_condition(target.name, scenario, planned=settings.calibration_attempts, planned_kind="upper_bound")
    result = {"size": size, "variant": variant, "target_tokens": target_tokens, "status": "inconclusive",
              "reason": "input length not calibrated", "reason_code": "input_not_calibrated", "filler_chars": filler,
              "input_tokens": None, "request_ids": []}
    shape = f"{size}_{'short' if variant == 'short_answer' else 'long'}"
    try:
        for _ in range(settings.calibration_attempts):
            if not run.allowed(target.name):
                result.update(reason=run.condition_reason(target.name), reason_code=window_cause(run.condition_reason(target.name)))
                break
            spec = make_spec(settings, shape, scenario, {"type": "calibration", "input_size": size, "variant": variant}, filler, calibration=True,
                             request_timeout_seconds=config.request_timeout_seconds)
            spec.stream = run.config.stream
            spec.options = deterministic_options(run.config)
            record = await run.request(target, client, spec)
            if record is None:
                result.update(reason=run.condition_reason(target.name), reason_code=window_cause(run.condition_reason(target.name)))
                break
            result["request_ids"].append(record.id)
            tokens = record.input_tokens
            if record.status == "timeout" or (record.http_status is not None and record.http_status >= 400) or record.status == "failed" and tokens is None:
                result.update(status="fail", reason=f"Calibration failed: {record.error or record.status}", reason_code="calibration_failed")
                break
            if record.status not in {"completed", "failed"}:
                reason = run.condition_reason(target.name)
                result.update(reason=reason, reason_code=window_cause(reason))
                break
            result.update(input_tokens=tokens, filler_chars=filler)
            if tokens is None:
                result.update(status="estimated", reason="estimated; endpoint did not report token counts", reason_code="tokens_not_reported")
                break
            if abs(tokens - target_tokens) <= target_tokens * settings.input_token_tolerance:
                result.update(status="calibrated", reason=None, reason_code=None)
                break
            if filler == 0 and tokens > target_tokens:
                result.update(status="calibrated", reason_code="minimum_prompt_length",
                    reason=f"{size.capitalize()} input target {target_tokens} is below the minimum prompt length (≈{tokens} tokens); measured at {tokens}")
                break
            filler = max(0, round((overhead + filler) * target_tokens / max(1, tokens)) - overhead)
    finally:
        run.finish_condition(target.name, scenario, reason=result["reason"])
    return result


class OutputProgress:
    def __init__(self, changed):
        self.first = False
        self.chunks = 0
        self.changed = changed

    def set(self):
        self.first = True
        self.changed.set()

    def on_answer_chunk(self):
        self.chunks += 1
        self.changed.set()


def point_shapes(settings, point):
    if point["type"] == "mixed":
        return sorted(shape for shape, share in settings.mixed.shares.items() if share > 0)
    return ["short_short", "long_short"] if point["type"] == "interference" else [point["shape"]]


def _conditions(point):
    if point["type"] == "interference":
        return [f"load_interference_{name}_c{point['level']}" for name in ("control", "probe")]
    return [point["id"]]



def interference_measurements(records, level, duration, latencies=(), never_triggered=0):
    short = [r for r in records if r.load_point["role"] == "short_stream" and r.valid and r.status == "completed"]
    probes = [r for r in records if r.load_point["role"] == "probe"]
    groups, matched_groups, ambiguous = {}, {0: [], 1: []}, []
    for record in short:
        window = answer_window(record)
        if record.answer_chunks < 2 or window is None:
            continue
        overlaps = {p.load_point["sub_window"] % 2 for p in probes
                    if p.first_output_ms is not None and p.timing_start_ms is not None
                    and max(window[0], p.timing_start_ms) < min(window[1], p.timing_start_ms + p.first_output_ms)}
        if len(overlaps) > 1:
            ambiguous.append(record.id)
        elif overlaps:
            matched_groups[next(iter(overlaps))].append(record)
    for name, parity in (("control", 0), ("probe", 1)):
        matched = matched_groups[parity]
        groups[name] = {"matched_count": len(matched), "request_ids": [r.id for r in matched],
            "decode_median_ms": _median([r.last_output_ms - r.first_output_ms for r in matched]),
            "response_median_ms": _median([r.elapsed_ms for r in matched]),
            "longest_stream_pause_ms": max((r.max_stream_gap_ms for r in matched if r.max_stream_gap_ms is not None), default=None)}
    def ratio(field):
        a, b = groups["control"][field], groups["probe"][field]
        return b / a if a is not None and a > 0 and b is not None else None
    return {**groups, "slowdown": ratio("decode_median_ms"), "response_time_ratio": ratio("response_median_ms"),
            "buffered_stream": sum(r.answer_chunks < 2 for r in short) > len(short) / 2,
            "valid_short_streams": len(short), "buffered_short_streams": sum(r.answer_chunks < 2 for r in short),
            "ambiguous_short_streams": len(ambiguous), "ambiguous_request_ids": ambiguous,
            "allocation": f"{level - 1} short {'stream' if level == 2 else 'streams'} + 1 probe slot alternating short/long every {duration / 4:g} s",
            "trigger_latency_ms": _distribution(list(latencies)), "probes_never_triggered": never_triggered,
            "sub_windows": [{"index": i, "phase": "B" if i % 2 else "A", "probe_shape": "long_short" if i % 2 else "short_short",
                             "configured_seconds": duration / 4} for i in range(4)]}


def _median(values):
    return median(values) if values else None


def _percentile(values, percent):
    return sorted(values)[max(0, math.ceil(len(values) * percent) - 1)] if values else None


def _distribution(values):
    return {"median": _median(values), "min": min(values, default=None), "max": max(values, default=None)}


def inflight(records, start, end):
    area, events = 0., []
    if start is not None and end is not None:
        for r in records:
            a, b = max(start, r.dispatch_ms if r.dispatch_ms is not None else start), min(end, r.completed_ms if r.completed_ms is not None else end)
            if b > a:
                area += b - a
                events.extend(((a, 1), (b, -1)))
    active = peak = 0
    for _, delta in sorted(events, key=lambda e: (e[0], e[1])):
        active += delta
        peak = max(peak, active)
    average = area / (end - start) if start is not None and end > start else 0
    return average, peak


def measurements(run, target, settings, point, records):
    completed = [r for r in records if r.status == "completed"]
    exhausted = [r for r in records if reasoning_budget(r)]
    valid = [r for r in completed if r.valid and r not in exhausted]
    scored = [r for r in records if r.score is not None and r not in exhausted]
    failed = sum(r not in exhausted and (r.status == "failed" or r.status == "completed" and not r.valid) for r in records)
    timeouts = sum(r.status == "timeout" for r in records)
    cancelled = sum(r.status == "cancelled" for r in records)
    population = len(records) - cancelled
    start, end = point.get("sending_start_ms"), point.get("sending_end_ms")
    average, peak = inflight(records, start, end)
    evidenced = peak_evidenced_concurrency(records, scheduling_delay_windows(run))
    response = [r.elapsed_ms for r in valid]
    first = [r.first_output_ms for r in valid if r.first_output_ms is not None]
    first_completion = min((r.completed_ms for r in completed if r.completed_ms is not None), default=None)
    steady = max(0, (end - first_completion) / 1000) if end is not None and first_completion is not None else 0
    steady_records = [r for r in completed if r.completed_ms is not None and first_completion <= r.completed_ms <= end] if steady > 0 else []
    output = [r.completion_tokens for r in steady_records]
    shapes = point_shapes(settings, point)
    token_lengths = {}
    for shape in shapes:
        cohort = [r for r in records if r.load_point["shape"] == shape]
        a, b = shape.split("_")
        token_lengths[shape] = {"input_target": getattr(settings.profile, a).input_tokens,
            "output_cap": getattr(settings.profile, b).output_tokens,
            "input_tokens": _distribution([r.input_tokens for r in cohort if r.input_tokens is not None]),
            "output_tokens": _distribution([r.completion_tokens for r in cohort if r.completion_tokens is not None]),
            "cached_prompt_tokens": _distribution([r.cached_prompt_tokens for r in cohort if r.cached_prompt_tokens is not None])}
    point.update(sent=len(records), completed=len(completed), failed=failed, timed_out=timeouts, cancelled=cancelled,
        valid=len(valid), required=settings.pass_criteria.min_valid_responses, scored=len(scored), correct=sum(r.score is True for r in scored),
        reasoning_budget_exhausted=len(exhausted), error_percent=(failed + timeouts) / population * 100 if population else 0,
        correct_percent=sum(r.score is True for r in scored) / len(scored) * 100 if scored else None,
        correctness_applicable=bool(scored), requested_concurrency=point["level"], client_mean_inflight=average,
        client_peak_inflight=peak, server_evidenced_concurrency=evidenced,
        load_qualified=point["level"] == 1 or run.config.stream and evidenced >= point["level"] and average >= .9 * point["level"],
        concurrency_reason=None if run.config.stream or point["level"] == 1 else "client-delivered only; server overlap not evidenced",
        first_output_p50_ms=_median(first), first_output_p95_ms=_percentile(first, .95),
        response_p50_ms=_median(response), response_p95_ms=_percentile(response, .95),
        longest_stream_pause_ms=max((r.max_stream_gap_ms for r in records if r.max_stream_gap_ms is not None), default=None),
        steady_state_seconds=steady, requests_per_second=len(steady_records) / steady if steady > 0 else None,
        output_tokens_per_second=sum(output) / steady if steady > 0 and output and all(n is not None for n in output) else None,
        throughput_warning="window shorter than responses; throughput unreliable" if response and point.get("delivered_window_seconds", 0) < 3 * median(response) / 1000 else None,
        token_lengths=token_lengths)
    if point["type"] == "interference":
        short = [r for r in records if r.load_point["role"] == "short_stream"]
        short_mean, short_peak = inflight(short, start, end)
        short_evidenced = peak_evidenced_concurrency(short, scheduling_delay_windows(run))
        short_qualified = bool(run.config.stream and short_evidenced >= point["level"] - 1 and short_mean >= .9 * (point["level"] - 1))
        groups = point.get("interference", {})
        matched = min((groups.get(name, {}).get("matched_count", 0) for name in ("control", "probe")))
        point.update(short_stream_mean_inflight=short_mean, short_stream_peak_inflight=short_peak,
            short_stream_evidenced_concurrency=short_evidenced, short_stream_load_qualified=short_qualified,
            load_qualified=short_qualified and matched >= settings.pass_criteria.min_valid_responses)
    if point["type"] == "mixed":
        durations = Counter()
        for r in records:
            durations[r.load_point["shape"]] += max(0, ((r.completed_ms or 0) - (r.dispatch_ms or 0)) / 1000)
        counts, total = Counter(r.load_point["shape"] for r in records), sum(durations.values())
        point["observed_mix"] = {s: {"requested_percent": settings.mixed.shares[s], "sent": counts[s],
            "sent_percent": counts[s] / len(records) * 100 if records else None,
            "inflight_seconds": durations[s], "inflight_percent": durations[s] / total * 100 if total else None} for s in sorted(settings.mixed.shares)}
    if point["type"] == "stress" and point["workload"] == "output":
        threshold = math.ceil(settings.profile.long.output_tokens * settings.stress.min_output_percent / 100)
        point["coverage"] = {"threshold_tokens": threshold, "covered_valid_responses": sum(
            r.completion_tokens is not None and r.completion_tokens >= threshold for r in valid), "required": point["required"]}
    quality = run_quality(run, valid, target.name, ("first_output_ms", "latency_ms"))
    point["timing_quality"] = quality
    return point


def score_point(settings, point, records):
    """Failure evidence precedes every coverage and window downgrade."""
    criteria = settings.pass_criteria
    def result(status, code, reason):
        point.update(status=status, reason_code=code, reason=reason)
        return point
    if point.get("blocked"):
        return result("blocked", "context_exceeded", point["blocked"])
    if point.get("calibration_failure"):
        return result("fail", "calibration_failed", point["calibration_failure"])
    if point["error_percent"] > criteria.max_error_percent:
        context = any(r.http_status == 400 and re.search(r"context|maximum.*length|too many tokens", r.error or "", re.I) for r in records)
        return result("fail", "context_exceeded" if context else "error_rate", "context window exceeded" if context else f"Error rate {point['error_percent']:g}% exceeds {criteria.max_error_percent:g}%")
    if any(r.completion_tokens is not None and r.requested_output_tokens is not None and r.completion_tokens > r.requested_output_tokens for r in records):
        return result("fail", "output_cap", "Endpoint generated more tokens than the requested output cap")
    wrong = point["scored"] - point["correct"]
    if (point["scored"] >= criteria.min_valid_responses and point["correct_percent"] < criteria.min_correct_percent
            or wrong > math.floor(criteria.min_valid_responses * (100 - criteria.min_correct_percent) / 100)):
        return result("fail", "correctness", f"{wrong} incorrect label {'answer' if wrong == 1 else 'answers'}; minimum correct {criteria.min_correct_percent:g}%")
    delayed = False
    for field, record_field, limit in (("first_output_p95_ms", "first_output_ms", criteria.first_output_p95_ms),
                                        ("response_p95_ms", "latency_ms", criteria.response_p95_ms)):
        value = point[field]
        if limit is not None and value is not None and value > limit:
            affected = point["timing_quality"]["affected_metrics"].get(record_field, [])
            lag = point["timing_quality"].get("max_overlapping_lag_ms", 0)
            unexplained = [r for r in records if r.valid and r.status == "completed"
                           and (getattr(r, "elapsed_ms" if record_field == "latency_ms" else record_field) or 0) > limit
                           and (r.id not in affected or lag < ((getattr(r, "elapsed_ms" if record_field == "latency_ms" else record_field) or 0) - limit) / 2)]
            if unexplained:
                label = "First answer p95" if field == "first_output_p95_ms" else "Response p95"
                return result("fail", "timing", f"{label} {value:g} exceeds {limit:g} ms")
            delayed = True
    interference = point.get("interference")
    if interference and min(interference["control"]["matched_count"], interference["probe"]["matched_count"]) >= criteria.min_valid_responses:
        if settings.interference.max_slowdown is not None and interference["slowdown"] is not None and interference["slowdown"] > settings.interference.max_slowdown:
            return result("fail", "slowdown", f"Decode slowdown {interference['slowdown']:.3g} exceeds {settings.interference.max_slowdown:g}")
    if delayed:
        return result("inconclusive", "client_delay", "Client scheduling delay explains the timing violation")
    if point.get("not_run") and point.get("input_not_calibrated"):
        return result("inconclusive", "input_not_calibrated", "input length not calibrated")
    if point.get("not_run"):
        return result("inconclusive", window_cause(point.get("window_reason")), point.get("window_reason") or "not run: shared run stopped")
    if point.get("cut_short") and not (point["valid"] >= criteria.min_valid_responses and point["load_qualified"]
            and point["delivered_window_seconds"] >= max(.25 * point["configured_window_seconds"], 3 * (point["response_p50_ms"] or 0) / 1000)):
        return result("inconclusive", window_cause(point.get("window_reason")), f"Ran {point['delivered_window_seconds']:.3g} s of {point['configured_window_seconds']:g} s: {point.get('window_reason') or 'run limit'}")
    if point["valid"] < criteria.min_valid_responses:
        return result("inconclusive", "insufficient_responses", f"{point['valid']} valid {'response' if point['valid'] == 1 else 'responses'}; {criteria.min_valid_responses} required")
    if not point["load_qualified"] and not (interference and point.get("short_stream_load_qualified")):
        return result("inconclusive", "load_not_delivered", point.get("concurrency_reason") or "Requested load not delivered or server overlap not evidenced")
    for shape, lengths in point["token_lengths"].items():
        if shape.startswith("long_") and lengths["cached_prompt_tokens"]["median"] is not None and lengths["input_tokens"]["median"] is not None and lengths["cached_prompt_tokens"]["median"] > lengths["input_tokens"]["median"] * .5:
            return result("inconclusive", "cached_input", "More than half the long input was cached")
    if point.get("input_not_calibrated"):
        return result("inconclusive", "input_not_calibrated", "input length not calibrated")
    if point["type"] == "stress" and point["workload"] == "input" and any(
            lengths["input_tokens"]["median"] is None or abs(lengths["input_tokens"]["median"] - lengths["input_target"]) > lengths["input_target"] * settings.input_token_tolerance for lengths in point["token_lengths"].values()):
        return result("inconclusive", "input_not_verified", "Large input length not verified; endpoint token counts are required")
    if point.get("coverage", {}).get("covered_valid_responses", criteria.min_valid_responses) < criteria.min_valid_responses:
        return result("inconclusive", "insufficient_coverage", "Too few responses reached the minimum output length")
    if interference:
        if interference["buffered_stream"]:
            return result("inconclusive", "buffered_stream", "stream is buffered; interruption cannot be measured")
        if min(interference["control"]["matched_count"], interference["probe"]["matched_count"]) < criteria.min_valid_responses or interference["slowdown"] is None:
            return result("inconclusive", "insufficient_matches", "Insufficient matched short streams for both probe groups")
        if settings.interference.max_slowdown is None:
            return result("inconclusive", "missing_slowdown_limit", "Slowdown measured only; no maximum slowdown configured")
    if point.get("cut_short"):
        code = window_cause(point.get("window_reason"))
        label = {"budget_limited": "Budget-limited", "time_limited": "Time-limited", "run_stopped": "Stopped"}[code]
        return result("pass", code, f"{label} window: ran {point['delivered_window_seconds']:.3g} s of {point['configured_window_seconds']:g} s")
    return result("pass", "criteria_met", "Observed evidence met the configured criteria")


def later_reservation(config):
    from giraffe.planning import request_plan
    later = [c for c in config.checks if c in {"sessions", "consistency", "tools"}]
    plan = request_plan(config.model_copy(update={"checks": later, "restart_target": None}))
    probes = 0
    if "sessions" in later and config.sessions:
        probes += sum(r["id"] == "sessions" and r["requests_per_target"] is None for r in plan["rows"])
    return plan["known_per_target"] + probes


async def execute(run, target, client):
    # Compatibility import only: all execution is unified.
    from giraffe.load_test import execute as unified
    return await unified(run, target, client)


def result(run, target):
    from giraffe.load_test import result as unified
    return unified(run, target)


def remedy_conditions(check, config, records):
    codes = {p.get("reason_code") for g in check.metrics.get("load_types", {}).values() for p in g["points"]}
    return "correctness" in codes, bool(codes & {"timing", "slowdown"})


def remedies(metrics, config):
    points = [p for g in metrics.get("load_types", {}).values() for p in g["points"]]
    codes = {p.get("reason_code") for p in points if p.get("status") != "pass"}
    guidance = []
    if "budget_limited" in codes and config.max_requests is not None:
        rates = [p["sent"] / p["delivered_window_seconds"] / p["level"] for p in points if p.get("delivered_window_seconds", 0) > 0]
        remaining = sum(p["level"] * max(0, p["configured_window_seconds"] - p.get("delivered_window_seconds", 0)) for p in points)
        suggested = config.max_requests + math.ceil(median(rates) * remaining) if rates else None
        guidance.append(f"raise Stop after this many requests to approximately {suggested:,} (observed requests/second × remaining concurrency-seconds) and allow the configured load time" if suggested else "raise Stop after this many requests (max_requests) to allow the configured load windows")
    if "time_limited" in codes and config.max_duration_seconds is not None:
        guidance.append("raise Stop test traffic after (max_duration_seconds) to allow the configured load windows")
    if "load_not_delivered" in codes:
        guidance.append("raise Limit simultaneous requests or server concurrency (OLLAMA_NUM_PARALLEL or vLLM --max-num-seqs); disable response buffering")
    if "insufficient_coverage" in codes:
        guidance.append("raise the output cap or lower Minimum output length")
    if "context_exceeded" in codes:
        shapes = [f"{load_shape_label(shape)} ({lengths['input_target']} input + {lengths['output_cap']} output tokens)" for p in points if p.get("reason_code") == "context_exceeded" for shape, lengths in p["token_lengths"].items()]
        guidance.append("lower the input/output size for " + ", ".join(dict.fromkeys(shapes)) + " or use a server with a larger context window")
    if "client_delay" in codes:
        guidance.append("reduce client load or use a less busy client, then rerun the same settings")
    if "missing_slowdown_limit" in codes:
        guidance.append("set Maximum slowdown vs short-only control")
    for code, message in (
        ("correctness", "check the model's label answers and prompt-following settings, then rerun"),
        ("timing", "inspect slow requests and server load against the configured p95 targets"),
        ("slowdown", "inspect short-stream pauses during long-input prefill and adjust server scheduling"),
        ("error_rate", "inspect the failed requests and timeouts, correct the endpoint issue and rerun"),
        ("calibration_failed", "correct the endpoint errors during token calibration and rerun"),
        ("output_cap", "configure the endpoint to enforce the requested output cap"),
        ("insufficient_responses", "allow enough valid responses to meet Minimum valid responses"),
        ("input_not_calibrated", "enable prompt token usage and adjust the input target or calibration attempts"),
        ("input_not_verified", "enable accurate prompt token usage to verify large-input coverage"),
        ("cached_input", "disable prefix caching for the long input and rerun"),
        ("buffered_stream", "disable response buffering so short-stream interruptions can be measured"),
        ("insufficient_matches", "allow more active short-stream and probe overlaps in both A/B groups")):
        if code in codes:
            guidance.append(message)
    return "; ".join(guidance)


def aggregate(statuses):
    return next((status for status in ('fail', 'blocked', 'inconclusive', 'pass') if status in statuses), 'inconclusive')
