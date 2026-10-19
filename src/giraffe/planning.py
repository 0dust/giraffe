"""Describe selected target work without traffic, credentials or local input imports."""

from __future__ import annotations

import math
from typing import Any

from giraffe.fixtures import builtin_fixtures
from giraffe.logprob_workload import corpus_width, sample_count
from giraffe.concurrent_workloads import SCENARIOS, execution_config
from giraffe.models import CHECK_NAMES, MAX_PLANNED_ARRIVALS, ConfigDraft, RunConfig
from giraffe.request_pack import active_pack, pack_summary
from giraffe.workloads import bucket_spec, fixed_session_turns, label_prompt, prefix_admitted
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


def request_plan(config: RunConfig | ConfigDraft, *,
                 pack_details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Count complete fixed work before hard caps; leave adaptive totals unknown.

    Rows account for shared controls only once. ``known`` includes only exact
    fixed portions, and evaluator calls share the dataset preview's count rules.
    The optional preview avoids rebuilding request-pack schedules in the API.
    """
    public_config = config
    config = execution_config(config)
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

    if "concurrency" in public_config.checks:
        names = public_config.concurrent_workloads.scenarios
        add("concurrency", 0, "Selected scenarios: " + "; ".join(SCENARIOS[name][1] for name in names)
            + ". The rows below count their separate requests and controls.")

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
        warnings.append("Scheduled requests use the run limit even when Giraffe cannot send them because all request slots are busy.")
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
                count = round(sum(traffic.rates) * traffic.duration_seconds)
                kind = "estimate"
            add("capacity", count,
                (f"About {count} requests are expected across {len(traffic.rates)} rates. " if kind == "estimate"
                 else f"Starts {count} requests across {len(traffic.rates)} rates. ")
                + "Requests may be dropped if all request slots are busy.",
                source="your dataset" if pack else "generated examples", kind=kind)
        if len(traffic.rates) * traffic.duration_seconds > config.max_duration_seconds:
            warnings.append("The selected rates need more time than the run limit allows, before connection and answer checks.")
        if traffic.arrival == "poisson" and not pack:
            warnings.append("Random arrival timing uses an expected request count. The saved seed may produce more or fewer requests.")

    if "fairness" in selected:
        count = config.effective_check("fairness")["samples"]
        fairness_needed = len(next(spec for spec in fixtures["generation"]
            if spec.fixture_id.startswith("fairness.copy")).expected.split()) + 12
        if config.stream and config.concurrency >= 2 and config.max_output_tokens >= fairness_needed:
            add("fairness_baseline", count, "Matched quiet streaming controls.", title="Fairness controls")
            add("fairness_short", count, "One short request per fairness trial.", title="Fairness short requests")
            add("fairness", None, f"Up to {count} long requests start only while a short response is streaming.",
                kind="bounded")
        else:
            add("fairness", 0, "Requires streaming, concurrency of at least two and enough output tokens; no fairness requests run.")

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
        count = max(options["samples"], normal) + cancelled + bool(cancelled) + options["samples"]
        add("cancellation", count, "Output/stop controls, a client cancellation, a deadline probe and follow-up health requests.")
        if options["backend_verification"] == "required":
            add("backend_cancellation", None,
                f"Runtime and local evidence availability determine whether up to {3 * options['samples']} additional verification requests run.",
                kind="bounded", title="Backend cancellation verification")
    if "recovery" in selected:
        options = config.effective_check("recovery")
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
    if selected & {"buckets", "mixed"}:
        settings = config.buckets
        for input_shape in ("short", "medium", "long"):
            for output_shape in ("short", "medium", "long"):
                request = bucket_spec(settings, input_shape, output_shape, "preview", "buckets", 1, config.context_limit)
                bucket_cases.append({"input_chars": settings.input_chars[input_shape],
                                     "actual_input_chars": request.input_chars,
                                     "output_tokens": request.max_tokens})
        if "buckets" in selected:
            bucket_levels = [{"concurrency": level,
                              "requests_per_case": sample_count(settings.samples_per_bucket, level),
                              "requests_per_target": 9 * sample_count(settings.samples_per_bucket, level)}
                             for level in settings.levels]
            add("buckets", sum(level["requests_per_target"] for level in bucket_levels),
                "Tries every question length with every answer cap at each selected load. Each combination sends enough requests to reach that load.",
                title="Question and answer size tests")
        if "mixed" in selected and config.max_output_tokens < 96:
            add("mixed", 0, "Requires an output cap of at least 96 tokens for the known light passage.")
            warnings.append("Mixed workload cannot qualify: max_output_tokens must be at least 96.")
        elif "mixed" in selected:
            add("mixed_control", settings.mixed_pairs, "Quiet short controls for mixed-load comparisons.",
                title="Mixed-load controls")
            if config.concurrency < 2:
                add("mixed", 0, "Requires concurrency of at least two; only quiet controls run.")
            else:
                pairs = sum(settings.heavy_weights.values()) * settings.mixed_pairs
                add("mixed", 4 * pairs,
                    "Two requests per pair in each of the before-output and active-stream trials; buffered light responses leave overlap unqualified.")
        if config.request_pack is not None:
            warnings.append("Size and mixed-load tests use generated questions. Your saved questions are kept for answer and rate tests.")
    if "token_stress" in selected:
        settings = config.concurrent_workloads
        for case in settings.stress_cases:
            required_context = (case.input_tokens or case.max_input_tokens or 128) + case.output_tokens
            context_limit = config.effective_check("concurrency")["context_limit"]
            title = (f"{case.input_tokens:,}-token input / {case.output_tokens:,}-token summary"
                     if case.input_tokens else f"Short input / {case.output_tokens:,}-token output")
            identifier = f"token_stress_{case.name}"
            if required_context > context_limit or case.output_tokens > config.max_output_tokens:
                add(identifier, 0, f"Needs context length {required_context:,} and output cap {case.output_tokens:,}. "
                    "Current limits prevent this case; no smaller substitute is sent.", title=title)
                warnings.append(f"{title}: configured limits prevent this case; length coverage will be inconclusive.")
                continue
            count = sum(max(settings.samples_per_bucket, level) for level in settings.levels)
            if case.input_tokens:
                add(identifier, None, f"Up to {settings.calibration_attempts} input-sizing requests, followed by "
                    f"up to {count} measured requests if calibration succeeds. Actual input and output usage must verify the targets.",
                    kind="bounded", title=title)
            else:
                add(identifier, count, f"{count} measured requests across concurrency levels {settings.levels}. "
                    "Early stopping leaves output-length coverage inconclusive.", title=title)
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
    if "buckets" in selected:
        sample_counts["buckets.samples_per_bucket"] = config.buckets.samples_per_bucket
    if "mixed" in selected:
        sample_counts["buckets.mixed_pairs"] = config.buckets.mixed_pairs
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
    if config.request_source == "request_pack" and not pack_selected and not selected & {"buckets", "mixed"}:
        warnings.append("Your dataset is inactive: select answer correctness or arrival capacity to use it.")
    if config.overlap_models and target_count > 1:
        warnings.append("Models run at the same time and share the maximum requests at a time. Their actual overlap depends on when request slots become free.")
    known = sum(row["requests_per_target"] for row in rows if row["kind"] == "exact")
    unknown = any(row["requests_per_target"] is None for row in rows)
    estimated = any(row["kind"] == "estimate" for row in rows)
    kind = "bounded" if unknown or any(row["kind"] == "bounded" for row in rows) else "estimate" if estimated else "exact"
    planned = None if unknown else sum(row["requests_per_target"] for row in rows)
    adaptive_estimates = {}
    if "context" in selected:
        adaptive_estimates["context"] = 1 + 2 * context_count
    if "fairness" in selected and any(row["id"] == "fairness_short" for row in rows):
        adaptive_estimates["fairness"] = config.effective_check("fairness")["samples"]
    if "recovery" in selected and config.effective_check("recovery")["sustained_seconds"] > 0:
        adaptive_estimates["sustained"] = max(config.limits.min_samples,
            config.effective_check("recovery")["concurrency"] * 2,
            max(quotas.values()) - known - sum(adaptive_estimates.values()))
    estimated_total = sum(row["requests_per_target"] or 0 for row in rows) + sum(adaptive_estimates.values())
    for target, quota in quotas.items():
        if estimated_total > quota and known <= quota:
            warnings.append(f"{target}: fixed work and conservative adaptive traffic need about {estimated_total} requests "
                            f"against a quota of {quota}; selected checks are likely to be starved.")
        if known > quota:
            warnings.append(f"{target} needs at least {known} requests, but its share of the run limit is {quota}. Some tests may be incomplete.")
        elif planned is not None and planned > quota:
            warnings.append(f"About {planned} requests are planned for {target}, but its share of the run limit is {quota}. Some tests may be incomplete.")
    return {"count_kind": kind, "planned_per_target": planned,
            "planned_total": planned * target_count if planned is not None else None,
            "conservative_requests_per_target": estimated_total, "adaptive_estimates": adaptive_estimates,
            "known_per_target": known, "known_total": known * target_count,
            "request_limit": config.max_requests,
            "required_request_limit": planned * target_count if kind == "exact" else None,
            "target_quotas": quotas, "rows": rows, "bucket_cases": bucket_cases,
            "bucket_levels": bucket_levels, "evaluator": evaluator, "warnings": warnings}
