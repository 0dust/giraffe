"""Per-user delivery over explicit common windows, separate from generation pace."""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from itertools import combinations
from operator import itemgetter
from typing import Any

from .models import LoadTestSettings, MultiUserCriteria, RequestRecord
from .overlap import _delays, _overlap, answer_window, max_evidenced_overlap
from .stats import distribution, median
from .timing import qualify, threshold


SUMMARY_VERSION = 1


def _status(statuses: list[str], *, measured: bool = False) -> str:
    if "fail" in statuses:
        return "fail"
    if "inconclusive" in statuses or not statuses:
        return "inconclusive"
    return "measured" if measured else "pass"


def _valid(record: RequestRecord) -> bool:
    return record.status == "completed" and not _error(record)


def _error(record: RequestRecord) -> bool:
    return record.status in {"failed", "timeout"} or record.status == "completed" and (
        not record.valid or record.score is False or truncated_conversation_turn(record))


def truncated_conversation_turn(record: RequestRecord) -> bool:
    return (record.status == "completed" and bool(record.workload.get("session_id"))
            and record.finish_reason == "length")


def _tokens(records: list[RequestRecord]) -> tuple[int, int]:
    return (sum(r.output_tokens for r in records if r.output_tokens is not None),
            sum(r.output_tokens is None for r in records))


def _has_streamed_answer(record: RequestRecord) -> bool:
    # Retained usage can contradict text classified as answer during the stream.
    return (record.stream and record.output_tokens != 0 and record.completion_tokens != 0
            and record.answer_reads >= 2 and record.answer_chunks >= 2)


def _first(record: RequestRecord) -> float | None:
    start = record.timing_start_ms if record.timing_start_ms is not None else record.dispatch_ms
    return start + record.first_output_ms if start is not None and record.first_output_ms is not None else None


def _first_generation(record: RequestRecord) -> float | None:
    start = record.timing_start_ms if record.timing_start_ms is not None else record.dispatch_ms
    first = min((value for value in (record.first_token_ms, record.first_reasoning_ms, record.first_output_ms)
                 if value is not None), default=None)
    return start + first if start is not None and first is not None else None


def user_latency(record: RequestRecord, first: bool, *, ready: bool = True) -> float | None:
    start = record.workload.get("ready_ms") if ready else record.dispatch_ms
    end = _first(record) if first else record.completed_ms
    return max(0, end - start) if start is not None and end is not None else None


def user_delivery_completions(records: list[RequestRecord], window: dict[str, Any],
                              user_id: str, *, include_drain: bool = False) -> list[RequestRecord]:
    """Select valid completions admitted in the window, excluding drain by default."""
    start, end = window.get("start_ms"), window.get("end_ms")
    if start is None or end is None:
        return []
    return [r for r in records if r.workload.get("user_id") == user_id
            and r.workload.get("window_id") == window["id"] and not r.intentional and _valid(r)
            and r.dispatch_ms is not None and start <= r.dispatch_ms < end
            and r.completed_ms is not None and (include_drain or r.completed_ms <= end)]


def _user_summary(user: dict[str, Any], window: dict[str, Any], records: list[RequestRecord],
                  criteria: MultiUserCriteria, health: dict | None, lag: float,
                  timing_threshold: float, *, conversation_modes=()) -> dict[str, Any]:
    start, end = window.get("start_ms"), window.get("end_ms")
    duration = max(0, (end-start)/1000) if start is not None and end is not None else 0
    rows = [r for r in records if r.workload.get("user_id") == user["user_id"]
            and r.workload.get("window_id") == window["id"]
            and r.dispatch_ms is not None and start is not None and end is not None
            and start <= r.dispatch_ms
            and (r.dispatch_ms < end or window.get("cut_short") and r.dispatch_ms == end)]
    missing_terminal = max(0, user.get("dispatched_count", len(rows))-len(rows))
    intentional = [r for r in rows if r.intentional]
    attempts = [r for r in rows if not r.intentional]
    valid = [r for r in attempts if _valid(r)]
    completed = [r for r in attempts if r.status == "completed"]
    errors = [r for r in attempts if _error(r)]
    cancelled = [r for r in attempts if r.status == "cancelled"]
    terminal_failures = {r.id for r in errors + cancelled}
    conversations = user.get("conversations", [])
    unadmitted_turn = any(item.get("stopped_before_cutoff") and terminal_failures.isdisjoint(item["turns"])
                          for item in conversations)
    valid_ids = {record.id for record in valid}
    expected_modes = set(conversation_modes) | {item["mode"] for item in conversations}
    complete_modes = {item["mode"] for item in conversations if not item.get("stop_reason")
                      and (turns := item["turns"]) and len(turns) == len(set(turns)) == item["planned_turns"]
                      and valid_ids.issuperset(turns)}
    missing_modes = sorted(expected_modes - complete_modes)
    eligible = user_delivery_completions(records, window, user["user_id"])
    unclassified = [r for r in valid if r.completed_ms is None]
    late = [r for r in valid if r.completed_ms is not None and r.completed_ms > end]
    partial = [r for r in rows if not _valid(r)]
    known, missing = _tokens(eligible)
    late_tokens, late_missing = _tokens(late)
    partial_tokens, partial_missing = _tokens(partial)
    cancelled_tokens, cancelled_missing = _tokens([r for r in rows if r.status == "cancelled"])
    known_rate = known/duration if duration > 0 else None
    rate = known_rate if not missing and not unclassified and not missing_terminal else None
    if not rows and not user.get("dispatched_count"):
        rate = known_rate = 0.0

    timing_rows = [r.model_copy(update={"scheduled_ms": r.workload.get("ready_ms")}) for r in valid]
    quality = qualify(timing_rows, health, timing_threshold,
                      ("latency_ms", "first_output_ms"), legacy_max_lag_ms=lag)
    pending_ready, pending_end = user.get("pending_ready_ms"), user.get("pending_observed_ms")
    pending = None
    if pending_ready is not None and pending_end is not None:
        pending_quality = qualify([], health, timing_threshold, ("latency_ms",), legacy_max_lag_ms=lag,
                                  phase_intervals=((pending_ready, pending_end),))
        pending = {"ready_ms": pending_ready, "observed_ms": pending_end,
                   "wait_ms": max(0, pending_end-pending_ready),
                   "status": "right_censored", "attribution": "client_admission",
                   "timing_quality": pending_quality["status"], "breached_criteria": []}

    incomplete = []
    if not attempts:
        incomplete.append("No nonintentional requests were dispatched.")
    if len(valid) < criteria.min_samples:
        incomplete.append(f"Only {len(valid)} valid responses; {criteria.min_samples} required.")
    if window.get("cut_short") or duration <= 0:
        incomplete.append("The common measurement window was not completed.")
    if window.get("drain_reason") or cancelled or missing_terminal:
        incomplete.append("Some admitted work has incomplete terminal coverage.")
    if unadmitted_turn:
        incomplete.append("Some required conversation turns were not admitted before the window closed.")
    if missing_modes:
        incomplete.append(f"Conversation coverage is incomplete for {', '.join(missing_modes)}.")
    reasons = []
    selected = {}
    for name, first, metric in (("max_first_answer_ms", True, "first_output_ms"),
                                ("max_completion_ms", False, "latency_ms")):
        limit = getattr(criteria, name)
        if limit is None:
            continue
        met, missed, unknown = 0, 0, missing_terminal
        affected = set(quality["affected_metrics"][metric])
        for record in attempts:
            if _error(record):
                missed += 1
            elif record.status == "cancelled":
                unknown += 1
            else:
                value = user_latency(record, first)
                if value is None or record.id in affected:
                    unknown += 1
                elif value > limit:
                    missed += 1
                else:
                    met += 1
        pending_breach = bool(pending and pending["wait_ms"] > limit)
        pending_unknown = pending_breach and pending["timing_quality"] != "qualified"
        if pending_breach and not pending_unknown:
            pending["status"] = "breach"
            pending["breached_criteria"].append(name)
        elif pending_unknown and pending["status"] != "breach":
            pending["status"] = "unqualified"
        status = "fail" if missed or pending_breach and not pending_unknown else (
            "inconclusive" if unknown or incomplete or pending_unknown else "pass")
        denominator = len(attempts)+missing_terminal
        selected[name] = {"status": status, "limit": limit, "met": met, "missed": missed,
                          "unknown": unknown, "excluded": len(intentional),
                          "denominator": denominator,
                          "attainment_fraction": met/denominator if denominator else None,
                          "pending_breach": pending_breach and not pending_unknown,
                          "pending_unknown": pending_unknown}
        if missed:
            reasons.append(f"{missed} attempted requests missed {name}.")
        if pending_breach and not pending_unknown:
            reasons.append(f"Client admission wait exceeded {name} before dispatch.")
        if unknown or pending_unknown:
            reasons.append(f"Some {name} measurements are missing or affected by client scheduling delay.")

    denominator = len(attempts)+missing_terminal
    error_percent = 100*len(errors)/denominator if denominator else None
    if criteria.max_error_percent is not None:
        failed = error_percent is not None and error_percent > criteria.max_error_percent
        selected["max_error_percent"] = {
            "status": "fail" if failed else "inconclusive" if incomplete else "pass",
            "limit": criteria.max_error_percent, "value": error_percent,
            "errors": len(errors), "denominator": denominator,
            "unknown": len(cancelled)+missing_terminal, "excluded": len(intentional)}
        if failed:
            reasons.append(f"Observed request error rate exceeds {criteria.max_error_percent:g}%.")
    if criteria.min_output_tokens_per_second is not None:
        interrupted = bool(window.get("cut_short") or window.get("drain_reason")
                           or cancelled or missing_terminal or unadmitted_turn or missing_modes)
        failed = (bool(attempts) and not interrupted and rate is not None
                  and rate < criteria.min_output_tokens_per_second)
        selected["min_output_tokens_per_second"] = {
            "status": "fail" if failed else "inconclusive" if rate is None or incomplete else "pass",
            "limit": criteria.min_output_tokens_per_second, "value": rate,
            "known_output_tokens": known, "missing_usage_count": missing}
        if failed:
            reasons.append("Shared-window output throughput is below the selected minimum.")
    status = _status([item["status"] for item in selected.values()]) if selected else "measured"
    if status == "inconclusive":
        reasons.extend(incomplete)
    ready_first = [user_latency(r, True) for r in valid]
    ready_completion = [user_latency(r, False) for r in valid]
    dispatch_first = [user_latency(r, True, ready=False) for r in valid]
    dispatch_completion = [user_latency(r, False, ready=False) for r in valid]
    coverage_reasons = list(incomplete)
    if missing:
        coverage_reasons.append("Some eligible completions have no reported output token count.")
    if unclassified:
        coverage_reasons.append("Some successful responses lack a completion timestamp.")
    if any(value is None for value in ready_first):
        coverage_reasons.append("Some successful responses lack first-answer timing.")
    if quality["status"] != "qualified":
        coverage_reasons.append("Some response timings overlap client scheduling delay.")
    if pending and pending["timing_quality"] != "qualified":
        coverage_reasons.append("Pending client admission wait overlaps scheduling delay.")
    return {**user, "status": status, "reasons": list(dict.fromkeys(reasons)),
            "request_ids": [r.id for r in rows],
            "outcomes": {"attempted": len(rows)+missing_terminal, "completed": len(completed),
                         "valid_completed": len(valid), "errors": len(errors),
                         "failed": sum(r.status == "failed" for r in attempts),
                         "invalid": sum(r.status == "completed" and _error(r) for r in attempts),
                         "timeouts": sum(r.status == "timeout" for r in attempts),
                         "cancelled": len(cancelled),
                         "intentional_cancelled": sum(r.status == "cancelled" for r in intentional),
                         "missing_terminal": missing_terminal},
            "known_output_tokens": known, "missing_usage_count": missing,
            "known_output_tokens_per_second": known_rate,
            "output_tokens_per_second": rate, "late_output_tokens": late_tokens,
            "late_missing_usage_count": late_missing, "late_completed": len(late),
            "unclassified_completed_count": len(unclassified),
            "partial_output_tokens": partial_tokens, "partial_missing_usage_count": partial_missing,
            "cancelled_output_tokens": cancelled_tokens, "cancelled_missing_usage_count": cancelled_missing,
            "latency": {"ready_first_answer_ms": distribution(ready_first),
                        "ready_completion_ms": distribution(ready_completion),
                        "dispatch_first_answer_ms": distribution(dispatch_first),
                        "dispatch_completion_ms": distribution(dispatch_completion),
                        "client_wait_ms": distribution([
                            r.dispatch_ms-r.workload["ready_ms"] for r in rows
                            if r.workload.get("ready_ms") is not None])},
            "lengths": {"input_tokens": distribution([r.input_tokens for r in rows]),
                        "output_tokens": distribution([r.output_tokens for r in rows]),
                        "requested_input_tokens": distribution([r.requested_input_tokens for r in rows]),
                        "requested_output_tokens": distribution([r.requested_output_tokens for r in rows]),
                        "missing_input_count": sum(r.input_tokens is None for r in rows),
                        "missing_output_count": sum(r.output_tokens is None for r in rows),
                        "early_eos_count": sum(r.finish_reason == "stop" and r.output_tokens is not None
                            and r.requested_output_tokens is not None and r.output_tokens < r.requested_output_tokens
                            for r in valid)},
            "generation_tokens_per_second": distribution([r.generation_tokens_per_second for r in valid]),
            "finish_reasons": dict(Counter(r.finish_reason or "unknown" for r in rows)),
            "pending_wait": pending, "timing_quality": quality, "criteria": selected,
            "coverage": {"status": "unexercised" if not attempts else "partial" if coverage_reasons else "complete",
                         "reasons": coverage_reasons, "valid_samples": len(valid),
                         "unadmitted_conversation_turn": unadmitted_turn,
                         "missing_conversation_modes": missing_modes,
                         "required_samples": criteria.min_samples,
                         "missing_first_answer_count": sum(value is None for value in ready_first),
                         "missing_completion_count": sum(value is None for value in ready_completion),
                         "qualified_first_answer_count": sum(value is not None and r.id not in quality["affected_metrics"]["first_output_ms"]
                                                              for r, value in zip(valid, ready_first)),
                         "qualified_completion_count": sum(value is not None and r.id not in quality["affected_metrics"]["latency_ms"]
                                                            for r, value in zip(valid, ready_completion))}}


def _variable(distribution: dict | None) -> bool:
    if not distribution:
        return False
    values = distribution.get("values", [])
    weights = distribution.get("weights") or [1] * len(values)
    return len({value for value, weight in zip(values, weights) if weight > 0}) > 1


def _uncached_input_tokens(record: RequestRecord) -> int | None:
    total, cached = record.input_tokens, record.cached_prompt_tokens
    if total is None or cached is None or not 0 <= cached <= total:
        return None
    return total - cached


def _total_generated_tokens(record: RequestRecord) -> int | None:
    if record.completion_tokens is not None:
        return record.completion_tokens
    if record.output_tokens is not None:
        return record.output_tokens + (record.reasoning_tokens or 0)
    return None


def _length_mix_differs(samples: list[list[int | None]], tolerance: float) -> bool:
    """Compare how much token work per request changes between sampled length mixes."""
    histograms = [Counter(value for value in lengths if value is not None) for lengths in samples]
    cohorts = [(counts, sum(counts.values()), sum(length*count for length, count in counts.items()))
               for counts in histograms if counts]
    for (left, left_n, left_tokens), (right, right_n, right_tokens) in combinations(cohorts, 2):
        # Integrate the gap between normalized cumulative counts. This compares
        # per-request mixes even when different numbers of requests completed.
        gap = distance = previous = 0
        allowed = min(left_tokens*right_n, right_tokens*left_n) * tolerance / 100
        for length in sorted(left.keys() | right.keys()):
            distance += abs(gap) * (length - previous)
            if distance > allowed:
                return True
            gap += left.get(length, 0)*right_n - right.get(length, 0)*left_n
            previous = length
    return False


def _input_shape_floors(observation, output):
    variant = "short_answer" if output == "short" else "long_output"
    return {item["size"]: item["input_tokens"] for item in observation.get("calibrations", [])
            if item.get("variant") == variant and item.get("status") == "calibrated"
            and item.get("reason_code") == "minimum_prompt_length"
            and item.get("token_count_scope") != "builtin_reference" and item.get("input_tokens") is not None}


def _input_shape(observation, settings, profile, records):
    from .load_test import input_qualification
    size, output = profile.split("_")
    return input_qualification(settings, size, records, floors=_input_shape_floors(observation, output))


def _fairness(observation, window, users, records, criteria, health, lag, timing_threshold):
    """Qualify observed delivery, never attribute a difference to server scheduling."""
    settings = observation.get("settings", {})
    tolerance = settings.get("fairness_tolerance_percent", 10)
    limit = criteria.max_throughput_ratio
    start, end = window.get("start_ms"), window.get("end_ms")
    phase_quality = qualify([], health, timing_threshold, ("latency_ms",), legacy_max_lag_ms=lag,
                            phase_intervals=((start, end),) if start is not None and end is not None else ())
    interpretation = (
        "Median user throughput divided by worst user throughput over the common window. "
        "Comparability tolerance applies to actual token length medians and mixes, including total generation "
        "and reported uncached input, and active exposure. "
        "This measures observed delivery fairness, not a server scheduling defect."
    )
    if len(users) < 2:
        return {"status": "not_applicable", "qualified": False, "threshold": limit,
                "tolerance_percent": tolerance, "ratio": None, "compared_user_ids": [],
                "eligible_user_ids": [], "overlap_user_ids": [],
                "reasons": ["Fairness requires at least two compared users."],
                "users": [], "timing_quality": phase_quality, "interpretation": interpretation}
    reasons = []
    if window.get("cut_short") or start is None or end is None or end <= start:
        reasons.append("The common measurement window was not completed.")
    if window.get("drain_reason"):
        reasons.append("The measurement has incomplete drain coverage.")
    if len({user["profile"] for user in users}) > 1:
        reasons.append("Work not comparable: assigned profiles differ.")
    plans = [user.get("think_time_plan", settings.get("think_time_seconds", {"values": [0]}))
             for user in users]
    if plans and (any(plan != plans[0] for plan in plans[1:])
                  or any(_variable(plan) and not plan.get("seed_key") for plan in plans)):
        reasons.append("Work not comparable: users have different or unverified think-time plans.")
    conversation = settings.get("conversations") or {}
    if any(_variable(value) for key, value in (conversation.get("sampling") or {}).items()
           if key != "seed"):
        reasons.append("Work not comparable: independently sampled conversation plans differ.")
    saved_delay = start is not None and end is not None and any(
        start < delay["end_ms"] and delay["start_ms"] < end
        for delay in observation.get("scheduling_delay_windows", []))
    if phase_quality["status"] != "qualified" or saved_delay:
        reasons.append("Client scheduling delay overlaps the common measurement window.")
    load_settings = LoadTestSettings(profile=settings.get("profile", {}),
                                    pass_criteria={"min_valid_responses": criteria.min_samples})
    measurements, streams = [], {}
    length_samples = defaultdict(list)
    cache_reuse = cache_unknown = False
    for user in users:
        uid = user["user_id"]
        rows = records.get((window["id"], uid), [])
        work = [r for r in rows if _valid(r) and not r.intentional
                and start is not None and end is not None and r.dispatch_ms is not None
                and start <= r.dispatch_ms < end]
        samples = [r for r in work if r.completed_ms is not None and r.completed_ms <= end]
        notes = []
        if len(samples) < criteria.min_samples:
            notes.append(f"Only {len(samples)} valid in-window samples; {criteria.min_samples} required.")
        if user["outcomes"]["errors"] or user["outcomes"]["cancelled"] or user["outcomes"]["missing_terminal"]:
            notes.append("Failed or incomplete requests prevent an equal-work comparison.")
        if user["coverage"]["unadmitted_conversation_turn"]:
            notes.append("Unadmitted conversation turns prevent an equal-work comparison.")
        if user["coverage"]["missing_conversation_modes"]:
            notes.append("Incomplete conversation mode coverage prevents an equal-work comparison.")
        if any(r.input_tokens is None or r.output_tokens is None for r in work):
            notes.append("Input or output usage is missing.")
        uncached = [_uncached_input_tokens(r) for r in samples]
        cache_reuse |= any((r.cached_prompt_tokens or 0) > 0 for r in samples)
        cache_unknown |= any(r.cached_prompt_tokens is None for r in samples)
        if any(r.cached_prompt_tokens is not None and count is None for r, count in zip(samples, uncached)):
            notes.append("Reported cached input usage is invalid.")
        if any(r.finish_reason == "stop" and r.output_tokens is not None
               and (r.requested_output_tokens is not None or not r.workload.get("session_id"))
               and r.output_tokens < (r.requested_output_tokens if r.requested_output_tokens is not None
                                      else r.requested_max_tokens)
               for r in work):
            notes.append("An answer ended before its requested output shape was exercised.")
        qualification = None
        if "_" in user["profile"] and not user["profile"].startswith("conversation:"):
            qualification = _input_shape(observation, load_settings, user["profile"], samples)
            if not qualification["qualified"]:
                notes.append("Input shape not qualified: " + qualification["reason"])
        activated = user.get("activated_ms")
        exposure = max(0, end - max(start, activated)) if all(value is not None for value in (start, end, activated)) else None
        if exposure is None or exposure <= 0:
            notes.append("Active exposure is missing or unexercised.")
        if user["timing_quality"]["status"] != "qualified":
            notes.append("Client scheduling delay affects user timing.")
        streams[uid] = [r.model_copy(update={"stream_end_ms": r.last_output_ms}) for r in samples
                        if _has_streamed_answer(r)]
        lengths = {"input_median_tokens": [r.input_tokens for r in samples],
                   "output_median_tokens": [r.output_tokens for r in samples],
                   "uncached_input_median_tokens": uncached,
                   "total_generated_median_tokens": [_total_generated_tokens(r) for r in samples]}
        for field, values in lengths.items():
            length_samples[field].append(values)
        measurements.append({"user_id": uid, "reasons": notes, "valid_samples": len(samples),
                             **{field: median(values) for field, values in lengths.items()},
                             "active_exposure_ms": exposure, "input_qualification": qualification})
    overlap_users = []
    for measured in measurements:
        uid = measured["user_id"]
        peers = [record for other, values in streams.items() if other != uid for record in values]
        if max_evidenced_overlap(streams[uid], peers, observation.get("scheduling_delay_windows", [])) >= 2:
            overlap_users.append(uid)
        else:
            measured["reasons"].append("No qualified streamed answer overlap with another compared user.")
    comparable = [("input_median_tokens", "actual median input lengths"),
                  ("output_median_tokens", "actual median output lengths"),
                  ("total_generated_median_tokens", "actual median total generation lengths"),
                  ("active_exposure_ms", "active exposure")]
    if cache_reuse:
        if cache_unknown:
            reasons.append("Cached input usage is missing for some samples despite observed cache reuse.")
        comparable.append(("uncached_input_median_tokens", "actual median uncached input lengths"))
    for field, label in comparable:
        values = [item[field] for item in measurements if item[field] is not None]
        if values and max(values) > min(values) * (1 + tolerance / 100):
            reasons.append(f"Work not comparable: {label} differ by more than {tolerance:g}%.")
        elif field in length_samples and _length_mix_differs(length_samples[field], tolerance):
            reasons.append(f"Work not comparable: {label.replace('median ', '')} have different per-request mixes.")
    rates = [user["output_tokens_per_second"] for user in users]
    ratio = median(rates) / min(rates) if rates and all(value is not None and value > 0 for value in rates) else None
    for measured in measurements:
        measured["eligible"] = not measured["reasons"] and not reasons
    eligible = [item["user_id"] for item in measurements if item["eligible"]]
    if ratio is None:
        reasons.append("The median-to-worst throughput ratio is undefined because a user has zero or missing throughput.")
    reasons.extend(f"{item['user_id']}: {reason}" for item in measurements for reason in item["reasons"])
    qualified = not reasons
    status = ("measured" if limit is None else "inconclusive" if not qualified else
              "fail" if ratio > limit else "pass")
    if status == "fail":
        reasons.append(f"Observed delivery ratio {ratio:g} exceeds the selected maximum {limit:g}.")
    return {"status": status, "qualified": qualified, "threshold": limit, "tolerance_percent": tolerance,
            "ratio": ratio, "compared_user_ids": [user["user_id"] for user in users],
            "eligible_user_ids": eligible, "overlap_user_ids": overlap_users, "reasons": reasons,
            "users": measurements, "timing_quality": phase_quality, "interpretation": interpretation}


def _cancellation(window, records, criteria, health, lag, timing_threshold, delays):
    triggered = [r for r in records if r.workload.get("cancellation_role") == "selected"
                 and r.status == "cancelled" and r.intentional and r.termination_reason == "generation_chunk_limit"
                 and r.generation_chunks > 0 and r.finish_reason is None
                 and r.client_cancelled_ms is not None and r.client_cancelled_at_ns is not None
                 and 0 <= r.client_cancelled_ms <= r.elapsed_ms]
    cancelled = [r for r in triggered if r.timing_start_ms is not None
                 and qualify([r], health, timing_threshold, ("latency_ms",),
                             legacy_max_lag_ms=lag)["status"] == "qualified"]
    peer_ids = set()
    for record in cancelled:
        at = record.timing_start_ms + record.client_cancelled_ms
        for peer in records:
            span = answer_window(peer)
            if (peer.workload.get("user_id") != window["selected_user_id"] and span
                    and _has_streamed_answer(peer)
                    and _overlap(span, (span[0], at), delays) and _overlap(span, (at, span[1]), delays)
                    and qualify([peer], health, timing_threshold, ("latency_ms", "max_stream_gap_ms"),
                                legacy_max_lag_ms=lag)["status"] == "qualified"):
                peer_ids.add(peer.id)
    followups = [r for r in records if r.workload.get("cancellation_role") == "followup"
                 and r.workload.get("user_id") == window["selected_user_id"] and r.dispatch_ms is not None
                 and any(record.completed_ms is not None and r.dispatch_ms >= record.completed_ms
                         for record in triggered)]

    def observation(ok, ids, reason, *, failed=False):
        return {"status": "fail" if failed else "pass" if ok else "inconclusive",
                "request_ids": sorted(ids), "reasons": [] if ok and not failed else [reason]}

    cancellation = observation(bool(cancelled), [r.id for r in cancelled],
                               "The selected request did not reach an observed streaming cancellation.")
    peers = observation(bool(peer_ids), peer_ids, "No qualified peer answer output spans the cancellation.")
    followup = observation(sum(_valid(r) for r in followups) >= criteria.min_samples,
                           [r.id for r in followups], "Same-user follow-up is missing, failed or insufficiently sampled.",
                           failed=any(_error(r) for r in followups))
    coverage = "inconclusive" if window.get("cut_short") or window.get("drain_reason") else "pass"
    status = (_status([followup["status"], coverage])
              if cancellation["status"] == peers["status"] == "pass" else "inconclusive")
    return {"status": status,
            "selected_user_id": window["selected_user_id"], "cancellation": cancellation,
            "peer_continuation": peers, "followup": followup, "backend_release": "unverified"}


def _comparable_tokens(a, b, tolerance):
    return a is not None and b is not None and max(a, b) <= min(a, b) * (1 + tolerance)


def _comparable_uncached_input(control, probe, tolerance):
    a, b = control.cached_prompt_tokens, probe.cached_prompt_tokens
    if a is None or b is None:
        # An unreported count can coexist with no observed cache reuse, but
        # cannot establish equivalence to a request with known cached input.
        return not (a or b)
    if not (0 <= a <= control.input_tokens and 0 <= b <= probe.input_tokens):
        return False
    return _comparable_tokens(control.input_tokens - a, probe.input_tokens - b, tolerance)


def _interference(observation, window, control, user_records, criteria, health, lag, timing_threshold, delays):
    """Compare the same logical short work, retaining the exposure that exercised it."""
    from .load_test import input_class_bounds

    settings = observation.get("settings", {})
    tolerance = settings.get("fairness_tolerance_percent", 10) / 100
    limit = settings.get("noisy_neighbors", {}).get("max_slowdown")
    load_settings = LoadTestSettings(profile=settings.get("profile", {}),
                                    pass_criteria={"min_valid_responses": criteria.min_samples})
    heavy_input, heavy_output = window["heavy_profile"].split("_")
    min_uncached = None
    if heavy_input == "long":
        min_uncached, _ = input_class_bounds(load_settings, heavy_input,
                                             floors=_input_shape_floors(observation, heavy_output))
    trigger_time = itemgetter(0)
    heavy_by_peer = defaultdict(list)
    for heavy_id in window["heavy_user_ids"]:
        for competitor in user_records.get((window["id"], heavy_id), []):
            at = competitor.workload.get("trigger_ms")
            if not _valid(competitor) or competitor.output_tokens == 0 or at is None:
                continue
            for uid in competitor.workload.get("trigger_peer_ids", []):
                heavy_by_peer[uid].append((at, competitor))
    for candidates in heavy_by_peer.values():
        candidates.sort(key=trigger_time)
    reasons = []
    for phase in (control, window):
        if not phase or phase.get("cut_short") or phase.get("drain_reason"):
            reasons.append("Control or competing window has incomplete coverage.")
            continue
        a, b = phase.get("start_ms"), phase.get("end_ms")
        if a is None or b is None or b <= a:
            reasons.append("Control or competing window was not exercised.")
        elif (qualify([], health, timing_threshold, ("latency_ms", "max_stream_gap_ms"),
                      legacy_max_lag_ms=lag, phase_intervals=((a, b),))["status"] != "qualified"
              or any(a < end and start < b for start, end in delays.windows)):
            reasons.append("Client scheduling delay affects the comparison window.")
    measured = []
    for user in window["users"]:
        uid = user["user_id"]
        if uid in window["heavy_user_ids"]:
            continue
        notes = list(reasons)
        controls = {r.workload.get("matched_work_sequence"): r
                    for r in user_records.get((control["id"], uid), [])} if control else {}
        probes = user_records.get((window["id"], uid), [])
        candidates = heavy_by_peer.get(uid, [])
        pairs, exposed, probe_heavy_ids, excluded = [], {}, {}, set()
        for probe in probes:
            baseline = controls.get(probe.workload.get("matched_work_sequence"))
            if not baseline or not _valid(baseline) or not _valid(probe):
                continue
            if (not probe.workload.get("matched_work_sha256")
                    or baseline.workload.get("matched_work_sha256") != probe.workload["matched_work_sha256"]
                    or baseline.workload.get("think_time_seconds") != probe.workload.get("think_time_seconds")):
                notes.append("Matched logical work or pause plan differs.")
                continue
            if any(r.completed_ms is None or phase.get("end_ms") is None or r.completed_ms > phase["end_ms"]
                   for r, phase in ((baseline, control), (probe, window))):
                continue
            if not all(_comparable_tokens(a, b, tolerance) for a, b in
                       ((baseline.input_tokens, probe.input_tokens),
                        (baseline.output_tokens, probe.output_tokens))):
                excluded.add("Actual matched short-user token lengths are missing or not comparable.")
                continue
            a, b = baseline.completion_tokens, probe.completion_tokens
            if (a is not None or b is not None) and not _comparable_tokens(a, b, tolerance):
                excluded.add("Actual matched short-user completion tokens are missing or not comparable.")
                continue
            if not _comparable_uncached_input(baseline, probe, tolerance):
                excluded.add("Actual matched short-user uncached input is not comparable.")
                continue
            if not _has_streamed_answer(probe):
                continue
            span = answer_window(probe)
            if window["trigger"] == "before_output":
                first = _first_generation(probe)
                trigger_span = (probe.dispatch_ms, first) if probe.dispatch_ms is not None and first is not None else None
                short_span = ((probe.request_body_sent_ms, first)
                              if probe.request_body_sent_ms is not None and first is not None else None)
            else:
                second_answer = probe.second_answer_event_ms
                reads = probe.answer_read_timestamps_ms
                # The second finalized answer read is at or after the second
                # answer event. Keep the exact time when events share a read.
                if (second_answer is None and probe.output_events_truncated and span is not None
                        and len(reads) >= 2
                        and probe.first_output_ms <= reads[0] <= reads[1] < probe.last_output_ms):
                    second_answer = reads[1]
                if span is None or second_answer is None:
                    excluded.add("Second short-user answer event receipt is unavailable.")
                    continue
                second_answer += probe.timing_start_ms
                trigger_span = short_span = (max(span[0], second_answer), span[1])
            if trigger_span is None or short_span is None:
                continue
            lo = bisect_left(candidates, trigger_span[0], key=trigger_time)
            hi = bisect_left(candidates, trigger_span[1], lo, key=trigger_time)
            matched_heavy = []
            for _, competitor in candidates[lo:hi]:
                # Runner admission can precede a connection wait or upload.
                sent = competitor.request_body_sent_ms
                if sent is None or not short_span[0] <= sent < short_span[1]:
                    continue
                if heavy_output == "short":
                    last = _first_generation(competitor)
                    heavy_span = (sent, last) if last is not None else None
                else:
                    heavy_span = answer_window(competitor) if _has_streamed_answer(competitor) else None
                overlap_span = span if heavy_output == "long" else short_span
                if not _overlap(overlap_span, heavy_span, delays):
                    continue
                if min_uncached is not None and competitor.cached_prompt_tokens is not None:
                    total, cached = competitor.input_tokens, competitor.cached_prompt_tokens
                    if total is None or not 0 <= cached <= total or total - cached < min_uncached:
                        excluded.add("Reported cache usage does not establish long uncached competitor input.")
                        continue
                    if cached > total / 2:
                        excluded.add("More than half the long competitor input was cached.")
                        continue
                matched_heavy.append(competitor.id)
                exposed[competitor.id] = competitor
            if matched_heavy:
                pairs.append((baseline, probe))
                probe_heavy_ids[probe.id] = matched_heavy
        before, after = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
        competitors = list(exposed.values())
        if len(pairs) < criteria.min_samples:
            notes.append(f"Only {len(pairs)} matched samples with the selected trigger and heavy overlap; {criteria.min_samples} required.")
            notes.extend(sorted(excluded))
        if {r.workload.get("user_id") for r in competitors} != set(window["heavy_user_ids"]):
            notes.append("Not every selected heavy user has qualified overlap.")
            notes.extend(sorted(excluded))
        values = {}
        for label, rows, shape in (("control", before, "short_short"), ("probe", after, "short_short"),
                                   ("heavy", competitors, window["heavy_profile"])):
            if not _input_shape(observation, load_settings, shape, rows)["qualified"]:
                notes.append(f"Actual {label} input shape is unqualified.")
            if any(r.input_tokens is None or r.output_tokens is None for r in rows):
                notes.append(f"Actual {label} token usage is missing.")
            if shape.endswith("_long") and any(r.output_tokens is None
                    or r.output_tokens < max(r.requested_output_tokens or 0, load_settings.profile.long.output_tokens) * (1-tolerance)
                    for r in rows):
                notes.append("Heavy output ended before the requested long shape was exercised.")
            if shape.endswith("_short") and any(r.output_tokens is not None
                    and r.output_tokens > load_settings.profile.short.output_tokens * (1+tolerance) for r in rows):
                notes.append(f"Actual {label} output exceeds the short-output shape.")
            values[f"{label}_input_median_tokens"] = median([r.input_tokens for r in rows])
            values[f"{label}_output_median_tokens"] = median([r.output_tokens for r in rows])
            if qualify(rows, health, timing_threshold, ("latency_ms", "first_output_ms", "max_stream_gap_ms"),
                       legacy_max_lag_ms=lag)["status"] != "qualified":
                notes.append(f"Actual {label} timing is unqualified.")
        control_user = next((u for u in (control or {}).get("users", []) if u["user_id"] == uid), {})
        bounds = [(phase.get("start_ms"), phase.get("end_ms"), u.get("activated_ms"))
                  for phase, u in ((control or {}, control_user), (window, user))]
        exposure = [max(0, end-max(start, activated)) if all(value is not None for value in (start, end, activated)) else None
                    for start, end, activated in bounds]
        if any(value is None or value <= 0 for value in exposure) or max(exposure) > min(exposure)*(1+tolerance):
            notes.append("Short-user active exposure is not comparable.")
        control_completion = median([user_latency(r, False) for r in before])
        probe_completion = median([user_latency(r, False) for r in after])
        slowdown = probe_completion/control_completion if control_completion and probe_completion is not None else None
        if slowdown is None:
            notes.append("Matched completion slowdown is undefined.")
        qualified = not notes
        status = "inconclusive" if not qualified else "measured" if limit is None else "fail" if slowdown > limit else "pass"
        if status == "fail":
            notes.append(f"Observed slowdown {slowdown:g} exceeds {limit:g}.")
        def difference(a, b):
            return b-a if a is not None and b is not None else None
        measured.append({"user_id": uid, "status": status, "qualified": qualified, "reasons": list(dict.fromkeys(notes)),
            "matched_samples": len(pairs), "control_request_ids": [r.id for r in before],
            "probe_request_ids": [r.id for r in after], "heavy_request_ids": sorted(exposed),
            "probe_heavy_request_ids": probe_heavy_ids, "slowdown": slowdown, "first_answer_change_ms": difference(
                median([user_latency(r, True) for r in before]), median([user_latency(r, True) for r in after])),
            "completion_change_ms": difference(control_completion, probe_completion),
            "stream_gap_change_ms": difference(median([r.max_stream_gap_ms for r in before]), median([r.max_stream_gap_ms for r in after])),
            "output_tokens_change": difference(values["control_output_median_tokens"], values["probe_output_median_tokens"]), **values})
    statuses = [item["status"] for item in measured]
    return {"status": _status(statuses, measured=all(s == "measured" for s in statuses)),
            "qualified": bool(measured) and all(item["qualified"] for item in measured),
            "control_window_id": window["control_window_id"], "trigger": window["trigger"],
            "heavy_profile": window["heavy_profile"], "heavy_user_ids": window["heavy_user_ids"],
            "reasons": list(dict.fromkeys(reasons)), "users": measured}


def summarize(observation: dict[str, Any], records: list[RequestRecord], *,
              timing_health: dict | None = None, legacy_max_lag_ms: float = 0) -> dict[str, Any]:
    """Summarize the planned population without mutating its ledger or requests."""
    settings = observation.get("settings", {})
    criteria = MultiUserCriteria.model_validate(settings.get("pass_criteria", {}))
    modes = (settings.get("conversations") or {}).get("modes", ())
    timing_threshold = threshold(records)
    user_records = defaultdict(list)
    for record in records:
        window_id, user_id = record.workload.get("window_id"), record.workload.get("user_id")
        if isinstance(window_id, str) and isinstance(user_id, str):
            user_records[window_id, user_id].append(record)
    windows = []
    all_users = []
    for window in observation.get("windows", []):
        if window["phase"] != "measurement":
            windows.append({**window, "status": "warmup"})
            continue
        users = [_user_summary(user, window, user_records.get((window["id"], user["user_id"]), []),
                               criteria, timing_health, legacy_max_lag_ms, timing_threshold,
                               conversation_modes=modes if window["scenario"] == "repeated_conversations" else ())
                 for user in window["users"]]
        fairness = _fairness(observation, window, users, user_records, criteria, timing_health,
                             legacy_max_lag_ms, timing_threshold)
        statuses = [user["status"] for user in users]
        if (criteria.max_throughput_ratio is not None and not window.get("role")
                and fairness["status"] != "not_applicable"):
            statuses.append(fairness["status"])
        window_status = _status(statuses, measured=all(status == "measured" for status in statuses))
        windows.append({**window, "users": users, "status": window_status, "fairness": fairness})
        all_users.extend({**user, "window_id": window["id"], "population": window["population"],
                          "scenario": window["scenario"]} for user in users)
    scenario_delays = (_delays(observation.get("scheduling_delay_windows", []))
                       if any(window.get("role") in {"probe", "cancellation"} for window in windows) else None)
    for window in windows:
        if window["phase"] != "measurement":
            continue
        evidence = None
        if window.get("role") == "probe":
            control = next((item for item in windows if item["id"] == window["control_window_id"]), None)
            evidence = window["interference"] = _interference(observation, window, control, user_records, criteria,
                                                               timing_health, legacy_max_lag_ms, timing_threshold, scenario_delays)
        elif window.get("scenario") == "cancellation":
            evidence = window["cancellation"] = _cancellation(window,
                [r for r in records if r.workload.get("window_id") == window["id"]], criteria,
                timing_health, legacy_max_lag_ms, timing_threshold,
                scenario_delays)
        if evidence:
            window["status"] = _status([window["status"], evidence["status"]],
                                      measured=window["status"] == evidence["status"] == "measured")
    measurement_windows = [window for window in windows if window["phase"] == "measurement"]
    statuses = [window["status"] for window in measurement_windows]
    fairness_windows = [window for window in measurement_windows if not window.get("role")]
    if criteria.max_throughput_ratio is not None and all(
            window["fairness"]["status"] == "not_applicable" for window in fairness_windows):
        statuses.append("inconclusive")
    status = _status(statuses, measured=bool(all_users) and all(value == "measured" for value in statuses))
    ranked = sorted(all_users, key=lambda user: (
        {"fail": 0, "inconclusive": 1, "measured": 2, "pass": 2}[user["status"]],
        user["output_tokens_per_second"] if user["output_tokens_per_second"] is not None else -1,
        -(user["latency"]["ready_completion_ms"]["max"] or 0)))
    worst = ({key: ranked[0][key] for key in ("user_id", "population", "window_id", "profile",
              "status", "reasons", "output_tokens_per_second")} if ranked else None)
    return {"version": SUMMARY_VERSION, "window_policy": observation.get("window_policy"),
            "settings": observation.get("settings", {}), "population": observation.get("population", []),
            "windows": windows, "status": status, "worst_user": worst,
            "user_windows": len(all_users),
            "failed_user_windows": sum(user["status"] == "fail" for user in all_users),
            "inconclusive_user_windows": sum(user["status"] == "inconclusive" for user in all_users),
            "interpretation": "Completed valid output tokens delivered inside each common measurement window, "
                "divided by that window's actual duration. Late and partial output are separate. "
                "Latency distributions contain successful responses; timing attainment retains failed attempts. "
                "Ready-to-answer latency includes client admission wait. This is closed-loop delivery, not fixed-rate capacity."}
