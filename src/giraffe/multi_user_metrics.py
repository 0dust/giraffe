"""Per-user delivery over explicit common windows, separate from generation pace."""
from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

from .models import LoadTestSettings, MultiUserCriteria, RequestRecord
from .overlap import max_evidenced_overlap
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


def _first(record: RequestRecord) -> float | None:
    start = record.timing_start_ms if record.timing_start_ms is not None else record.dispatch_ms
    return start + record.first_output_ms if start is not None and record.first_output_ms is not None else None


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


def _fairness(observation, window, users, records, criteria, health, lag, timing_threshold):
    """Qualify observed delivery, never attribute a difference to server scheduling."""
    from .load_test import input_qualification

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
            size, output = user["profile"].split("_")
            variant = "short_answer" if output == "short" else "long_output"
            floors = {item["size"]: item["input_tokens"] for item in observation.get("calibrations", [])
                      if item.get("variant") == variant and item.get("status") == "calibrated"
                      and item.get("reason_code") == "minimum_prompt_length"
                      and item.get("token_count_scope") != "builtin_reference" and item.get("input_tokens") is not None}
            qualification = input_qualification(load_settings, size, samples, floors=floors)
            if not qualification["qualified"]:
                notes.append("Input shape not qualified: " + qualification["reason"])
        activated = user.get("activated_ms")
        exposure = max(0, end - max(start, activated)) if all(value is not None for value in (start, end, activated)) else None
        if exposure is None or exposure <= 0:
            notes.append("Active exposure is missing or unexercised.")
        if user["timing_quality"]["status"] != "qualified":
            notes.append("Client scheduling delay affects user timing.")
        streams[uid] = [r.model_copy(update={"stream_end_ms": r.last_output_ms}) for r in samples
                        if r.stream and r.answer_reads >= 2 and r.answer_chunks >= 2]
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
        if criteria.max_throughput_ratio is not None and fairness["status"] != "not_applicable":
            statuses.append(fairness["status"])
        window_status = _status(statuses, measured=all(status == "measured" for status in statuses))
        windows.append({**window, "users": users, "status": window_status, "fairness": fairness})
        all_users.extend({**user, "window_id": window["id"], "population": window["population"],
                          "scenario": window["scenario"]} for user in users)
    measurement_windows = [window for window in windows if window["phase"] == "measurement"]
    statuses = [window["status"] for window in measurement_windows]
    if criteria.max_throughput_ratio is not None and all(
            window["fairness"]["status"] == "not_applicable" for window in measurement_windows):
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
