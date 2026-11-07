"""Per-user delivery over explicit common windows, separate from generation pace."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .models import MultiUserCriteria, RequestRecord
from .stats import distribution
from .timing import qualify, threshold


SUMMARY_VERSION = 1


def _status(statuses: list[str], *, measured: bool = False) -> str:
    if "fail" in statuses:
        return "fail"
    if "inconclusive" in statuses or not statuses:
        return "inconclusive"
    return "measured" if measured else "pass"


def _valid(record: RequestRecord) -> bool:
    return record.status == "completed" and record.valid


def _error(record: RequestRecord) -> bool:
    return record.status in {"failed", "timeout"} or record.status == "completed" and not record.valid


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


def _user_summary(user: dict[str, Any], window: dict[str, Any], records: list[RequestRecord],
                  criteria: MultiUserCriteria, health: dict | None, lag: float,
                  timing_threshold: float) -> dict[str, Any]:
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
    eligible = [r for r in valid if r.dispatch_ms < end and r.completed_ms is not None and r.completed_ms <= end]
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
                           or cancelled or missing_terminal)
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
                         "invalid": sum(r.status == "completed" and not r.valid for r in attempts),
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
                         "required_samples": criteria.min_samples,
                         "missing_first_answer_count": sum(value is None for value in ready_first),
                         "missing_completion_count": sum(value is None for value in ready_completion),
                         "qualified_first_answer_count": sum(value is not None and r.id not in quality["affected_metrics"]["first_output_ms"]
                                                              for r, value in zip(valid, ready_first)),
                         "qualified_completion_count": sum(value is not None and r.id not in quality["affected_metrics"]["latency_ms"]
                                                            for r, value in zip(valid, ready_completion))}}


def summarize(observation: dict[str, Any], records: list[RequestRecord], *,
              timing_health: dict | None = None, legacy_max_lag_ms: float = 0) -> dict[str, Any]:
    """Summarize the planned population without mutating its ledger or requests."""
    criteria = MultiUserCriteria.model_validate(observation.get("settings", {}).get("pass_criteria", {}))
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
                               criteria, timing_health, legacy_max_lag_ms, timing_threshold)
                 for user in window["users"]]
        window_status = _status([user["status"] for user in users],
                                measured=all(user["status"] == "measured" for user in users))
        windows.append({**window, "users": users, "status": window_status})
        all_users.extend({**user, "window_id": window["id"], "population": window["population"],
                          "scenario": window["scenario"]} for user in users)
    status = _status([window["status"] for window in windows if window["phase"] == "measurement"],
                     measured=bool(all_users) and all(user["status"] == "measured" for user in all_users))
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
