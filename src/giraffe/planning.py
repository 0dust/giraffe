"""Describe selected target work without traffic, credentials or local input imports."""

from __future__ import annotations

import math
from typing import Any

from giraffe.fixtures import builtin_fixtures
from giraffe.logprob_workload import corpus_width, sample_count
from giraffe.concurrent_load import context_window, calibration_keys, load_time_plan, minimum_reservation, running_shapes, test_points, later_reservation, prompt_overhead
from giraffe.models import CHECK_NAMES, CHECK_OPTION_FIELDS, CORE_CHECKS, MAX_PLANNED_ARRIVALS, ConfigDraft, RunConfig, load_shape_label
from giraffe.request_pack import active_pack, pack_summary
from giraffe.workloads import fixed_session_turns, label_prompt, prefix_admitted
from giraffe.tool_checks import resolved_tool_fixtures, resolved_tool_modes


def _steady_count(rate: float, duration: float) -> int:
    # Match arrival_schedule's n / rate < duration test, including float edges,
    # without allocating every arrival or assigning its workload class.
    count = math.ceil(rate * duration)
    while count > 0 and (count - 1) / rate >= duration:
        count -= 1
    while count / rate < duration:
        count += 1
    return min(count, MAX_PLANNED_ARRIVALS)


def _time_plan(config: RunConfig | ConfigDraft, rows: list[dict[str, Any]],
               bucket_levels: list[dict[str, Any]]) -> dict[str, Any]:
    """Estimate target traffic before caps, with one second/request as a heuristic.

    Sum sequential phases and targets for a conservative bound even when targets
    overlap. Request calls do not retry; readiness polling has its own deadline.
    Configured load windows set minimum sending time; active requests can still drain.
    These are network/workload budgets, not bounds on Python or report I/O time.
    """
    typical = worst = minimum = 0.0
    reasons = []
    selected = set(config.checks)
    timeout = config.request_timeout_seconds
    for row in rows:
        name, count = row["id"], row["requests_per_target"]
        if count == 0 and name != "capacity":
            continue
        check = {"initial": "access", "pack_loaded": "correctness",
                 "context_first_batch": "context", "recovery_prime": "recovery",
                 "recovery_baseline": "recovery", "limits.stop_control": "cancellation",
                 "backend_cancellation": "cancellation"}.get(name, name)
        options = config.effective_check(check) if check in CHECK_OPTION_FIELDS else {}
        wait = options.get("request_timeout_seconds", timeout)
        concurrency = 1
        if name in {"correctness", "json", "pack_loaded"}:
            concurrency = options.get("concurrency", config.concurrency)
        if name == "initial" and "access" not in selected:
            wait = timeout
        if name == "capacity":
            traffic = config.effective_traffic()
            windows = len(traffic.rates) * traffic.duration_seconds
            minimum += windows
            typical += windows + len(traffic.rates) * min(1, timeout, traffic.drain_timeout_seconds)
            worst += windows + len(traffic.rates) * min(timeout, traffic.drain_timeout_seconds)
            continue
        if name.startswith("load_") and name != "load_calibration":
            detail = load_time_plan(config.concurrent_load)[name.removeprefix("load_")]
            windows = detail["configured_load_seconds"]
            minimum += windows
            typical += windows + detail["test_points"] * min(1, wait)
            worst += windows + detail["test_points"] * wait
            continue
        if name == "restart_readiness":
            typical += min(1, config.readiness_timeout_seconds)
            worst += config.readiness_timeout_seconds + timeout
            continue
        if name == "sustained":
            seconds = config.effective_check("recovery")["sustained_seconds"]
            minimum += seconds
            typical += seconds + min(1, timeout)
            worst += seconds + timeout
            continue
        if name == "arrivals":
            settings = config.arrivals
            last = ((settings.requests - 1) / settings.requests_per_second if settings.mode == "steady"
                    else ((settings.requests - 1) // settings.burst_size) * settings.interval_seconds)
            minimum += last
            typical += last + min(1, wait)
            worst += last + count * wait  # Conservatively include slot waiting/drain.
            continue
        if name == "consistency":
            settings = config.consistency
            levels = set([1, *settings.levels])
            if settings.logprobs:
                batches = len(settings.shapes) * (settings.repetitions + settings.logprobs.priming_rounds) * sum(
                    math.ceil(corpus_width(settings) / min(level, config.concurrency)) for level in levels)
            else:
                batches = len(settings.shapes) * settings.prompts_per_shape * sum(
                    math.ceil(sample_count(settings.repetitions, level) / min(level, config.concurrency)) for level in levels)
            typical += batches * min(1, wait)
            worst += count * wait
            continue
        if name == "sessions":
            settings = config.sessions
            turns = max(len(workload.turns) for workload in settings.workloads) if settings.profile == "performance" else len(settings.turns)
            modes = 1 if settings.profile == "performance" else len(settings.modes)
            waves = math.ceil(settings.sessions / min(settings.concurrency, config.concurrency))
            typical += modes * waves * (turns * min(1, wait) + (turns - 1) * settings.delay_seconds)
            # A serial bound also covers unequal-length conversations and shared slots.
            requests = count if count is not None else settings.sessions * turns * modes
            worst += requests * (wait + settings.delay_seconds)
            continue
        if count is None:
            if name == "context":
                count = 1 + 2 * next(row["requests_per_target"] for row in rows if row["id"] == "context_first_batch")
            elif name == "backend_cancellation":
                count = 3 * config.effective_check("cancellation")["samples"]
            else:
                reasons.append(f"{row['title']}: the full workload size is unknown before local inputs or target responses are available.")
                continue
        # Serial is intentionally conservative for compound scenarios whose
        # requests overlap only when the server exhibits the required behavior.
        batches = math.ceil(count / min(concurrency, config.concurrency))
        typical += batches * min(1, wait)
        worst += batches * wait
    if "cancellation" in selected:
        options = config.effective_check("cancellation")
        worst += min(5, options["cleanup_seconds"])
        if options["backend_verification"] == "required":
            worst += options["samples"] * options["trace_wait_seconds"]
    if "gpu" in selected:
        typical = max(typical, config.gpu_min_observation_seconds)
        worst = max(worst, config.gpu_min_observation_seconds) + 2 * config.metrics_timeout_seconds
    targets = len(config.targets)
    # Overlapping targets still share request slots. Never reduce the upper bound.
    estimate = max(typical, typical * targets / config.concurrency) if config.overlap_models else typical * targets
    minimum *= 1 if config.overlap_models else targets
    worst *= targets
    errors, warnings = [], []
    if minimum > config.max_duration_seconds:
        fields = (["concurrent_load.schedule.duration_seconds", "max_duration_seconds"]
                  if "concurrency" in selected else ["max_duration_seconds"])
        if "capacity" in selected:
            fields += ["traffic.duration_seconds", "traffic.rates"]
        if "arrivals" in selected:
            fields += ["arrivals.requests", "arrivals.requests_per_second" if config.arrivals.mode == "steady" else "arrivals.interval_seconds"]
        if "recovery" in selected:
            fields += ["test_options.recovery.sustained_seconds"]
        values = {"minimum_seconds": minimum, "max_duration_seconds": config.max_duration_seconds}
        if "capacity" in selected:
            values.update({"traffic.duration_seconds": config.effective_traffic().duration_seconds,
                           "traffic.rates": config.effective_traffic().rates})
        if "arrivals" in selected:
            values.update({field: getattr(config.arrivals, field.split(".")[-1])
                           for field in fields if field.startswith("arrivals.")})
        if "recovery" in selected:
            values["test_options.recovery.sustained_seconds"] = config.effective_check("recovery")["sustained_seconds"]
        errors.append({"field": fields[0], "fields": fields, "loc": ["body", fields[0]],
                       "values": values,
                       "msg": f"Fixed sending windows need at least {minimum:g} seconds, exceeding Traffic time limit {config.max_duration_seconds:g} seconds."})
    elif not reasons and worst > config.max_duration_seconds:
        warnings.append({"fields": ["max_duration_seconds", "request_timeout_seconds"],
                         "msg": "Request timeouts may exceed the Traffic time limit and stop target traffic with checks unfinished. Shorter per-check timeouts also apply."})
    return {"estimated_seconds": None if reasons else round(estimate, 3),
            "worst_case_seconds": None if reasons else round(worst, 3),
            "minimum_seconds": minimum, "time_bound_reasons": reasons,
            "time_estimate_assumptions": ["Assumes one second per request (or its shorter timeout); no endpoint measurements are available. Target traffic only, excluding AI evaluation and report saving."],
            "errors": errors, "time_warnings": warnings}


def request_plan(config: RunConfig | ConfigDraft, *,
                 pack_details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Count complete fixed work before hard caps; leave adaptive totals unknown.

    Rows account for shared controls only once. ``known`` includes only exact
    fixed portions, and evaluator calls share the dataset preview's count rules.
    The optional preview avoids rebuilding request-pack schedules in the API.
    """
    public_config = config
    selected = set(config.checks)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    target_count = len(config.targets)
    base, extra = divmod(config.max_requests, target_count)
    quotas = {target.name: base + (index < extra)
              for index, target in enumerate(config.targets)}

    def add(identifier: str, count: int | None, summary: str, *,
            source: str = "generated examples", kind: str = "exact", title: str | None = None):
        rows.append({"id": identifier, "title": title or CHECK_NAMES.get(identifier, identifier),
                     "requests_per_target": count, "kind": kind, "source": source,
                     "summary": summary})

    # Only fixture cardinalities are needed. Keep context filler small even if
    # the operator has configured a very large context limit.
    fixtures = builtin_fixtures(config.model_copy(update={"context_limit": 128}))
    initial = bool(selected & {"access", "first_output"})
    if initial:
        add("initial", 1, "One generated request checks the connection. Time to first text uses this same request.",
            title="Connection check")
    if config.restart_target:
        add("restart_readiness", None,
            f"GET readiness polls for {config.restart_target}, then one inference probe outside the request budget.",
            kind="bounded", title="Restart readiness")
    warm_count = config.samples if selected & {"serving", "first_output"} else 0
    if warm_count:
        add("warm", warm_count, "Repeated connection requests reused to compare response timing and mixed-length requests.",
            title="Repeated connection checks")
    for check in ("access", "serving", "first_output"):
        if check in selected:
            add(check, 0, "Reuses connection checks and the other selected tests; adds no requests.")

    if "generation" in selected:
        count = config.effective_check("generation")["samples"]
        add("generation", count, f"{count} copies of the generated passage.")

    pack_selected = config.request_source == "request_pack" and bool(selected & {"capacity", "correctness"})
    pack = active_pack(config)
    details = pack_details if pack_details is not None else pack_summary(config) if pack else None
    if pack_selected:
        count = len(config.request_pack.cases) if pack else None
        add("pack_control", count, "Sends each of your questions once. The answer and arrival checks reuse these results."
            if pack else "Add your questions to calculate their request count.",
            source="your dataset", kind="exact" if pack else "bounded", title="Your questions (one each)")
        if not pack:
            warnings.append("The selected dataset is empty; request counts and run readiness are incomplete.")

    if pack_selected and "capacity" not in selected and pack:
        count = max(len(config.request_pack.cases), config.effective_check("correctness")["concurrency"])
        add("pack_loaded", count, "Concurrent answer pass over your questions.", source="your dataset")

    if "capacity" in selected:
        traffic = config.effective_traffic()
        limits = config.effective_check("capacity")["limits"]
        if limits["first_output_ms"] is None and limits["latency_ms"] is None:
            warnings.append("Set a maximum answer time or time to first text for a pass/fail result under load. Without a time target, Giraffe reports measurements only.")
        if pack_selected and not pack:
            add("capacity", None, "Add your questions to calculate the arrival request count.",
                source="your dataset", kind="bounded")
        else:
            if pack:
                count = sum(stage["planned"] for stage in details["stages"])
                kind = "exact"
            elif traffic.arrival == "steady":
                count = sum(_steady_count(rate, traffic.duration_seconds) for rate in traffic.rates)
                kind = "exact"
            else:
                count = sum(min(round(rate * traffic.duration_seconds), MAX_PLANNED_ARRIVALS)
                            for rate in traffic.rates)
                kind = "exact"
            add("capacity", count,
                f"Starts {count} requests across {len(traffic.rates)} rates. "
                + "Scheduled requests use the run limit even when Giraffe cannot send them because all request slots are busy.",
                source="your dataset" if pack else "generated examples", kind=kind)
        if len(traffic.rates) * traffic.duration_seconds > config.max_duration_seconds:
            warnings.append("The selected rates need more time than the run limit allows, before connection and answer checks.")

    context_count = 0
    if "context" in selected:
        context_options = config.effective_check("context")
        context_shapes = builtin_fixtures(config.model_copy(update={"context_limit": min(512, context_options["context_limit"])}))["context"]
        context_count = count = max(context_options["samples"], len(context_shapes))
        add("context_first_batch", count, "The first generated context batch.", title="Initial context cases")
        add("context", None,
            f"Observed input usage may require a calibration request and up to two further batches of {count} requests.",
            kind="bounded")

    if "correctness" in selected:
        if pack_selected:
            add("correctness", 0, "Uses each question's first result; adds no requests when arrival traffic is also selected.",
                source="your dataset")
        elif config.custom_fixtures:
            add("correctness", None,
                "Built-in questions plus a local custom-fixture file; its case count is unknown without importing the file.",
                source="generated examples and your local fixture file", kind="bounded")
        else:
            count = max(config.effective_check("correctness")["samples"], len(fixtures["correctness"]))
            add("correctness", count, f"{count} requests using {len(fixtures['correctness'])} generated questions with known answers.")
    if "json" in selected:
        count = max(config.effective_check("json")["samples"], len(fixtures["json"]))
        add("json", count, f"{count} generated structured-answer requests.")

    if "cancellation" in selected:
        options = config.effective_check("cancellation")
        normal = sum(spec.cancel_after_ms is None and not spec.fixture_id.startswith("limits.stop_control")
                     for spec in fixtures["limits"])
        cancelled = sum(spec.cancel_after_ms is not None for spec in fixtures["limits"])
        add("limits.stop_control", 1, "One full-output control for stop-sequence enforcement.")
        count = max(options["samples"], normal) + cancelled + 2 * bool(cancelled) + options["samples"]
        add("cancellation", count, "Output/stop controls, a pre-cancellation baseline, a client cancellation, a client deadline self-test and follow-up health requests.")
        if options["backend_verification"] == "required":
            add("backend_cancellation", None,
                f"Runtime and local evidence availability determine whether up to {3 * options['samples']} additional verification requests run.",
                kind="bounded", title="Backend cancellation verification")
    if "recovery" in selected:
        options = config.effective_check("recovery")
        later = [name for name in config.checks if name not in CORE_CHECKS or name == "concurrency"]
        later_known = request_plan(config.model_copy(update={"checks": later, "restart_target": None}))["minimum_reserved_per_target"]
        prior = sum(row["requests_per_target"] or 0 for row in rows)
        needed = later_known + 2 * options["samples"] + max(options["limits"]["min_samples"], options["concurrency"] * 2)
        eligible = [quota - prior > needed for quota in quotas.values()]
        uncertain = any(row["kind"] != "exact" for row in rows) or len(set(eligible)) > 1
        add("recovery_prime", int(any(eligible)), "One baseline warm-up when remaining target/run budget exceeds later fixed work, both probe batches and the minimum load batch.",
            kind="bounded" if any(eligible) and uncertain else "exact", title="Recovery baseline warm-up")
        add("recovery_baseline", options["samples"], "Matched quiet controls before sustained load.")
        add("recovery", options["samples"], "Fixed follow-up health requests after sustained load.")
        if options["sustained_seconds"] > 0:
            add("sustained", None,
                f"Response pace determines the number of load batches during {options['sustained_seconds']:g} seconds.",
                kind="bounded", title="Sustained load")
    if "gpu" in selected:
        add("gpu", 0, "Observes metrics without adding inference requests.", source="configured metrics endpoint")

    if "arrivals" in selected:
        add("arrivals", config.arrivals.requests,
            f"{config.arrivals.requests} scheduled offers; the pending queue and run limits can leave some undispatched.")
    if "prefix" in selected:
        settings = config.prefix
        # Include the 32-character nonce, newline, filler/instruction and trial suffix.
        prefix_chars = len(label_prompt(settings.prefix_chars + 33 + 80)) + len("\nTrial 0000.")
        if not prefix_admitted(prefix_chars, 24, config.context_limit):
            add("prefix", 0, "Estimated prefix input plus output/template reserves exceeds context_limit; no prefix requests are admitted.")
            warnings.append("Prefix coverage will be unfinished: estimated token admission rejects the configured prefix.")
        else:
            prompt_chars = len(label_prompt(96))
            history = sum(prefix_admitted(prompt_chars * (turn + 1) + 4 * turn, 24, config.context_limit)
                          for turn in range(settings.history_turns))
            add("prefix", 2 * settings.repeats + history,
                f"{2 * settings.repeats} shared/independent prefix controls and {history} fixed-history requests admitted by the token estimate.")

    bucket_cases, bucket_levels = [], []
    load_plan = load_time_plan(config.concurrent_load) if "concurrency" in selected else {}
    load_warnings = []
    if load_plan:
        settings = config.concurrent_load
        points = test_points(settings)
        for kind, detail in load_plan.items():
            levels = f"{len(settings.schedule.levels)} {'level' if len(settings.schedule.levels) == 1 else 'levels'}"
            workloads = f"{detail['workloads']} {'workload' if detail['workloads'] == 1 else 'workloads'}"
            breakdown = (f"1 workload (alternating short/long probe) × {levels} × {2 * settings.schedule.duration_seconds} s"
                         if kind == "interference" else f"{workloads} × {levels} × {settings.schedule.duration_seconds} s")
            add(f"load_{kind}", None, breakdown + ". " +
                "Duration-based points send as many requests as the endpoint completes; slots refill individually.",
                title=detail["title"], kind="duration")
        add("load_calibration", len(calibration_keys(settings)) * settings.calibration_attempts,
            "At most this many token-calibration requests; calibration is shared per input size and output-instruction variant.",
            title="Input token calibration", kind="bounded")
        demand = sum(p["level"] * p["configured_window_seconds"] for p in points)
        preceding_known = sum(row["requests_per_target"] or 0 for row in rows if not row["id"].startswith("load_"))
        expected = max(0, min(quotas.values()) - preceding_known - later_reservation(config) - len(calibration_keys(settings)) * settings.calibration_attempts)
        if demand > expected:
            message = f"Concurrent load may need about {demand:g} requests at an assumed 1 s/request, above the expected allowance {expected:g}. Raise the total request limit or reduce load time."
            warnings.append(message)
            load_warnings.append({"fields": ["max_requests"], "msg": message})
        for size, variant in calibration_keys(settings):
            target = getattr(settings.profile, size).input_tokens
            overhead = prompt_overhead(variant)
            if target < overhead:
                message = f"{size.capitalize()} input target {target} is below the estimated minimum prompt length (≈{overhead} tokens); calibration measures the actual minimum."
                warnings.append(message)
                load_warnings.append({"fields": [f"concurrent_load.profile.{size}.input_tokens"], "msg": message})
        for shape in running_shapes(settings):
            a, b = shape.split("_")
            if settings.schedule.duration_seconds < 3 * getattr(settings.profile, b).output_tokens / 30:
                message = f"{load_shape_label(shape)}: window shorter than expected responses; throughput may be unreliable (output cap ÷ 30 tok/s)."
                warnings.append(message)
                load_warnings.append({"fields": ["concurrent_load.schedule.duration_seconds"], "msg": message})
            if "context_limit" in config.model_fields_set and any(context_window(t)[0] is None for t in config.targets) and getattr(settings.profile, a).input_tokens + getattr(settings.profile, b).output_tokens > config.context_limit:
                message = f"{load_shape_label(shape)}: input target + output cap exceeds Context window ({config.context_limit} tokens); server context window is unverified."
                warnings.append(message)
                load_warnings.append({"fields": [f"concurrent_load.profile.{a}.input_tokens", "context_limit"], "msg": message})
    if "sessions" in selected:
        settings = config.sessions
        if settings.profile == "performance":
            maximum = settings.sessions * max(len(workload.turns) for workload in settings.workloads)
            add("sessions", None,
                f"Up to {maximum} requests; prior responses and history admission determine whether later turns run.",
                source="configured performance conversations and live history", kind="bounded")
        else:
            fixed = sum(fixed_session_turns(settings, index, config.context_limit)
                        for index in range(settings.sessions)) if "fixed" in settings.modes else 0
            if "live" in settings.modes:
                add("sessions", None,
                    f"{fixed} fixed-history requests and up to {settings.sessions * len(settings.turns)} live requests; actual final-answer lengths determine live history admission.",
                    source="configured session prompts and saved/live history", kind="bounded")
            else:
                add("sessions", fixed,
                    "Fixed seeded histories with exact character admission; failed responses or shared budgets can stop sessions early.",
                    source="configured session prompts and seeded fixed history")
    if "consistency" in selected:
        settings = config.consistency
        levels = len(set([1, *settings.levels]))
        count = len(settings.shapes) * settings.prompts_per_shape * sum(
            sample_count(settings.repetitions, level) for level in set([1, *settings.levels]))
        if settings.logprobs:
            count = len(settings.shapes) * levels * corpus_width(settings) * (settings.repetitions + settings.logprobs.priming_rounds)
            add("consistency", count, f"{settings.repetitions} measured rounds plus {settings.logprobs.priming_rounds} priming rounds per shape and level; {corpus_width(settings)} fixed corpus requests per round. Complete responses with logprobs enabled; client overlap does not prove server batching.")
        else:
            add("consistency", count, f"At least {settings.repetitions} repeats of {settings.prompts_per_shape} distinct prompts per shape and level, rounded up to full load groups, including sequential controls once.")
    if "tools" in selected:
        fixtures = resolved_tool_fixtures(config)
        count = (len(fixtures) + sum(fixture.choice is None for fixture in fixtures)
                 * config.forced_tool_diagnostic) * len(resolved_tool_modes(config))
        conversations = config.tool_checks.conversations if config.tool_checks else []
        count += sum(len(fixture.steps) + 1 for fixture in conversations) * len(resolved_tool_modes(config))
        add("tools", count, "Tool fixtures in each selected mode. Conversation counts are upper bounds: expected calls plus one final answer; invalid or repeated calls stop a conversation early. No tools execute.",
            source="configured tool fixtures" if config.tool_checks and (config.tool_checks.fixtures or conversations) else "generated examples",
            kind="bounded" if conversations else "exact")

    sample_counts = {}
    if "arrivals" in selected:
        sample_counts["arrivals.requests"] = config.arrivals.requests
    if "prefix" in selected:
        sample_counts["prefix.repeats (excluding first use)"] = config.prefix.repeats - 1
    if "consistency" in selected:
        if config.consistency.logprobs:
            sample_counts["consistency.logprob measured samples"] = config.consistency.repetitions * corpus_width(config.consistency)
        else:
            sample_counts["consistency.repetitions"] = config.consistency.repetitions
    if "sessions" in selected:
        sample_counts["sessions.sessions"] = config.sessions.sessions
    for name, count in sample_counts.items():
        if count < config.limits.min_samples:
            warnings.append(f"{name}={count} is below min_samples={config.limits.min_samples}; this check cannot qualify.")

    judging = details["judging"] if details else None
    evaluator = {"active": bool(judging and judging["active"]),
                 "call_upper_bound": judging["planned_call_upper_bound"] if judging else 0,
                 "judge_count": judging["judge_count"] if judging else 0,
                 "ready": judging["ready"] if judging else True,
                 "readiness_errors": judging["readiness_errors"] if judging else []}
    if evaluator["active"]:
        warnings.append("AI judge calls run separately from the target request budget and send cases and generated answers to each judge.")
        warnings.extend(evaluator["readiness_errors"])
    if config.request_source == "request_pack" and not pack_selected:
        warnings.append("Your dataset is inactive: select answer correctness or arrival capacity to use it.")
    if config.overlap_models and target_count > 1:
        warnings.append("Models run at the same time and share the maximum requests at a time. Their actual overlap depends on when request slots become free.")
    known = sum(row["requests_per_target"] or 0 for row in rows if row["kind"] == "exact")
    unknown = any(row["requests_per_target"] is None for row in rows)
    estimated = any(row["kind"] == "estimate" for row in rows)
    kind = "bounded" if unknown or any(row["kind"] == "bounded" for row in rows) else "estimate" if estimated else "exact"
    planned = None if unknown else sum(row["requests_per_target"] for row in rows)
    counted = sum(row["requests_per_target"] or 0 for row in rows)
    adaptive_estimates = {}
    if load_plan:
        adaptive_estimates["concurrency"] = sum(p["level"] * p["configured_window_seconds"] for p in test_points(config.concurrent_load))
    if "context" in selected:
        adaptive_estimates["context"] = 1 + 2 * context_count
    if "recovery" in selected and config.effective_check("recovery")["sustained_seconds"] > 0:
        adaptive_estimates["sustained"] = max(config.limits.min_samples,
            config.effective_check("recovery")["concurrency"] * 2,
            max(quotas.values()) - counted - sum(adaptive_estimates.values()))
    estimated_total = counted + sum(adaptive_estimates.values())
    for target, quota in quotas.items():
        if estimated_total > quota and known <= quota:
            warnings.append(f"{target}: fixed work and conservative adaptive traffic need about {estimated_total} requests "
                            f"against a quota of {quota}; selected checks are likely to be starved.")
        if known > quota:
            warnings.append(f"{target} needs at least {known} requests, but its share of the run limit is {quota}. Some tests may be incomplete.")
        elif planned is not None and planned > quota:
            warnings.append(f"About {planned} requests are planned for {target}, but its share of the run limit is {quota}. Some tests may be incomplete.")
    return {**_time_plan(config, rows, bucket_levels), "count_kind": kind, "planned_per_target": planned,
            "planned_total": planned * target_count if planned is not None else None,
            "conservative_requests_per_target": estimated_total, "adaptive_estimates": adaptive_estimates,
            "concurrent_load": load_plan,
            "configured_load_seconds": sum(v["configured_load_seconds"] for v in load_plan.values()),
            "load_warnings": load_warnings,
            "minimum_reserved_per_target": known + (minimum_reservation(config.concurrent_load) if load_plan else 0),
            "known_per_target": known, "known_total": known * target_count,
            "request_limit": config.max_requests, "memory_limit_bytes": config.max_memory_mb * 1024**2,
            "memory_limit_label": "Memory limit (RAM growth during this run)",
            "required_request_limit": planned * target_count if kind == "exact" else None,
            "target_quotas": quotas, "rows": rows, "bucket_cases": bucket_cases,
            "bucket_levels": bucket_levels, "evaluator": evaluator, "warnings": warnings}
