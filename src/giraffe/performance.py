"""Version 1 of the shared performance report contract.

All durations originate at the request's monotonic timing start, except occupancy
(dispatch to completion), and explicitly arrival-relative metrics. Percentiles
use linear interpolation at (n - 1) * p. Concurrency ties close before opening;
zero-duration lifetimes occupy no time. Full windows include startup and drain.
Each headline belongs to ONE target/cohort/window; controls, warmups, calibration
and different workload shapes and roles remain selectable, never silently pooled.
Schedule levels/rates of one workload are windows of its common full cohort. Raw series
retain all observations, including failures. Rendering/decimation belongs to UI.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math
import re

from .stats import PERCENTILES, distribution as sample_distribution, percentile as percentile

PERFORMANCE_SCHEMA = 1
SPEC_DECODE_ROWS = (
    ("spec_decode_drafts", "Speculative decoding drafts", "drafts"),
    ("spec_decode_draft_tokens", "Proposed speculative tokens", "tokens"),
    ("spec_decode_accepted_tokens", "Accepted speculative tokens", "tokens"),
    ("spec_decode_proposal_length", "Proposed tokens per speculative draft", "tokens/draft"),
    ("spec_decode_acceptance_ratio", "Speculative decoding acceptance rate", "fraction"),
    ("spec_decode_sglang_acceptance_length", "SGLang reported acceptance length", "tokens/forward"),
    ("spec_decode_sglang_acceptance_ratio", "SGLang reported acceptance rate", "fraction"),
)

KPI_IDS = ("request_throughput", "output_throughput", "response_latency", "first_useful_output",
           "tpot", "generation_pace", "client_ttft", "itl", "first_reasoning", "server_ttft")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def metric(value=None, *, label, unit, source, target, window, state=None, reason=None,
           action=None, sample_count=0, eligible_count=0, **details):
    """One envelope for scalar, structured and distribution-valued metrics."""
    if isinstance(value, float) and not math.isfinite(value):
        value = None
    state = state or ("measured" if value is not None else "not_measured")
    if value is None and state in {"measured", "estimated"}:
        state = "insufficient_evidence"
    return dict(value=value, label=label, unit=unit, source=source, state=state,
                reason=reason or (None if value is not None else "No eligible observations in this scope."),
                action=action or (None if value is not None else {
                    "no_baseline": "Attach a baseline with comparable workload and measurement coverage.",
                    "not_applicable": "Select the corresponding workload or transport to collect this measure.",
                    "not_collected": "Enable the corresponding collector and rerun the workload.",
                }.get(state, "Collect eligible observations for this scope.")),
                sample_count=sample_count, eligible_count=eligible_count,
                counts={"sample": sample_count, "eligible": eligible_count,
                        "missing": max(0, eligible_count - sample_count)},
                window=window, target=target, **details)


def distribution(values, **kwargs):
    result = sample_distribution(values)
    return metric(result if result["n"] else None, sample_count=result["n"],
                  percentile_convention=PERCENTILES, **kwargs)


def concurrency_series(records, start_ms, end_ms):
    """Clipped lifetimes; includes requests in flight at the window boundary."""
    events = []
    occupancy_ms = 0.0
    known = 0
    for record in records:
        if record.dispatch_ms is None:
            continue
        finish = record.completed_ms if record.completed_ms is not None else end_ms
        left, right = max(start_ms, record.dispatch_ms), min(end_ms, finish)
        if right <= left:
            continue
        known += 1
        occupancy_ms += right - left
        events.extend(((left, 1), (right, -1)))
    events.sort(key=lambda e: (e[0], e[1]))  # completion before dispatch at a tie
    count = peak = 0
    points = [{"elapsed_s": 0.0, "in_flight": 0}]
    for when, delta in events:
        count += delta
        peak = max(peak, count)
        points.append({"elapsed_s": (when - start_ms) / 1000, "in_flight": count})
    points.append({"elapsed_s": max(0, end_ms - start_ms) / 1000, "in_flight": count})
    duration = end_ms - start_ms
    return {"points": points, "peak": peak if duration > 0 else None,
            "average": occupancy_ms / duration if duration > 0 else None,
            "sample_count": known,
            "tie_rule": "Completion before dispatch at equal timestamps; zero-duration lifetimes excluded."}


def completion_buckets(records, start_ms, end_ms):
    """Counts, not rates; half-open buckets except the final inclusive endpoint."""
    seconds = max(0, end_ms - start_ms) / 1000
    widths = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)
    width = next((w for w in widths if seconds / w <= 20), max(3600, math.ceil(seconds / 20 / 3600) * 3600))
    buckets = []
    for i in range(math.ceil(seconds / width)):
        left, right = i * width, min(seconds, (i + 1) * width)
        buckets.append(dict(start_s=left, end_s=right, duration_s=right-left,
                            count=0, valid_completions=0, partial=right-left < width))
    for record in records:
        if record.completed_ms is None or not start_ms <= record.completed_ms <= end_ms or not buckets:
            continue
        index = min(len(buckets) - 1, int((record.completed_ms-start_ms) / 1000 / width))
        buckets[index]["count"] += 1
        buckets[index]["valid_completions"] += int(record.status == "completed" and record.valid)
    return {"unit": "requests", "width_s": width, "buckets": buckets,
            "rule": "Aim for 10–20 buckets; choose first 1/2/5/10/15/30/60/120/300/600/900/1800/3600-second width giving at most 20 (whole hours thereafter).",
            "boundary_rule": "Half-open [start,end), final endpoint inclusive; every terminal request assigned once.",
            "final_partial_bucket": bool(buckets and buckets[-1]["partial"]),
            "partial_bucket_disclosure": "Final bucket uses its actual duration; counts are not requests/s."}


_PERFORMANCE_CHECKS = {"load", "concurrency", "capacity", "arrivals", "sessions", "buckets"}


def _cohort(record):
    """Comparable workload identity; schedule level/rate is a window, not a cohort.

    Explicit source, shape, phase and role remain part of identity. Actual token
    counts vary within a workload and are not its requested shape.
    """
    from .conditions import condition_for

    point = record.load_point or {}
    identity = str(point.get("point_id") or record.condition_id
                   or condition_for(record.scenario, record.workload)["id"])
    if _preparation(record):
        return identity
    if point or _PERFORMANCE_CHECKS.intersection(record.check_ids):
        if record.traffic_stage is not None and re.fullmatch(r"traffic(?:_stage_|-)\d+", identity):
            identity = "traffic"
        identity = re.sub(r"_stage_\d+$", "", identity)
        identity = re.sub(r"^bucket_c\d+_(?=(?:short|medium|long)_(?:short|medium|long)$)", "bucket_", identity)
        identity = re.sub(r"_c\d+(?=\.|$)", "", identity)
        identity = re.sub(r"_r[0-9.e+-]+(?=\.|$)", "", identity)
        # Concurrent, fixed-rate, Poisson and burst delivery alter the schedule,
        # not the workload. Strip only the executor's known suffix grammar.
        if identity.startswith("load_"):
            identity = re.sub(r"_(?:concurrent|constant|poisson|bursts)(?:_bursts)?(?:_only)?$", "", identity)
        parts = [identity]
        parts.extend(f"{key}={point[key]}" for key in ("source", "type", "workload", "shape", "phase", "role")
                     if point.get(key) is not None and not (key == "role" and point[key] == "request"))
        parts.append("check=" + ",".join(sorted(record.check_ids)))
        parts.extend(f"{key}={record.workload[key]}" for key in ("source", "request_source", "input_shape", "output_shape", "role", "phase")
                     if record.workload.get(key) is not None)
        return ":".join(parts)
    return identity


def _cohort_label(group, observation):
    from .conditions import condition_for
    from .models import CHECK_NAMES

    first = group[0]
    point = first.load_point or {}
    check = next((name for name in first.check_ids if name in _PERFORMANCE_CHECKS), first.check_ids[0] if first.check_ids else "serving")
    check_label = CHECK_NAMES.get(check, check.replace("_", " ").capitalize())
    if point:
        shape = point.get("shape") or point.get("workload")
        name = {
            "mixed": "Mixed workload", "interference": "Interference workload", "input": "Large-input stress", "output": "Large-output stress"
        }.get(shape, check_label)
        # The compact arrow label matches the request-shape selector.
        if shape and re.fullmatch(r"(short|medium|long)_(short|medium|long)", shape):
            name = " → ".join(part.title() for part in shape.split("_"))
        label = f"{check_label} · {name}" if name != check_label else check_label
        source = {"custom_fixtures": "Custom questions", "request_pack": "Request pack"}.get(first.workload.get("request_source"))
        if source:
            label += " · " + source
        role = (point.get("phase") or first.workload.get("phase") or first.workload.get("role")
                or (point.get("role") if point.get("role") != "request" else None))
        if role:
            label += " · " + str(role).replace("_", " ").capitalize()
        return check, label
    if first.traffic_stage is not None or first.scenario.startswith("traffic_stage_"):
        return check, check_label
    saved = next((row.get("condition", {}).get("label") for row in observation.get("conditions", [])
                  if row.get("condition", {}).get("id") == first.condition_id), None)
    label = saved or condition_for(first.scenario, first.workload)["label"]
    label = re.sub(r"(?: · | at )\d+(?: concurrent requests?| at once| per second).*?$", "", label)
    if re.fullmatch(r"(?:synthetic|traffic)_stage_\d+", first.scenario):
        label = saved.split(" · ")[0] if saved else "Comparable request workload"
    return check, f"{check_label} · {label}" if check in _PERFORMANCE_CHECKS and label != check_label else label


def _identities(records):
    return {str(value) for row in records for value in
            (row.scenario, row.condition_id, (row.load_point or {}).get("point_id")) if value}


def _cohort_schedule(group, all_records, observation):
    """Only this workload's sending stages and their recorded drain intervals.

    Unknown legacy stage IDs may be associated by dispatch time, never merely
    by a long-lived request overlapping somebody else's sending window.
    """
    own_ids, all_ids = _identities(group), _identities(all_records)
    checks = {name for row in group for name in row.check_ids}
    indices = []
    previous_owned = False
    observed_end = _observed_interval(group)[1]
    for index, stage in enumerate(observation.get("schedule_transitions", [])):
        left = stage.get("start_ms")
        right = stage.get("end_ms") if stage.get("end_ms") is not None else observed_end
        if not _number(left) or not _number(right) or right <= left:
            previous_owned = False
            continue
        identifier = str(stage.get("stage_id", ""))
        base = identifier.removesuffix(".drain")
        compatible = not stage.get("check_id") or stage["check_id"] in checks
        if stage.get("kind") == "drain":
            owned = compatible and (base in own_ids or previous_owned)
        else:
            owned = compatible and (identifier in own_ids or
                (identifier not in all_ids and any(row.dispatch_ms is not None and left <= row.dispatch_ms < right for row in group)))
        if owned:
            indices.append(index)
        previous_owned = owned
    return indices


def _stage_label(stage):
    if stage.get("kind") == "drain":
        return "Drain"
    if stage.get("intended_concurrency") is not None:
        return f"C{stage['intended_concurrency']:g}"
    if stage.get("intended_rate_rps") is not None:
        return f"{stage['intended_rate_rps']:g} requests/s"
    return str(stage.get("pattern") or "Recorded stage").replace("_", " ").capitalize()


def _window_labels(window, full_start):
    left, right = window["start_ms"], window["end_ms"]
    bounds = (f"{(left-full_start)/1000:,.2f}–{(right-full_start)/1000:,.2f} s" if left is not None and right is not None and full_start is not None
              else "timing not measured")
    population = f"{window['request_count']:,} requests"
    scope = "full run" if window["kind"] == "full" else ("Drain" if window["includes_drain"] else f"Stage {window['label']}")
    window["selection_label"] = f"{window['workload_label']} · {scope} ({bounds}, {population})"
    window["description"] = " · ".join(filter(None, (window["label"],
        "includes first-request startup" if window["includes_startup"] else None,
        f"{window['duration_s']:,.2f} s" if window["duration_s"] is not None else "timing not measured",
        population, window.get("stages_description"))))


def _preparation(record):
    return (record.scenario in {"warm", "initial", "readiness", "connection", "recovery_prime"}
            or "calibration" in record.scenario or not record.check_ids)


def _observed_interval(records):
    starts = [r.dispatch_ms for r in records if r.dispatch_ms is not None]
    ends = [r.completed_ms for r in records if r.completed_ms is not None]
    # No wall-clock or elapsed-time substitution for absent monotonic lifetimes.
    return (min(starts), max(ends)) if starts and ends else (None, None)


def _request_series(records):
    rows = []
    for number, record in enumerate(records, 1):
        modern = getattr(record, "timing_schema", 0) >= 1
        generation_pace = record.generation_tokens_per_second
        stream_duration = (record.last_output_ms - record.first_output_ms
                           if record.stream and record.first_output_ms is not None and record.last_output_ms is not None
                           and record.last_output_ms >= record.first_output_ms else None)
        finish_relative = (record.completed_ms - record.timing_start_ms
                           if record.completed_ms is not None and record.timing_start_ms is not None else record.elapsed_ms)
        tail = (finish_relative - record.last_output_ms
                if record.last_output_ms is not None and finish_relative >= record.last_output_ms else None)
        before = (record.timing_start_ms + record.first_output_ms - record.dispatch_ms
                  if record.first_output_ms is not None and record.timing_start_ms is not None and record.dispatch_ms is not None else None)
        rows.append(dict(number=number, request_id=record.id, cohort_id=_cohort(record),
            status=record.status, valid=record.valid, dispatch_ms=record.dispatch_ms,
            completed_ms=record.completed_ms, timing_start_ms=record.timing_start_ms,
            values={"response_latency_ms": record.elapsed_ms if record.elapsed_ms > 0 else None,
                    "first_useful_output_ms": record.first_output_ms,
                    "before_first_useful_output_ms": before,
                    "answer_streaming_ms": stream_duration, "completion_tail_ms": tail,
                    "client_ttft_ms": getattr(record, "first_token_ms", None) if modern else None,
                    "first_reasoning_ms": record.first_reasoning_ms,
                    "input_tokens": record.input_tokens, "answer_tokens": record.output_tokens,
                    "completion_tokens": record.completion_tokens,
                    "cached_prompt_tokens": record.cached_prompt_tokens,
                    "reasoning_tokens": record.reasoning_tokens,
                    "tpot_estimate_ms": 1000 / generation_pace if generation_pace else None,
                    "tpot_direct_ms": getattr(record, "tpot_direct_ms", None) if modern else None}))
    return rows


def _window_summary(report, target, window, records, series, observation):
    from .runner import _stats
    from .timing import qualify, threshold

    wid = window["id"]
    start, end = window["start_ms"], window["end_ms"]
    duration = (end-start) / 1000 if start is not None and end is not None and end > start else None
    # Intersecting lifetimes determine occupancy; completion-window membership
    # determines throughput and terminal outcomes. Preserve carry-in and drain.
    finished = [r for r in records if r.completed_ms is not None and start is not None and end is not None
                and start <= r.completed_ms <= end and (window.get("end_inclusive") or window["kind"] == "full" or r.completed_ms < end)]
    population = records if window["kind"] == "full" else finished
    good = [r for r in population if r.status == "completed" and r.valid]
    rate_good = [r for r in finished if r.status == "completed" and r.valid]
    n = len(population)
    base = dict(target=target, window=wid, eligible_count=n)
    metrics, distributions = {}, {}

    def put(key, value=None, label=None, unit="", source="Request records", samples=None, eligible=None, **extra):
        row = metric(value, label=label or key.replace('_', ' ').capitalize(), unit=unit,
                     source=source, **{**base, "eligible_count": n if eligible is None else eligible,
                     "sample_count": n if samples is None and value is not None else (samples or 0)}, **extra)
        row["id"] = key
        metrics[key] = row
        return row

    def dist(key, values, label, unit="ms", source="Client receipt timestamps; request timing origin", state=None, reason=None, action=None):
        row = distribution(values, label=label, unit=unit, source=source, state=state,
                           reason=reason, action=action, **base)
        row["id"] = key
        distributions[key] = row
        headline = row["value"]["median"] if row["value"] else None
        put(key, headline, label, unit, source, samples=row["sample_count"], state=row["state"],
            reason=row["reason"], action=row["action"], distribution=key,
            p95=row["value"]["p95"] if row["value"] else None)
        return row

    ids = {r.id for r in population}
    selected_series = [row for row in series if row["request_id"] in ids]
    valid_series = [row for row in selected_series if row["valid"] and row["status"] == "completed"]
    for key, field, label in (
        ("response_latency", "response_latency_ms", "Response latency"),
        ("first_useful_output", "first_useful_output_ms", "First useful output"),
        ("answer_streaming", "answer_streaming_ms", "Answer streaming duration"),
        ("completion_tail", "completion_tail_ms", "Completion time (tail)"),
        ("client_ttft", "client_ttft_ms", "TTFT (client)"),
        ("first_reasoning", "first_reasoning_ms", "First reasoning latency")):
        observed_series = selected_series if key in {"client_ttft", "first_reasoning", "first_useful_output"} else valid_series
        dist(key, [row["values"][field] for row in observed_series], label,
             reason=("Versioned streamed generated-content receipt timing was not collected." if key == "client_ttft" else None))
    modern = [r for r in good if getattr(r, "timing_schema", 0) >= 1]
    event_records = [r for r in population if getattr(r, "timing_schema", 0) >= 1]
    gaps = [gap for r in event_records for gap in (getattr(r, "read_gaps_ms", None) or [])]
    reasoning_gaps = [gap for r in event_records for gap in (getattr(r, "reasoning_read_gaps_ms", None) or [])]
    dist("answer_read_gap", gaps, "Answer read gaps", source="Gaps between separate answer network reads; not token ITL")
    dist("reasoning_read_gap", reasoning_gaps, "Reasoning read gaps", source="Gaps between separate reasoning network reads; not token ITL")
    maximum_gaps = gaps + [r.max_stream_gap_ms for r in good if r.max_stream_gap_ms is not None]
    put("longest_stream_pause", max(maximum_gaps, default=None), "Longest stream pause", "ms",
        "Maximum gap between answer network reads; not token ITL", samples=len(maximum_gaps))
    # The saved generation estimate predates versioned token-event evidence.
    # Its existing property enforces valid, completed, unbuffered streaming
    # evidence; only direct/token-event metrics require the newer schema.
    generation_rates = [r.generation_tokens_per_second for r in good]
    legacy_rates = [r.generation_tokens_per_second for r in good if getattr(r, "timing_schema", 0) < 1]
    legacy_source = "; from saved generation-rate estimate; older run" if any(rate is not None for rate in legacy_rates) else ""
    estimates = [1000 / rate if rate else None for rate in generation_rates]
    direct = [getattr(r, "tpot_direct_ms", None) for r in modern]
    dist("tpot_estimate", estimates, "TPOT estimate", "ms/token", "1000 / generation_tokens_per_second" + legacy_source, state="estimated")
    dist("tpot_direct", direct, "Direct TPOT", "ms/token", "(last answer receipt − first answer receipt) / (answer tokens − 1); proven first event token boundary")
    selected_tpot = metrics["tpot_direct"] if metrics["tpot_direct"]["value"] is not None else metrics["tpot_estimate"]
    metrics["tpot"] = {**selected_tpot, "id": "tpot", "label": "Time per output token (TPOT)"}
    metrics["tpot_direct"]["exclusion_reasons"] = dict(Counter(r.tpot_direct_reason for r in good if r.tpot_direct_reason))
    dist("generation_pace", generation_rates, "Generation pace", "tok/s",
         "Answer streaming estimate; first-read tokens estimated by character share" + legacy_source, state="estimated")
    itl = [gap for r in modern for gap in (getattr(r, "itl_ms", None) or [])]
    evidence_states = Counter(r.workload.get("token_evidence", {}).get("logprobs", "not_recorded") for r in good)
    if good and not any(r.stream for r in good):
        itl_state, itl_reason = "not_applicable", "Non-streamed replies provide no individual token arrival timestamps."
    elif evidence_states.get("supported"):
        itl_state, itl_reason = "insufficient_evidence", "Streamed logprobs were requested automatically, but these responses lack separate single-token reads with matching answer usage."
    elif evidence_states.get("rejected"):
        itl_state, itl_reason = "not_supported", "The endpoint rejected logprobs during the compatibility probe; chunk receipt gaps cannot establish individual token arrival times."
    elif evidence_states.get("not_returned"):
        itl_state, itl_reason = "insufficient_evidence", "The endpoint accepted the compatibility probe but did not return matching streamed logprob token boundaries."
    elif evidence_states.get("shape_unverified"):
        itl_state, itl_reason = "not_collected", "Plain-text probe did not establish logprobs compatibility with this tool or structured-output request shape; automatic fields were not added."
    elif evidence_states.get("disabled_by_request"):
        itl_state, itl_reason = "not_collected", "Explicit request options disabled logprobs; automatic collection preserves those options."
    else:
        itl_state, itl_reason = "not_collected", "No successful streamed token-evidence compatibility probe was recorded for these requests."
    dist("itl", itl, "Inter-token latency (ITL)", "ms/token", "Proven single-token events in separate reads; usage reconciled",
         state="measured" if itl else itl_state, reason=None if itl else itl_reason,
         action=None if itl or evidence_states.get("supported") or evidence_states.get("rejected") else
                "A new run probes token-evidence compatibility automatically; exact ITL still requires unbuffered single-token events and matching usage.")
    metrics["itl"]["capability_evidence"] = dict(evidence_states)
    if itl or itl_state == "not_applicable" or any(evidence_states.get(state) for state in ("supported", "rejected", "shape_unverified", "disabled_by_request")):
        metrics["itl"]["action"] = distributions["itl"]["action"] = None
    metrics["itl"]["exclusion_reasons"] = dict(Counter(getattr(r, "itl_reason", "Missing timing evidence") for r in good if getattr(r, "itl_reason", None)))
    for key, prop in (("answer_read_gap", "read_gaps_ms"), ("reasoning_read_gap", "reasoning_read_gaps_ms"), ("itl", "itl_ms")):
        eligible_records = modern if key == "itl" else event_records
        eligible_count = len(good) if key == "itl" else n
        covered = sum(bool(getattr(r, prop, None)) for r in eligible_records)
        for row in (metrics[key], distributions[key]):
            row["request_coverage"] = {"sample": covered, "eligible": eligible_count, "missing": eligible_count-covered}
            row["sample_unit"] = "gaps"
    if not event_records:
        for key in ("tpot_direct", "client_ttft", "answer_read_gap", "reasoning_read_gap"):
            metrics[key].update(value=None, state="not_collected", reason="No versioned timing evidence; old records remain unavailable.", action="Rerun with timing schema 1 instrumentation.")
    stats = _stats(population)
    for key, label in (("attempted", "Requests started"), ("completed", "Valid completions"),
                       ("failed", "Failed / invalid"), ("timed_out", "Timeouts"),
                       ("cancelled", "Cancelled"), ("reasoning_budget_exhausted", "Reasoning budget exhausted")):
        put(key, stats[key], label, "requests")
    put("error_rate", stats["error_rate"], "Error rate", "ratio", "Existing outcome accounting; failures + timeouts / non-cancelled, non-reasoning-budget requests",
        samples=n-stats["cancelled"]-stats["reasoning_budget_exhausted"], eligible=n-stats["cancelled"]-stats["reasoning_budget_exhausted"])
    scored = [r for r in population if r.score is not None]
    put("scored", len(scored), "Scored", "requests")
    put("unscored", n-len(scored), "Unscored", "requests")
    put("correctness", sum(r.score is True for r in scored)/len(scored) if scored else None,
        "Answer correctness", "ratio", "Correct / scored; unscored answers are not correct", samples=len(scored))
    for key, label, predicate in (
        ("partial_failed", "Partial failed responses", lambda r: r.status != "completed" and (r.first_output_ms is not None or r.first_reasoning_ms is not None)),
        ("length_capped", "Length-capped replies", lambda r: r.finish_reason == "length"),
        ("normal_finish", "Normally stopped replies", lambda r: r.finish_reason == "stop")):
        put(key, sum(map(predicate, population)), label, "requests")
    for key, label, check in (("structured_correctness", "Structured-output correctness", "json"), ("tool_correctness", "Tool-call correctness", "tools")):
        values = [r for r in scored if check in r.check_ids]
        put(key, sum(r.score is True for r in values)/len(values) if values else None,
            label, "ratio", "Existing request scoring", samples=len(values),
            state="measured" if values else "not_applicable", reason=None if values else "No scored requests for this check in the selected scope.")
    judgements = [r.load_point.get("response_criteria") for r in population if r.load_point and r.load_point.get("response_criteria")]
    put("latency_attainment", judgements or None, "Latency-limit attainment", "responses",
        "Existing per-response load criteria; no new acceptance decision", samples=len(judgements))
    put("request_throughput", len([r for r in finished if r.status == "completed" and r.valid])/duration if duration else None,
        "Request throughput", "req/s", "Valid completions with observed completion timestamps in window / window seconds", samples=len(finished),
        partial=window["kind"] == "full" and len(finished) < n)

    # Usage fields can overlap: completion includes reasoning, cached is part of
    # input. Totals are known subsets if coverage is partial, never zero-filled.
    totals = {}
    for key, field, label in (("input", "input_tokens", "Prompt tokens received"), ("answer", "output_tokens", "Answer tokens"),
            ("completion", "completion_tokens", "Completion tokens"), ("cached", "cached_prompt_tokens", "Cached prompt tokens"),
            ("reasoning", "reasoning_tokens", "Reasoning tokens")):
        values = [getattr(r, field) for r in good if getattr(r, field) is not None]
        totals[key] = sum(values) if values else None
        put(key+"_tokens", totals[key], label, "tokens", "Endpoint-reported usage on valid completions; known subset when coverage is partial",
            samples=len(values), partial=len(values) < len(good), eligible=len(good))
        dist(key+"_length", values, label+" distribution", "tokens", "Endpoint-reported usage on valid completions")
        if key in {"input", "answer", "completion"}:
            rate_values = [getattr(r, field) for r in rate_good if getattr(r, field) is not None]
            put({"answer": "output", "input": "input", "completion": "completion"}[key]+"_throughput",
                sum(rate_values)/duration if rate_values and duration else None,
                {"answer": "Output token throughput", "input": "Input throughput", "completion": "Completion throughput"}[key],
                "tok/s", "Known valid-completion usage with observed completion timestamps / matching window seconds; partial totals disclosed", samples=len(rate_values), partial=len(rate_values) < len(good))
    paired = [r.input_tokens + r.completion_tokens for r in good if r.input_tokens is not None and r.completion_tokens is not None]
    total = sum(paired) if paired else None
    put("total_tokens", total, "Total input + completion", "tokens", "Input + completion once; cached subset and answer tokens not added again", samples=len(paired), partial=len(paired) < len(good))
    rate_pairs = [r.input_tokens+r.completion_tokens for r in rate_good if r.input_tokens is not None and r.completion_tokens is not None]
    put("total_throughput", sum(rate_pairs)/duration if rate_pairs and duration else None, "Total throughput (input + completion)", "tok/s",
        "Input + completion usage on matching observed completions / window seconds", samples=len(rate_pairs), partial=len(rate_pairs) < len(good))
    cache_pairs = [r for r in good if r.cached_prompt_tokens is not None and r.input_tokens is not None and 0 <= r.cached_prompt_tokens <= r.input_tokens]
    cache_den = sum(r.input_tokens for r in cache_pairs)
    put("cached_share", sum(r.cached_prompt_tokens for r in cache_pairs)/cache_den if cache_den else None,
        "Cached input share", "ratio", "Cached / input from matching usage population; not a server hit rate or latency saving", samples=len(cache_pairs))
    put("missing_usage", {key: sum(getattr(r, field) is None for r in population) for key, field in
        (("input", "input_tokens"), ("answer", "output_tokens"), ("completion", "completion_tokens"), ("reasoning", "reasoning_tokens"), ("cached", "cached_prompt_tokens"))},
        "Missing usage", "requests", "Unknown token counts are not zero")
    identical = bool(good) and all(r.completion_tokens is not None and r.output_tokens == r.completion_tokens
                                    and r.reasoning_tokens == 0 for r in good)
    metrics["completion_throughput"]["semantically_identical_to_answer"] = identical
    dist("output_rate", [r.output_tokens*1000/r.elapsed_ms for r in good if r.output_tokens is not None and r.elapsed_ms > 0],
         "Output rate per request", "tok/s", "Answer tokens / whole request latency; distinct from streaming generation pace")
    occupancy = concurrency_series(records, start, end) if start is not None and end is not None else dict(points=[], peak=None, average=None, sample_count=0)
    for key, label in (("peak", "Peak concurrency"), ("average", "Average concurrency")):
        put(key+"_concurrency", occupancy[key], label, "requests", "Request lifetimes clipped to window; time weighted average", samples=occupancy["sample_count"])
    config = report.manifest.get("config", {})
    put("configured_ceiling", config.get("concurrency"), "Configured ceiling", "requests", "Configuration; never used as intended concurrency",
        reason=None if config.get("concurrency") is not None else "Not set.", action=None if config.get("concurrency") is not None else "Optionally set Simultaneous limit.")
    for prefix in ("output", "total"):
        rate = metrics[prefix+"_throughput"]["value"]
        put(prefix+"_per_in_flight", rate/occupancy["average"] if rate is not None and occupancy["average"] else None,
            prefix.capitalize()+" throughput per in-flight request", "tok/s/request", "Window token throughput / time-weighted average concurrency; not individual generation speed")
    health = report.observations.get("client_timing_health", {})
    quality = qualify(population, health, threshold(population), legacy_max_lag_ms=report.observations.get("generator_max_lag_ms", 0))
    put("timing_quality", quality, "Latency samples affected", "requests", "Existing overlapping client timing qualification")
    pauses = [{**w, "elapsed_s": (w["end_ms"]-start)/1000} for w in health.get("windows", [])
              if start is not None and w.get("end_ms", -1) >= start and w.get("start_ms", math.inf) <= end]
    put("max_event_loop_delay", max((w["lag_ms"] for w in pauses), default=None), "Maximum event-loop delay", "ms", "Client scheduling health; cause not established", samples=len(pauses),
        state="measured" if pauses else "not_observed", reason=None if pauses else "No delays above recording threshold in this window.")
    put("delayed_intervals", len(pauses) if health else None, "Delayed sampling intervals", "intervals", "Client scheduling health")
    dispatched = [r for r in records if start is not None and r.dispatch_ms is not None and start <= r.dispatch_ms <= end
                  and (window["kind"] == "full" or r.dispatch_ms < end)]
    put("dispatch_rate", len(dispatched)/duration if duration else None, "Observed dispatch rate", "req/s", "Dispatches within selected window / window seconds", samples=len(dispatched))
    for key, field, label in (("client_preparation", "client_preparation_ms", "Client preparation"), ("client_wait", "client_wait_ms", "Client wait"),
                             ("dispatch_delay", "dispatch_lag_ms", "Dispatch delay"), ("arrival_first_output", "arrival_first_output_ms", "Arrival-to-output latency"),
                             ("arrival_completion", "arrival_elapsed_ms", "Arrival-to-completion latency")):
        dist(key, [getattr(r, field) for r in population], label, source="Recorded client scheduler/request timestamps; not server queue time")
    transitions = [row for index, row in enumerate(observation.get("schedule_transitions", []))
                   if index in window.get("schedule_indices", []) and start is not None
                   and row.get("start_ms", math.inf) < end and (row.get("end_ms") if row.get("end_ms") is not None else end) > start]
    intended = [{**row, "elapsed_s": (max(start, row["start_ms"])-start)/1000,
                 "end_elapsed_s": (min(end, row["end_ms"] if row.get("end_ms") is not None else end)-start)/1000} for row in transitions]
    put("sampled_concurrency", list(dict.fromkeys(row["intended_concurrency"] for row in transitions if row.get("intended_concurrency") is not None and row.get("kind") != "drain")) or None,
        "Sampled concurrency", "requests", "Recorded schedule transitions only")
    rates = list(dict.fromkeys(row["intended_rate_rps"] for row in transitions if row.get("intended_rate_rps") is not None and row.get("kind") != "drain"))
    put("offered_rate", rates or None, "Offered / scheduled arrival rate", "req/s", "Recorded arrival scheduler transitions; distinct from intended concurrency", state="measured" if rates else "not_applicable")
    _bind_existing(report, target, window, population, observation, put)
    _server_rows(report, target, window, observation, put, metrics)
    _baseline_rows(report, target, window, population, put)
    sections = {
        "delivery": ("sampled_concurrency", "dispatch_rate", "max_event_loop_delay", "delayed_intervals", "timing_quality", "client_preparation", "client_wait", "offered_rate", "dispatch_delay", "arrival_first_output", "arrival_completion", "drops", "server_running", "server_queue"),
        "outcomes": ("attempted", "completed", "failed", "timed_out", "cancelled", "error_rate", "reasoning_budget_exhausted", "partial_failed", "length_capped", "normal_finish", "scored", "unscored", "correctness", "structured_correctness", "tool_correctness", "latency_attainment"),
        "token_accounting": ("input_tokens", "answer_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cached_share", "reasoning_tokens", "missing_usage"),
        "throughput_capacity": ("input_throughput", "completion_throughput", "total_throughput", "output_rate", "output_per_in_flight", "total_per_in_flight", "goodput", "knee", "highest_acceptable_rate"),
        "conversations": ("turn_latency", "turn_generation", "conversation_completion", "user_fairness", "workload_occupancy", "decode_interference", "response_interference", "recovery_ratios", "cancel_followup"),
        "server": ("gpu_utilization", "gpu_memory", "gpu_temperature", "gpu_errors", "throttling", "server_running", "server_waiting", "kv_cache", "prefix_cache", "prefix_hit_rate", "preemptions", "server_retractions_per_second", "server_queue", "server_inference", "server_prompt_tokens_per_second", "server_tokens", "host_cpu", "host_ram", "websocket_rtt")
                  + tuple(key for key, _, _ in SPEC_DECODE_ROWS)
                  + (("spec_decode_accepted_by_position",) if "spec_decode_accepted_by_position" in metrics else ()),
        "baseline": ("baseline_throughput", "baseline_timing", "baseline_generation", "baseline_quality", "baseline_capacity"),
    }
    return {"metrics": metrics, "kpis": list(KPI_IDS), "distributions": distributions,
            "sections": {name: [metrics[key] for key in keys] for name, keys in sections.items()},
            "request_row_indices": [row["number"] - 1 for row in selected_series],
            "window": window,
            "series_units": {"timing": "ms", "tokens": "tokens"},
            "series_definitions": {"response_latency_ms": "Client request timing start to completion.",
                "before_first_useful_output_ms": "Dispatch to first useful output; includes offset before client timing start. Missing origin remains unavailable.",
                "first_useful_output_ms": "Client request timing start to first useful answer/tool output.",
                "answer_streaming_ms": "First to last answer receipt; not continuous server generation timing.",
                "completion_tail_ms": "Last answer receipt to recorded request completion, with matching timing origin; client elapsed fallback when absolute origins absent.",
                "client_ttft_ms": "Client request timing start to first generated-content event. Distinct from first useful output."},
            "completion_buckets": completion_buckets(population, start, end) if start is not None and end is not None else None,
            "actual_concurrency": occupancy,
            "intended_concurrency": [row for row in intended if row.get("intended_concurrency") is not None],
            "intended_arrival_rate": [row for row in intended if row.get("intended_rate_rps") is not None],
            "schedule_intervals": intended,
            "intended_state": "measured" if any(row.get("intended_concurrency") is not None for row in intended) else "not_collected",
            "intended_reason": None if any(row.get("intended_concurrency") is not None for row in intended) else "No recorded intended-concurrency transitions; configured ceiling is not intended history.",
            "client_pauses": pauses, "client_pause_threshold_ms": health.get("delay_floor_ms"),
            "event_evidence": {"timing_schema": 1, "eligible_requests": n, "versioned_requests": len(event_records),
                "truncated_requests": sum(bool(getattr(r, "output_events_truncated", False) or getattr(r, "read_timestamps_truncated", False)) for r in event_records)},
            "timing_quality": quality}


def _bind_existing(report, target, window, records, observation, put):
    """Expose existing qualification, never infer capacity from observed rates."""
    checks = [c for c in report.checks if c.target == target]
    by_id = {c.id: c.metrics for c in checks}
    ids = {r.id for r in records}
    cohort = window["cohort_id"]
    load = by_id.get("load", observation.get("load_test", {}))
    point_ids = {(row.load_point or {}).get("point_id") for row in records}
    points = [p for p in load.get("points", []) if str(p.get("id")) in point_ids]
    stage_ids = {row.traffic_stage for row in records if row.traffic_stage is not None}
    stage_entries = [(s.get("stage_index", index), s) for index, s in enumerate(
        by_id.get("capacity", {}).get("traffic_stages", observation.get("capacity", [])))
        if isinstance(s, dict) and s.get("stage_index", index) in stage_ids]
    stages = [stage for _, stage in stage_entries]
    if points:
        delivery = [{key: p.get(key) for key in ("id", "scheduled", "dispatched", "not_sent", "local_drops", "late_drops", "not_offered", "window_reason", "reason", "reason_code")}
                    for p in points]
    elif stages:
        delivery = [{key: s.get(key) for key in ("stage_index", "scheduled", "started", "dispatched", "dropped_local", "dropped_late", "local_drops", "late_drops", "not_offered", "reasons")}
                    for s in stages]
    else:
        delivery = None
    put("drops", delivery, "Local drops / late drops / not offered", "requests", "Existing scoped scheduler ledger; unsent work is not an endpoint error",
        samples=len(points)+len(stages), state="measured" if delivery else "not_applicable")
    accepted = [p for p in points if p.get("status") == "pass"]
    rate_findings = [p.get("rate") for p in accepted if p.get("rate") is not None and p.get("pattern") in {"constant", "poisson"} and not p.get("bursts")]
    rate_findings += [s.get("rate_rps") for s in stages if s.get("accepted") and s.get("rate_rps") is not None]
    put("highest_acceptable_rate", max(rate_findings, default=None), "Highest acceptable arrival rate", "req/s",
        "Existing qualified acceptance at tested rates for this cohort; not maximum deployment capacity",
        samples=len(rate_findings), state="measured" if rate_findings else "insufficient_evidence",
        reason=None if rate_findings else "No accepted arrival-rate evidence for this cohort.")
    goodput, legacy_coverage = [], []
    missing_goodput = set()
    capacity_config = (report.manifest.get("effective_checks", {}).get("capacity")
                       or report.manifest.get("config", {}))
    capacity_limits = capacity_config.get("limits", {})
    limited = any(capacity_limits.get(key) is not None for key in ("first_output_ms", "latency_ms"))
    valid_counts, scored_counts = Counter(), Counter()
    if stage_entries:
        for record in records:
            if record.valid and record.status == "completed":
                valid_counts[record.traffic_stage] += 1
                scored_counts[record.traffic_stage] += record.score is not None
    for stage_index, stage in stage_entries:
        stage_valid, stage_scored = valid_counts[stage_index], scored_counts[stage_index]
        legacy_coverage.append({"scored": stage_scored, "valid": stage_valid, "unscored": stage_valid-stage_scored})
        if not limited:
            missing_goodput.add("No first-answer or completion latency limit is established.")
        if not stage_scored:
            missing_goodput.add("Answers are unscored; answer correctness is unknown.")
        if stage.get("goodput_rps") is not None and not stage.get("generator_limited") and limited and stage_scored:
            goodput.append(stage["goodput_rps"])
        elif limited and stage_scored:
            missing_goodput.add("No client-qualified goodput rate was recorded for this load point.")
    shapes = {(row.load_point or {}).get("shape") for row in records} - {None}
    point_groups = [group for point in points for group in point.get("groups", [])
                    if not shapes or group.get("shape") in shapes]
    load_config = report.manifest.get("config", {}).get("load_test")
    settings = {"primary": load_config} if isinstance(load_config, dict) else {}
    for index, migrated in enumerate(settings.get("primary", {}).get("migrated_tests", [])):
        settings[f"legacy_{index}_{migrated['source']}"] = migrated["settings"]
    for point in points:
        groups = [group for group in point.get("groups", []) if not shapes or group.get("shape") in shapes]
        seconds = point.get("observation_seconds")
        criteria = (settings.get(point.get("source", "primary")) or {}).get("pass_criteria")
        limited = (any(criteria.get(key) is not None for key in ("max_first_answer_ms", "max_completion_ms"))
                   if criteria is not None else False)
        scored = any((_number(group.get("scored")) and group["scored"] > 0)
                     or (_number(group.get("correct_work_meeting_limits")) and group["correct_work_meeting_limits"] > 0)
                     for group in groups)
        if not limited:
            missing_goodput.add("No first-answer or completion latency limit is established.")
        if not scored:
            missing_goodput.add("Answers are unscored; answer correctness is unknown.")
        if seconds and groups and limited and scored and all(g.get("correct_work_meeting_limits") is not None and
                g.get("timing_quality", {}).get("status") == "qualified" for g in groups):
            goodput.append(sum(g["correct_work_meeting_limits"] for g in groups)/seconds)
        elif not missing_goodput:
            missing_goodput.add("Complete observation duration and qualified client timing are required.")
    coverage_groups = point_groups + legacy_coverage
    partial_scoring = any(group.get("unscored", 0) > 0 for group in coverage_groups)
    reason = (" ".join(sorted(missing_goodput)) if missing_goodput else
              "Separate load-point rates are not pooled; select one load-point window." if len(goodput) > 1 else
              "No scored and timing-qualified load-point evidence in this scope.")
    available_goodput = len(goodput) == 1 and not missing_goodput
    request_coverage = None
    if coverage_groups and all(group.get("scored") is not None and group.get("valid") is not None for group in coverage_groups):
        scored_count = sum(group["scored"] for group in coverage_groups)
        valid_count = sum(group["valid"] for group in coverage_groups)
        request_coverage = {"sample": scored_count, "eligible": valid_count, "missing": valid_count-scored_count}
    coverage_note = (f"{len(goodput)} / {len(points)+len(stages)} eligible load points"
                     + (f" · {request_coverage['sample']} / {request_coverage['eligible']} scored valid requests"
                        if request_coverage is not None else ""))
    put("goodput", goodput[0] if available_goodput else None,
        "Scored-subset correct goodput" if partial_scoring and goodput else "Goodput", "req/s",
        "Valid, correct answers meeting configured latency limits per observation second; unscored answers are excluded",
        samples=len(goodput), eligible=len(points)+len(stages), sample_unit="load points", request_coverage=request_coverage,
        scored_subset=partial_scoring, display_note=coverage_note,
        state="measured" if available_goodput else "insufficient_evidence",
        reason=None if available_goodput else reason,
        action=("Set a first-answer or completion latency limit and configure answer scoring to measure correct goodput; throughput and latency remain available."
                if not available_goodput else None))
    put("knee", None, "Knee / saturation concurrency", "requests", "No adaptive knee evidence in this revision",
        state="insufficient_evidence", reason="Not established. Sampled concurrency and observed throughput do not establish saturation.",
        action="Run a supported capacity-discovery workload when available.")
    if point_groups:
        put("latency_attainment", [{k: g.get(k) for k in ("shape", "valid", "timing_violations", "unattributed_timing_violations", "missing_timing", "timing_quality", "status", "reason")} for g in point_groups],
            "Latency-limit attainment", "responses", "Existing scoped per-response criteria and client-timing qualification; no new acceptance decision", samples=len(point_groups))
    sessions = by_id.get("sessions", {}).get("conversation_performance", observation.get("session_performance", {})) if cohort.startswith("session_") else {}
    if not sessions and cohort.startswith("session_"):
        from .session_metrics import performance
        from .runner import _stats
        session_observation = observation.get("sessions", {})
        if isinstance(session_observation, dict):
            sessions = performance(session_observation, records, _stats)
    turns = sessions.get("turns", [])
    put("turn_latency", [{k: row.get(k) for k in ("task_type", "mode", "turn", "p50_ms", "p95_ms", "first_output_p50_ms", "first_output_p95_ms", "attempted")} for row in turns] or None,
        "Per-turn latency / first output", "ms", "Existing session performance; turn and task populations retained", samples=len(turns), state="measured" if turns else "not_applicable")
    put("turn_generation", [{k: row.get(k) for k in ("task_type", "mode", "turn", "generation_tokens_per_second_mean", "actual_input_tokens", "actual_output_tokens", "unmeasured_generation_requests")} for row in turns] or None,
        "Per-turn generation pace / token growth", "tok/s, tokens", "Existing session performance; streaming estimates", samples=len(turns), state="estimated" if turns else "not_applicable")
    planned = sessions.get("planned_conversations")
    put("conversation_completion", sessions.get("completed_conversations", 0)/planned if planned else None,
        "Conversation completion rate", "ratio", "Completed / planned conversations from session lifecycle", samples=sessions.get("started_conversations", 0), state="measured" if planned else "not_applicable")
    put("user_fairness", sessions.get("users") or None, "Per-user generation pace / fairness", "tok/s", "Existing per-user estimates and equal-user weighting; missing coverage retained",
        samples=sessions.get("users_with_measured_generation", 0), state="estimated" if sessions.get("users") else "not_applicable")
    matched = [p for p in points if p.get("interference")]
    legacy = by_id.get("concurrency", {}).get("load_types", {}).get("interference", {}).get("points", [])
    interference = [p.get("interference") for p in matched] or [p.get("interference", p) for p in legacy if ids.intersection(p.get("request_ids", []))]
    for key, field, label in (("decode_interference", "slowdown", "Decode interference slowdown"),
                              ("response_interference", "response_time_ratio", "Response-time interference ratio")):
        values = []
        for evidence in interference:
            if not isinstance(evidence, dict):
                continue
            completion_only = evidence.get("comparison_metric") == "short-answer completion time"
            if key == "decode_interference" and completion_only:
                continue
            value = evidence.get("slowdown" if completion_only else field)
            if value is not None:
                values.append(value)
        put(key, values or None, label, "ratio", "Existing paired control/probe matched evidence; no pooled headline",
            samples=len(values), state="measured" if values else "not_applicable")
    workload = [{"point_id": p.get("id"), "shape": g.get("shape"), **{k:g.get(k) for k in
        ("client_mean_inflight", "client_peak_inflight", "requested_share_percent", "observed_request_share_percent", "server_evidenced_concurrency")}}
        for p in points for g in p.get("groups", []) if not shapes or g.get("shape") in shapes]
    put("workload_occupancy", workload or None, "Mixed-workload occupancy / shares", "requests, ratio", "Existing per-shape delivery/occupancy evidence", samples=len(workload), state="measured" if workload else "not_applicable")
    recovery = by_id.get("recovery", {}) if cohort.startswith(("recovery", "sustained")) else {}
    ratios = {k: recovery[k] for k in ("latency_ratio", "first_output_ratio") if recovery.get(k) is not None}
    put("recovery_ratios", ratios or None, "Recovery latency / first-output ratio", "ratio", "Existing recovery probe / baseline medians", samples=len(ratios), state="measured" if ratios else "not_applicable")
    followup = by_id.get("cancellation", {}).get("followup_delay") if any("cancellation" in r.check_ids for r in records) else None
    put("cancel_followup", followup, "Post-cancellation follow-up delay", "ms", "Existing matched cancellation follow-up evidence", samples=1 if followup else 0, state="measured" if followup else "not_applicable")


def _spec_decode_coverage(details):
    observed = details.get("observed_intervals", 0)
    excluded = details.get("excluded_intervals", 0)
    note = f"{observed} / {observed + excluded} telemetry intervals"
    first, last = details.get("first_elapsed_seconds"), details.get("last_elapsed_seconds")
    duration = details.get("duration_seconds")
    if _number(first) and _number(last):
        note += f" · {first:g}–{last:g} s elapsed"
    if _number(duration):
        note += f" · {duration:g} s covered"
    if details.get("partial_coverage"):
        note += " · Partial coverage; totals include valid intervals only"
    return note + " · Server-wide; attribution to the selected request window is not established."


def _server_rows(report, target, window, observation, put, metrics):
    from .telemetry import derive_server_metrics

    telemetry = observation.get("serving_telemetry", {})
    target_config = next((t for t in report.manifest.get("config", {}).get("targets", []) if t.get("name") == target), {})
    derived = telemetry.get("derived") or derive_server_metrics(telemetry, host_configured=bool(target_config.get("host_metrics_url")))
    if any(key not in derived for key, _, _ in SPEC_DECODE_ROWS):
        fresh = derive_server_metrics(telemetry)
        derived = {**derived, **{key: fresh[key] for key, _, _ in SPEC_DECODE_ROWS
                                if key not in derived and key in fresh}}
    mapping = (
        ("server_ttft", "server_ttft_mean_ms", "Server TTFT", "ms"),
        ("gpu_utilization", "utilization", "GPU utilization", "%"),
        ("gpu_memory", "memory", "GPU memory used", "bytes"),
        ("gpu_temperature", "temperature", "GPU temperature", "°C"),
        ("gpu_errors", "device_errors", "GPU device errors", "code"),
        ("server_running", "running", "Server running requests", "requests"),
        ("server_waiting", "waiting", "Server waiting requests", "requests"),
        ("kv_cache", "cache_occupancy", "KV-cache occupancy", "%"),
        ("prefix_cache", "prefix_cache_hit_ratio", "Server prefix-cache hit rate", "ratio"),
        ("preemptions", "server_preemptions_per_second", "Preemptions / retractions", "events/s"),
        ("server_queue", "server_queue_mean_ms", "Server queue duration", "ms"),
        ("server_inference", "server_inference_mean_ms", "Server inference duration", "ms"),
        ("server_tokens", "server_generated_tokens_per_second", "Server token throughput", "tok/s"),
        ("host_cpu", "host_cpu_percent", "Server CPU utilization", "%"),
        ("host_ram", "host_ram_used_bytes", "Server host RAM", "bytes"),
    ) + tuple((key, key, label, unit) for key, label, unit in SPEC_DECODE_ROWS)
    # Scrapes have a separate collection window. Never relabel them as linked
    # request/stage samples. Preserve raw bounds/coverage in each row.
    for key, derived_key, label, unit in mapping:
        row = derived.get(derived_key, {})
        if row.get("value") is None and key not in {"host_cpu", "host_ram"} and not key.startswith("spec_decode_") and not telemetry.get("snapshots"):
            row = {**row, "state": "not_collected", "reason": "This saved run has no runtime telemetry collection evidence.",
                   "action": "New runs discover same-origin /metrics automatically; an exporter at another route requires targets[].metrics_url."}
        put(key, row.get("value"), label, row.get("unit", unit), row.get("source", "Server telemetry"),
            samples=row.get("sample_count", 0), state=row.get("state", "not_collected"),
            reason=row.get("reason"), action=row.get("action"), scope="server-wide",
            collection_window="telemetry sample interval; may extend beyond selected request window",
            details={**row.get("details", {}), "selected_window_attribution": "not established"})
        metrics[key]["action"] = row.get("action")
        metrics[key]["eligible_count"] = row.get("eligible_count", 0)
        metrics[key]["counts"] = {"sample": row.get("sample_count", 0), "eligible": row.get("eligible_count", 0),
                                  "missing": max(0, row.get("eligible_count", 0)-row.get("sample_count", 0))}
        if key.startswith("spec_decode_"):
            metrics[key]["partial"] = bool(row.get("details", {}).get("partial_coverage"))
            details = row.get("details", {})
            if key.startswith("spec_decode_sglang_"):
                metrics[key]["sample_unit"] = "label series in latest scrape"
                metrics[key]["display_note"] = " · ".join(str(v) for v in (
                    "Latest exporter sample", details.get("collected_at"),
                    details.get("aggregation"), details.get("freshness"),
                    "Server-wide; attribution to the selected request window is not established.") if v)
            else:
                metrics[key]["sample_unit"] = "telemetry intervals"
                metrics[key]["display_note"] = _spec_decode_coverage(details)
        if key in {"server_ttft", "server_queue", "server_inference"}:
            family = {"server_ttft": "server_ttft", "server_queue": "server_queue", "server_inference": "server_inference"}[key]
            metrics[key]["statistic"] = "counter-delta mean; not a request median"
            metrics[key]["histogram_percentiles"] = {q: derived.get(f"{family}_{q}_ms") for q in ("p95", "p99")}
    for key, row in derived.items():
        if key in {derived_key for _, derived_key, _, _ in mapping}:
            continue
        put(key, row.get("value"), key.replace('_', ' ').capitalize(), row.get("unit", ""), row.get("source", "Server telemetry"),
            samples=row.get("sample_count", 0), state=row.get("state", "not_collected"), reason=row.get("reason"), action=row.get("action"),
            scope="server-wide", collection_window="telemetry sample interval", details=row.get("details", {}))
        metrics[key]["action"] = row.get("action")
    accepted = derived.get("spec_decode_accepted_tokens", {})
    positions = accepted.get("details", {}).get("accepted_tokens_by_position", [])
    if positions:
        coverage = accepted["details"].get("position_coverage", {})
        put("spec_decode_accepted_by_position", positions, "Accepted speculative tokens by position", "",
            "Optional vLLM position counters; zero-based positions. Acceptance rate uses all drafts on the engines exposing each position, not only drafts proposing that position.",
            samples=coverage.get("observed_intervals", 0),
            eligible=coverage.get("observed_intervals", 0) + coverage.get("excluded_intervals", 0),
            scope="server-wide", collection_window="telemetry sample interval",
            partial=bool(coverage.get("partial_coverage")), sample_unit="telemetry intervals",
            display_note=_spec_decode_coverage(coverage),
            details={**coverage, "selected_window_attribution": "not established"})
    put("websocket_rtt", None, "WebSocket round-trip time", "ms", "Inference transport: HTTP/SSE", state="not_applicable",
        reason="HTTP/SSE requests have no WebSocket round trip.", action="An actual WebSocket transport and request/response timing would be required.")


def _baseline_rows(report, target, window, records, put):
    baseline = report.baseline
    entry = next((t for t in (baseline or {}).get("targets", []) if t.get("target") == target), {})
    cohort = window["cohort_id"]
    scenarios = {r.scenario for r in records}
    load_points = [r.load_point for r in records if r.load_point and "load" in r.check_ids]

    def matches(row):
        if row.get("check") != "load":
            return row.get("scenario") in scenarios or row.get("scenario") == cohort
        point = row.get("load_point") or {}
        fields = ("source", "type", "workload", "shape", "pattern", "level", "rate", "bursts")
        return bool(point) and any(
            (point["point_id"] == other.get("point_id") if point.get("point_id")
             else all(point.get(key) == other.get(key) for key in fields))
            and point.get("shape") == other.get("shape")
            and point.get("phase") == other.get("phase")
            and (point.get("role") or "request") == (other.get("role") or "request")
            for other in load_points)

    # Existing comparisons already carry sample/shape/timing qualifications.
    comparisons = [r for r in entry.get("comparisons", []) if matches(r)]
    traffic = [row for index, row in enumerate(entry.get("traffic_capacity", {}).get("stages", []))
               if index in {record.traffic_stage for record in records} and row.get("status") in {"pass", "fail"}]
    mapping = (("baseline_throughput", "Throughput change", {"output_tokens_per_second"}),
               ("baseline_timing", "Latency / first-output change", {"latency_ms", "first_output_ms", "max_stream_gap_ms"}),
               ("baseline_generation", "Generation pace change", {"generation_tokens_per_second"}),
               ("baseline_quality", "Error / correctness change", {"error_rate", "correctness_rate", "valid_completion_rate"}))
    for key, label, names in mapping:
        rows = [r for r in comparisons if r.get("metric") in names]
        qualified = [r for r in rows if r.get("status") in {"pass", "fail"} and "worsening" in r]
        if key == "baseline_throughput":
            qualified += [{"goodput_delta_rps": row.get("goodput_delta_rps"), "rate_rps": row.get("rate_rps"),
                           "kind": "qualified arrival goodput change", "status": row["status"]} for row in traffic
                          if row.get("goodput_delta_rps") is not None]
        put(key, qualified or None, label, "% change / percentage points", "Existing like-for-like qualified baseline comparisons; per-check/scenario",
            samples=len(qualified), state="measured" if qualified else ("no_baseline" if baseline is None else "insufficient_evidence"),
            reason=None if qualified else ("No baseline attached." if baseline is None else "No comparable qualified baseline population for this scope."),
            evidence=rows, comparable=bool(qualified),
            qualification="Output-token rates in existing comparisons are per-request end-to-end rates; goodput deltas use requests/s. No aggregate throughput delta is invented.")
    point_ids = {point.get("point_id") for point in load_points}
    capacity = [r for r in entry.get("load_test", {}).get("points", [])
                if (r.get("current") or {}).get("id") in point_ids
                and (r.get("acceptance_change") in {"gained", "lost", "unchanged_accepted", "unchanged_not_accepted"}
                     or ("acceptance_change" not in r and r.get("status") in {"pass", "fail"}))]
    capacity += traffic
    put("baseline_capacity", capacity or None, "Accepted capacity change", "tested load", "Existing qualified tested-load baseline; not inferred saturation",
        samples=len(capacity), state="measured" if capacity else ("no_baseline" if baseline is None else "insufficient_evidence"))


def build_performance(report, *, target=None, window=None):
    """Use saved default metrics, or derive an explicitly selected target/window.

    Only the default selection is persisted, without the raw request series.
    Alternate selections intentionally use the current measurement code.
    The window catalog is cheap; only the selected summary is aggregated. Raw
    dispatch-ordered series occurs once per target. Summaries reference its row
    indices instead of copying rows. Missing selectors choose the first target
    and the performance workload with most valid completions (earliest on ties). Unknown explicit selectors are errors.
    """
    if target is None and window is None and getattr(report, "_stored_derived", False):
        # Persist the default headline; alternate target/window selections remain
        # computed from retained request evidence. Raw rows are never duplicated on disk.
        from copy import deepcopy
        stored = deepcopy(report.performance)
        for entry in stored.get("targets", []):
            records = sorted((r for r in report.requests if r.target == entry["target"]),
                             key=lambda r: (r.dispatch_ms is None, r.dispatch_ms or 0, r.started_at, r.id))
            entry["request_series"] = _request_series(records)
        return stored
    requested_window = window
    config_targets = report.manifest.get("config", {}).get("targets", [])
    names = list(dict.fromkeys([t["name"] for t in config_targets if t.get("name")] + [r.target for r in report.requests]))
    selected = target if target is not None else (names[0] if names else None)
    if target is not None and target not in names:
        raise ValueError("Unknown performance target")
    available = [{"target": name, "identity": next(({
        k: item.get(k) for k in ("name", "url", "model", "route", "parent")
    } for item in config_targets if item.get("name") == name), {"name": name})} for name in names]
    targets = []
    for name in [selected] if selected is not None else []:
        records = [r for r in report.requests if r.target == name]
        records.sort(key=lambda r: (r.dispatch_ms is None, r.dispatch_ms if r.dispatch_ms is not None else 0, r.started_at, r.id))
        series = _request_series(records)
        groups = defaultdict(list)
        for record in records:
            groups[_cohort(record)].append(record)
        observation = report.observations.get("targets", {}).get(name, {})
        cohorts = []
        for key, group in groups.items():
            check, label = _cohort_label(group, observation)
            cohorts.append(dict(id=key, label=label, check=check, request_count=len(group),
                valid_completions=sum(row.status == "completed" and row.valid for row in group),
                preparation=all(_preparation(row) for row in group),
                performance_workload=bool(_PERFORMANCE_CHECKS.intersection(group[0].check_ids)),
                scenarios=list(dict.fromkeys(row.scenario for row in group))))
        candidates = [row for row in cohorts if not row["preparation"]] or cohorts
        representative = max(candidates, key=lambda row: (row["performance_workload"], row["valid_completions"])) if candidates else None
        default = representative["id"] if representative else None
        windows, summaries = [], {}
        for descriptor in cohorts:
            cohort, group = descriptor["id"], groups[descriptor["id"]]
            start, end = _observed_interval(group)
            indices = _cohort_schedule(group, records, observation)
            stages = observation.get("schedule_transitions", [])
            sending = [stages[index] for index in indices if stages[index].get("kind") != "drain"]
            count_words = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}
            kinds = {row.get("kind") for row in sending}
            changes = (f"{count_words.get(len(sending), str(len(sending)))} changing " +
                       ("concurrency" if kinds == {"concurrency"} else "arrival-rate" if kinds == {"arrival_rate"} else "load") + " stages") if len(sending) > 1 else None
            window = dict(id=f"full:{cohort}", kind="full", label="Full request window", cohort_id=cohort,
                          workload_label=descriptor["label"], stage_count=len(sending),
                          stage_labels=[_stage_label(row) for row in sending], stages_description=changes,
                          schedule_indices=indices,
                          start_ms=start, end_ms=end, duration_s=(end-start)/1000 if start is not None and end is not None else None,
                          request_count=len(group), includes_startup=True, includes_drain=True,
                          population="One comparable workload across its recorded load stages; preparation, shapes, sources and control/probe roles remain separate.")
            _window_labels(window, start)
            windows.append(window)
            for index in indices:
                stage = stages[index]
                left = stage["start_ms"]
                right = stage.get("end_ms") if stage.get("end_ms") is not None else end
                if start is None or end is None or right < start or left > end:
                    continue
                inclusive = not any(stages[other].get("start_ms") == right for other in indices)
                dispatched = sum(row.dispatch_ms is not None and left <= row.dispatch_ms < right for row in group)
                completed = sum(row.completed_ms is not None and left <= row.completed_ms <= right and (inclusive or row.completed_ms < right) for row in group)
                drain = stage.get("kind") == "drain"
                if not drain and not dispatched:
                    continue
                if drain and not completed and not any(row.dispatch_ms is not None and row.dispatch_ms < right and (row.completed_ms is None or row.completed_ms > left) for row in group):
                    continue
                item = dict(id=f"stage:{index}:{cohort}", kind="stage", label=_stage_label(stage),
                    workload_label=descriptor["label"], cohort_id=cohort, stage_id=stage["stage_id"],
                    schedule_indices=[index], start_ms=left, end_ms=right, end_inclusive=inclusive,
                    duration_s=(right-left)/1000, request_count=completed if drain else dispatched,
                    dispatch_count=dispatched, completion_count=completed, includes_startup=False,
                    includes_drain=drain,
                    population="Request count is dispatches during the sending stage (completions during drain). Metrics use terminal events in [start,end); occupancy includes this workload's carry-in lifetimes.")
                _window_labels(item, start)
                windows.append(item)
        if not windows:
            window = dict(id="full", kind="full", label="Full request window", cohort_id="empty", start_ms=None,
                          end_ms=None, duration_s=None, request_count=0, includes_startup=True, includes_drain=True,
                          population="No request lifetime evidence.", workload_label="No requests",
                          stage_count=0, stage_labels=[], schedule_indices=[])
            _window_labels(window, None)
            windows.append(window)
        default_window = f"full:{default}" if default is not None else "full"
        selected_window = requested_window if requested_window is not None else default_window
        descriptor = next((item for item in windows if item["id"] == selected_window), None)
        if descriptor is None:
            raise ValueError("Unknown performance window")
        group = groups.get(descriptor["cohort_id"], [])
        cohort_series = [row for row in series if row["cohort_id"] == descriptor["cohort_id"]]
        summaries[selected_window] = _window_summary(report, name, descriptor, group, cohort_series, observation)
        full_start, full_end = _observed_interval(records)
        target_config = next((t for t in config_targets if t.get("name") == name), {})
        targets.append(dict(target=name, identity={k: target_config.get(k) for k in ("name", "url", "model", "route", "parent")}, default_cohort=default,
            default_window=default_window, selected_window=selected_window, cohorts=cohorts,
            windows=windows, summaries=summaries, request_series=series,
            full_request_window={"start_ms": full_start, "end_ms": full_end, "request_count": len(records),
                "population": "All target requests, including preparation and distinct conditions; no pooled headline."},
            full_series_summary_window=selected_window if descriptor["kind"] == "full" and len(group) == len(records) else None,
            schedule_transitions=observation.get("schedule_transitions", []),
            scope_note="Headlines use one comparable workload across its recorded load stages; shapes, sources and control/probe roles remain separate. Every request remains in the dispatch-ordered series; observed throughput does not establish capacity."))
    return dict(performance_schema=PERFORMANCE_SCHEMA, timing_schema=1,
                percentile_convention=PERCENTILES, default_target=names[0] if names else None,
                selected_target=selected, available_targets=available, targets=targets)


def refresh_performance(report, *, force=False):
    if getattr(report, "_stored_derived", False) and not force:
        return report
    report._stored_derived = False
    report.performance = build_performance(report)
    from .conditions import aggregate_conditions
    report.conditions = aggregate_conditions(report)
    return report


def cli_summary(report):
    performance = report.performance or build_performance(report)
    lines = []
    for entry in performance["available_targets"]:
        target = (performance if entry["target"] == performance["selected_target"] else
                  build_performance(report, target=entry["target"]))["targets"][0]
        wid = target["default_window"]
        summary = target["summaries"][wid]
        rows = summary["metrics"]
        available, unavailable = [], []
        for key in KPI_IDS:
            row = rows[key]
            if row["value"] is not None:
                available.append(f"{row['label']}: {row['value']:.3g} {row['unit']}" + (" (estimate)" if row["state"] == "estimated" else ""))
            else:
                unavailable.append(f"{row['label']}: {row['state'].replace('_', ' ')}")
        lines.append(f"Performance · {target['target']} · {wid}")
        if available:
            lines.append("  " + " · ".join(available))
        if unavailable:
            lines.append("  Availability: " + "; ".join(unavailable))
    return lines
