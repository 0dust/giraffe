"""Map request scenarios to the configured tests users recognize.

Condition IDs are local to a target. Request indices, session IDs, and the two
members of an interference pair stay request details, rather than extra tests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_SCENARIOS = {
    "initial": ("First request", "access"),
    "readiness": ("Readiness", "access"),
    "warm": ("Warm-up", "serving"),
    "short": ("Short requests", "serving"),
    "long": ("Long requests", "serving"),
    "generation": ("Generation speed", "generation"),
    "context": ("Long-context recall", "context"),
    "correctness": ("Answer correctness", "correctness"),
    "json": ("Structured JSON", "json"),
    "sustained": ("Sustained load", "recovery"),
    "recovery": ("Recovery check", "recovery"),
    "recovery_baseline": ("Recovery baseline", "recovery"),
    "recovery_prime": ("Recovery preparation", "recovery"),
    "limits": ("Output limits", "cancellation"),
    "stop_control": ("Stop-sequence baseline", "cancellation"),
    "client_cancel": ("Client cancellation", "cancellation"),
    "client_deadline": ("Client deadline", "cancellation"),
    "backend_cancel": ("Server cancellation", "cancellation"),
    "backend_cancel_control": ("Server cancellation baseline", "cancellation"),
    "backend_cancel_followup": ("Server cancellation follow-up", "cancellation"),
    "cancellation_probe": ("Cancellation probes", "cancellation"),
    "cancellation_baseline": ("Cancellation baseline", "cancellation"),
    "fairness": ("Mixed long/short fairness", "fairness"),
    "fairness_baseline": ("Short requests alone", "fairness"),
    "fairness_short": ("Short requests with competing load", "fairness"),
    "fairness_long": ("Competing long requests", "fairness"),
    "pack_control": ("Quiet baseline", "correctness"),
    "pack_loaded": ("Your questions under load", "correctness"),
    "request_pack": ("Your questions", "correctness"),
    "arrival": ("Scheduled arrivals", "arrivals"),
    "mixed_control": ("Short-request baseline", "mixed"),
    "prefix_shared": ("Shared prefix", "prefix"),
    "prefix_independent": ("Independent prefixes", "prefix"),
    "prefix_history": ("Growing conversation history", "prefix"),
    "consistency": ("Repeated-request consistency", "consistency"),
    "judging": ("AI evaluation", "judging"),
    "connection": ("Connection", "access"),
    "deadline_probe": ("Client deadline", "cancellation"),
}


def _plain(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").capitalize()


def _identity(scenario: str, workload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a stable condition and plain label without changing request metadata."""
    workload = workload or {}
    if calibration := re.fullmatch(r"load_calibration_v45_(short|medium|long)_(short_answer|long_output)", scenario):
        return {"id": scenario, "label": f"Calibrating {calibration[1]} input tokens", "check": "load", "load_type": "calibration"}
    if comparison := re.fullmatch(r"load_(?:primary|legacy_.+?)_interference_c(\d+)\.(baseline|probe)(?:\..+)?", scenario):
        level, phase = comparison.groups()
        name = "Short answers alone" if phase == "baseline" else "Short answers with a long request"
        return {"id": scenario, "label": f"{name} at {int(level)} concurrent requests", "check": "load", "load_type": "interference", "level": int(level)}
    if scenario.startswith(("load_primary_", "load_legacy_")):
        condition = {"id": scenario, "label": _plain(scenario.removeprefix("load_primary_")), "check": "load"}
        if level := re.search(r"_c(\d+)(?:\.|$)", scenario):
            condition["level"] = int(level[1])
        if rate := re.search(r"_r([0-9.e+-]+)(?:\.|$)", scenario):
            condition["rate_rps"] = float(rate[1].rstrip('.'))
        return condition
    if role := re.fullmatch(r"load_interference_c(\d+)\.(short_stream|short_probe|long_probe)", scenario):
        level, name = role.groups()
        scenario = f"load_interference_{'probe' if name == 'long_probe' else 'control'}_c{level}"
    if combo := re.fullmatch(r"load_combo_(short|medium|long)_(short|medium|long)_c(\d+)", scenario):
        a, b, level = combo.groups()
        return {"id": scenario, "label": f"{a.capitalize()} input → {b.capitalize()} output at {int(level)} concurrent request{'s' if int(level) != 1 else ''}",
                "check": "concurrency", "load_type": "combinations", "level": int(level), "input": a, "output": b}
    if mixed := re.fullmatch(r"(load_mixed_c(\d+))(?:\.(?:short|medium|long)_(?:short|medium|long))?", scenario):
        identifier, level = mixed.groups()
        return {"id": identifier, "label": f"Mixed load at {int(level)} concurrent request{'s' if int(level) != 1 else ''}", "check": "concurrency", "load_type": "mixed", "level": int(level)}
    if load := re.fullmatch(r"load_(interference|stress)_(control|probe|input|output)_c(\d+)", scenario):
        kind, workload, level = load.groups()
        name = {"control": "Short-only control", "probe": "Long input during short streams", "input": "Large-input stress", "output": "Large-output stress"}[workload]
        return {"id": scenario, "label": f"{name} at {int(level)} concurrent request{'s' if int(level) != 1 else ''}", "check": "concurrency", "load_type": kind, "level": int(level)}
    if calibration := re.fullmatch(r"load_calibration_(short|medium|long)_(short_answer|long_output)", scenario):
        size, variant = calibration.groups()
        name = "long-output" if variant == "long_output" else "short-answer"
        return {"id": scenario, "label": f"Calibrating {size.capitalize()} input tokens ({name} prompt)", "check": "concurrency", "load_type": "calibration"}
    if cell := re.fullmatch(r"bucket_c(\d+)_(short|medium|long)_(short|medium|long)", scenario):
        level, input_shape, output_shape = cell.groups()
        return {"id": scenario, "label": f"{input_shape.capitalize()} input / {output_shape} output · {int(level)} at once",
                "check": "buckets", "level": int(level), "input": input_shape, "output": output_shape}
    if consistency := re.fullmatch(r"consistency_c(\d+)_(.+?)(?:_p\d+)?", scenario):
        level, shape = consistency.groups()
        return {"id": f"consistency_c{int(level)}_{shape}",
                "label": f"{_plain(shape)} input consistency · {int(level)} at once",
                "check": "consistency", "level": int(level), "input": shape}
    if mixed := re.fullmatch(r"mixed_(?:(?:short|heavy)_)?(.+)_(before_output|active_stream)", scenario):
        kind, phase = mixed.groups()
        phase_label = "before output" if phase == "before_output" else "during output"
        return {"id": f"mixed_{kind}_{phase}", "label": f"{_plain(kind)} interference · {phase_label}",
                "check": "mixed", "level": 2}
    if session := re.fullmatch(r"session_(.+?)(?:_turn\d+)?", scenario):
        mode = session[1]
        return {"id": f"session_{mode}", "label": f"{_plain(mode)} sessions", "check": "sessions"}
    if tool := re.fullmatch(r"tools(?:_(.+))?_(auto|forced|required|none|named)_(complete|stream)", scenario):
        fixture, choice, mode = tool.groups()
        name = f"{_plain(fixture)} tool calls" if fixture else "Tool calls"
        choice_label = {"auto": "automatic", "forced": "forced", "required": "required", "none": "no tools", "named": "named tool"}[choice]
        return {"id": scenario, "label": f"{name} · {choice_label} · {'streaming' if mode == 'stream' else 'non-streaming'}", "check": "tools"}
    if conversation := re.fullmatch(r"tools_(.+)_(complete|stream)(?:_turn\d+)?", scenario):
        fixture, mode = conversation.groups()
        return {"id": f"tools_{fixture}_{mode}", "label": f"{_plain(fixture)} conversation · {'streaming' if mode == 'stream' else 'non-streaming'}",
                "check": "tools"}
    if stress := re.fullmatch(r"stress_(.+)_c(\d+)", scenario):
        case, level = stress.groups()
        return {"id": scenario, "label": f"{_plain(case)} · {int(level)} at once",
                "check": "token_stress", "level": int(level)}
    if scenario.startswith("stress_calibration_"):
        return {"id": scenario, "label": f"Input calibration · {_plain(scenario.removeprefix('stress_calibration_')).lower()}",
                "check": "token_stress"}
    if scenario == "arrival":
        rate = next((workload[key] for key in ("requests_per_second", "rate_rps", "arrival_rate_rps", "rate")
                     if workload.get(key) is not None), None)
        if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0:
            value = str(rate).removesuffix(".0")
            return {"id": f"arrival_r{value}", "label": f"Arrivals · {value} per second", "check": "arrivals"}
    if re.fullmatch(r"traffic_stage_\d+", scenario):
        rate = workload.get("rate_rps", workload.get("traffic_rate_rps"))
        label = f"{str(rate).removesuffix('.0')} requests/s" if rate is not None else "Capacity traffic"
        return {"id": scenario, "label": label, "check": "capacity"}
    if levelled := re.fullmatch(r"(correctness|json)_c(\d+)", scenario):
        check, level = levelled.groups()
        return {"id": scenario, "label": f"{_SCENARIOS[check][0]} · {int(level)} at once",
                "check": check, "level": int(level)}
    if scenario in _SCENARIOS:
        label, check = _SCENARIOS[scenario]
    else:
        label = _plain(scenario)
        check = next((prefix for prefix in ("tools", "prefix", "context", "correctness", "json", "sessions", "consistency")
                      if scenario.startswith(f"{prefix}_")), "serving")
    return {"id": scenario, "label": label, "check": check}


_PURPOSES = {
    "load": "Measures selected request shapes under maintained concurrency, arrivals or bursts; compares delivery and per-response criteria.",
    "concurrency": "Measures serving under sustained request concurrency with individually replenished slots.",
    "access": "Checks that the server is reachable and can answer a request.",
    "serving": "Checks that the server returns valid answers reliably.",
    "generation": "Measures how quickly the server generates an answer.",
    "context": "Checks whether the model can recall facts from a long input.",
    "correctness": "Checks answers against the expected answers for different questions.",
    "json": "Checks whether answers follow the required JSON structure.",
    "recovery": "Checks whether normal answers return after sustained heavy traffic.",
    "cancellation": "Checks request limits, cancellation, and the server's ability to keep serving afterwards.",
    "fairness": "Checks whether competing long requests delay short answers.",
    "mixed": "Checks whether a heavy partner delays a short request before or during its answer.",
    "prefix": "Compares serving behavior when requests share an input prefix or use independent prefixes.",
    "arrivals": "Checks whether the server keeps up with requests sent on a fixed schedule.",
    "capacity": "Measures how many requests the server can handle at the configured arrival rate.",
    "buckets": "Checks serving speed and reliability for this input size, output cap, and number of simultaneous requests.",
    "token_stress": "Checks serving behavior with a large input or a long generated answer under load.",
    "consistency": "Checks whether repeated copies of each prompt produce consistent answers under load.",
    "sessions": "Checks whether answers stay correct as conversation history grows across turns.",
    "tools": "Checks whether the model selects tools and uses their results correctly.",
    "judging": "Asks an AI evaluator to check a recorded answer against its expected answer.",
}


def condition_for(scenario: str, workload: Mapping[str, Any] | None = None, config: Any = None, *,
                  request_parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Canonical test name and explanation, enriched by configured and actual specs.

    Sizes preserve the builder's character measure, including serialized tool
    history. Missing sizes use message content characters without altering the
    spec or record. Approximate tokens use four characters per token unless the
    builder has a token target.
    Mixed conditions and multi-turn tests deliberately contain different inputs.
    """
    workload = dict(workload or {})
    details = request_parameters or {}
    if config is not None and (stage := re.fullmatch(r"traffic_stage_(\d+)", scenario)):
        rates = config.effective_traffic().rates
        if int(stage[1]) < len(rates):
            workload.setdefault("rate_rps", rates[int(stage[1])])
    if config is not None and scenario == "arrival" and config.arrivals:
        workload.setdefault("requests_per_second", config.arrivals.requests_per_second)
    condition = _identity(scenario, workload)
    check = condition["check"]
    parameters = {}
    if condition.get("level") is not None:
        parameters["concurrency"] = condition["level"]
    same = check in {"buckets", "token_stress", "arrivals", "generation"} or scenario in {
        "mixed_control", "fairness", "fairness_baseline", "fairness_short", "initial", "readiness",
        "recovery", "recovery_baseline", "recovery_prime", "stop_control", "client_cancel", "client_deadline",
        "backend_cancel", "backend_cancel_control", "backend_cancel_followup"}
    parameters["same_prompt"] = details.get("same_prompt", same)
    if scenario.startswith("stress_calibration_"):
        parameters["same_prompt"] = False
    if check == "fairness":
        parameters["concurrency"] = 1 if scenario in {"fairness", "fairness_baseline"} else 2
    if config is not None:
        if check == "consistency" and config.consistency:
            settings = config.consistency
            level = condition.get("level", 1)
            parameters["same_prompt"] = settings.logprobs is not None
            from .logprob_workload import sample_count
            parameters.update(repetitions_per_prompt=(settings.repetitions if settings.logprobs else
                sample_count(settings.repetitions, level)),
                prompts_per_shape=settings.prompts_per_shape)
            if settings.logprobs:
                from .logprob_workload import corpus_width
                width = corpus_width(settings)
                parameters.update(repetitions_per_prompt=width * settings.repetitions,
                                  priming_requests=width * settings.logprobs.priming_rounds,
                                  prompts_per_shape=1)
        if check == "sessions" and config.sessions:
            parameters["concurrency"] = min(config.sessions.concurrency, config.sessions.sessions)
        if check == "capacity" and (traffic := config.effective_traffic()) is not None:
            parameters["concurrency"] = traffic.max_in_flight
        if check == "arrivals" and (arrivals := config.effective_arrivals()) is not None:
            parameters["concurrency"] = arrivals.max_in_flight
        if check == "recovery":
            parameters["concurrency"] = config.effective_check("recovery")["concurrency"] if scenario == "sustained" else 1
        if check in {"correctness", "json"}:
            parameters.setdefault("concurrency", 1)
    for key in ("input_chars", "output_tokens", "repetitions_per_prompt", "concurrency"):
        if details.get(key) is not None:
            parameters[key] = details[key]
    if "output_tokens" not in parameters and workload.get("requested_output_tokens") is not None:
        parameters["output_tokens"] = workload["requested_output_tokens"]
    if details.get("requested_input_tokens") is not None:
        parameters["input_tokens_target"] = details["requested_input_tokens"]
    elif workload.get("requested_input_tokens") is not None:
        parameters["input_tokens_approx"] = workload["requested_input_tokens"]
    elif parameters.get("input_chars") is not None:
        parameters["input_tokens_approx"] = (parameters["input_chars"] + 3) // 4
    rate = workload.get("rate_rps", workload.get("requests_per_second", workload.get("traffic_rate_rps")))
    if rate is not None:
        parameters["rate_rps"] = rate
    purpose = {
        "fairness_baseline": "Measures short-request timing without competing load for comparison.",
        "mixed_control": "Measures short-request timing alone for comparison with heavy partners.",
        "prefix_shared": "Measures serving behavior with a shared input prefix and a different ending on each request.",
        "prefix_independent": "Measures serving behavior with independently generated input prefixes.",
        "prefix_history": "Checks recall as fixed conversation history grows across requests.",
        "sustained": "Applies sustained heavy traffic before checking whether the server recovers.",
    }.get(scenario, _PURPOSES[check])
    condition.update(purpose=purpose, parameters=parameters)
    return condition



def planned_conditions(config: Any) -> list[dict[str, Any]]:
    """Describe configured test cells before execution, without building requests.

    IDs come from the same point/scenario constructors as the executors. These
    are waiting UI/index summaries, not reservations or execution counters;
    authoritative lifecycle events replace them under the same target/id key.
    Setup, calibration and warm-up remain request/check evidence only.
    """
    from .fixtures import builtin_fixtures
    from .load_test import scheduled_count, settings_sets, test_points
    from .logprob_workload import corpus_width, sample_count
    from .planning import request_plan
    from .session_metrics import workload_plan
    from .tool_checks import resolved_tool_fixtures, resolved_tool_modes

    selected = set(config.checks)
    scenarios: dict[str, dict[str, Any]] = {}

    def add(scenario, planned=None, kind="unknown", workload=None):
        condition = condition_for(scenario, workload, config)
        key = condition["id"]
        if key in scenarios:
            previous = scenarios[key]
            if previous["planned"] is not None and planned is not None:
                previous["planned"] += planned
                previous["not_sent"] = previous["planned"]
            return
        scenarios[key] = {"condition": condition, "planned": planned,
            "planned_kind": kind if planned is not None else "unknown", "status": "waiting",
            "sent": 0, "started": 0, "running": 0, "completed": 0, "failed": 0,
            "not_sent": planned, "reason": None, "request_statuses": {}}

    # The request preview already accounts for shared initial work and finite
    # core batches; only rows that actually have a meaningful executor scenario
    # are cells (never check aliases that merely reuse another request).
    plan = request_plan(config)
    plan_rows = {row["id"]: row for row in plan["rows"]}
    # Preview/catalogue order differs from dispatch order. Match _target first,
    # then workloads.execute; ordering changes metadata only, never execution.
    for identifier in ("initial", "generation", "context_first_batch", "pack_control",
                       "pack_loaded", "correctness", "json"):
        row = plan_rows.get(identifier)
        if row is None:
            continue
        if identifier in {"correctness", "json"}:
            if not row["requests_per_target"]:
                continue
            identifier += f"_c{config.effective_check(identifier)['concurrency']}"
        elif identifier == "context_first_batch":
            identifier = "context"
        count = row["requests_per_target"]
        kind = "exact" if row["kind"] == "exact" else "upper_bound" if count is not None else "unknown"
        if identifier == "context":
            count, kind = None, "unknown"  # Adaptive repeated batches share one condition.
        if count != 0:
            add(identifier, count, kind)

    if "cancellation" in selected:
        options = config.effective_check("cancellation")
        fixtures = builtin_fixtures(config, check_id="cancellation")["limits"]
        normal = sum(spec.cancel_after_ms is None and not spec.fixture_id.startswith("limits.stop_control") for spec in fixtures)
        cancelled = sum(spec.cancel_after_ms is not None for spec in fixtures)
        add("stop_control", 1, "exact")
        add("limits", max(options["samples"], normal), "exact")
        if cancelled:
            add("cancellation_baseline", 1, "exact")
            add("client_cancel", cancelled, "exact")
        add("cancellation_probe", options["samples"], "exact")
        if cancelled:
            add("client_deadline", 1, "exact")
    for identifier in ("recovery_baseline", "sustained", "recovery"):
        row = plan_rows.get(identifier)
        if row and row["requests_per_target"] != 0:
            count = row["requests_per_target"]
            kind = "exact" if row["kind"] == "exact" else "upper_bound" if count is not None else "unknown"
            add(identifier, count, kind)
    if "prefix" in selected and config.prefix:
        settings = config.prefix
        add("prefix_shared", settings.repeats, "exact")
        add("prefix_independent", settings.repeats, "exact")
        add("prefix_history", settings.history_turns, "upper_bound")
    if "load" in selected:
        settings = dict(settings_sets(config.load_test))
        for point in test_points(config.load_test):
            own = settings[point["source"]]
            count = scheduled_count(own, point)
            # Poisson uses unconditioned draws: an expectation is not a plan.
            if point["pattern"] == "poisson":
                count = None
            for phase in ("baseline", "probe") if point["type"] == "interference" else (None,):
                workload = {"rate_rps": point["rate"]} if point["rate"] is not None else {}
                identifier = point["id"] + (f".{phase}" if phase else "")
                add(identifier, count, "exact" if count is not None else "unknown", workload)
                if point["pattern"] in {"concurrent", "interference"}:
                    scenarios[identifier]["planned_reason"] = "Unknown — duration-based"
    if "capacity" in selected:
        traffic = config.effective_traffic()
        if traffic is not None:
            from .models import MAX_PLANNED_ARRIVALS
            from .planning import _steady_count
            for stage, rate in enumerate(traffic.rates):
                count = (_steady_count(rate, traffic.duration_seconds) if traffic.arrival == "steady"
                         else min(round(rate * traffic.duration_seconds), MAX_PLANNED_ARRIVALS))
                add(f"traffic_stage_{stage}", count, "exact", {"rate_rps": rate})
    if "arrivals" in selected and config.arrivals:
        add("arrival", config.arrivals.requests, "exact",
            {"requests_per_second": config.arrivals.requests_per_second})
    if "sessions" in selected and config.sessions:
        settings = config.sessions
        count = (sum(len(item.turns) for item in workload_plan(settings.workloads, settings.sessions))
                 if settings.profile == "performance" else settings.sessions * len(settings.turns))
        for mode in settings.modes:
            add(f"session_{mode}_turn0", count, "upper_bound")
    if "consistency" in selected and config.consistency:
        settings = config.consistency
        for shape in settings.shapes:
            for level in dict.fromkeys([1, *settings.levels]):
                count = (corpus_width(settings) * (settings.repetitions + settings.logprobs.priming_rounds)
                         if settings.logprobs else settings.prompts_per_shape * sample_count(settings.repetitions, level))
                add(f"consistency_c{level}_{shape}", count, "exact")
    if "tools" in selected:
        custom = bool(config.tool_checks and config.tool_checks.fixtures)
        for fixture in config.tool_checks.conversations if config.tool_checks else []:
            for mode in resolved_tool_modes(config):
                add(f"tools_{fixture.fixture_id}_{mode}_turn0", len(fixture.steps) + 1, "upper_bound")
        for forced in ([False, True] if config.forced_tool_diagnostic else [False]):
            for fixture in resolved_tool_fixtures(config):
                if forced and fixture.choice:
                    continue
                choice = "forced" if forced else fixture.choice.mode if fixture.choice else "auto"
                prefix = f"tools_{fixture.fixture_id}" if custom else "tools"
                for mode in resolved_tool_modes(config):
                    add(f"{prefix}_{choice}_{mode}", 1, "exact")
    return [condition_aggregate({"target": target.name, **summary})
            for target in config.targets for summary in scenarios.values()]


def request_status(record: Mapping[str, Any]) -> str:
    """UI result classes; transport completion alone never establishes correctness."""
    status = record.get("status")
    if status == "running":
        return "running"
    if status in {"timeout", "timed_out"}:
        return "timed_out"
    if status == "cancelled":
        return "cancelled"
    if status == "failed" or record.get("score") is False or record.get("valid") is False:
        return "failed"
    return "passed" if record.get("score") is True else "unscored"


def request_result_text(record: Mapping[str, Any]) -> str:
    status = request_status(record)
    if status == "failed" and record.get("http_status") and record["http_status"] >= 400:
        return f"HTTP {record['http_status']}"
    return {"running": "Awaiting response", "passed": "Correct answer", "failed": "Wrong answer" if record.get("score") is False else "Request failed",
            "timed_out": "Timed out", "cancelled": "Cancelled", "unscored": "Unscored"}[status]


def condition_aggregate(summary: Mapping[str, Any], *, verdict: str | None = None,
                        statuses: Mapping[str, int] | None = None) -> dict[str, Any]:
    """Present lifecycle totals without treating finished requests as passing tests.

    Unknown and duration plans have no numeric denominator. An upper bound is
    retained for counts, but is never expanded into invented request rows.
    """
    condition = dict(summary.get("condition") or {})
    parameters = condition.get("parameters") or {}
    started = summary.get("started", summary.get("sent", 0)) or 0
    running = summary.get("running", 0) or 0
    completed = summary.get("completed", max(0, started - running)) or 0
    planned = summary.get("planned")
    kind = summary.get("planned_kind", "unknown") if planned is not None else "unknown"
    if kind not in {"exact", "upper_bound"}:
        kind = "unknown"
    statuses = dict(statuses if statuses is not None else summary.get("request_statuses") or {})
    failed = summary.get("failed", statuses.get("failed", 0) + statuses.get("timed_out", 0)) or 0
    not_started = max(0, planned - started) if planned is not None else None
    reason = summary.get("reason")
    terminal = summary.get("status") not in {"waiting", "running"}
    explicit = verdict or (summary.get("load_point_result") or {}).get("status")
    if running or not terminal and summary.get("status") == "running":
        status = "running"
    elif not terminal:
        status = "waiting"
    elif explicit in {"pass", "fail", "inconclusive", "blocked", "skipped"}:
        status = {"pass": "passed", "fail": "failed", "blocked": "inconclusive"}.get(explicit, explicit)
    elif failed:
        status = "failed"
    elif reason and (not started or kind == "exact" and not_started or any(
            word in reason.lower() for word in ("run stopped", "time limit", "request budget", "cancelled", "aborted"))):
        status = "stopped" if started else "skipped"
    elif completed and statuses.get("passed", 0) == completed:
        status = "passed"
    else:
        status = "inconclusive"
    rate = parameters.get("rate_rps", condition.get("rate_rps"))
    concurrency = parameters.get("concurrency", condition.get("level"))
    description = f"{rate:g} requests/s" if isinstance(rate, (int, float)) else (
        f"{concurrency:,} at once" if isinstance(concurrency, int) else "Concurrency not recorded")
    return {**dict(summary), "condition_id": condition.get("id"), "check_id": condition.get("check"),
            "name": condition.get("label") or condition.get("id"), "concurrency_description": description,
            "aggregate_status": status, "status": status, "lifecycle_status": summary.get("status"),
            "planned": planned, "planned_kind": kind, "started": started, "running": running,
            "completed": completed, "failed": failed, "not_started": not_started,
            "reasons": [reason] if reason else [], "request_statuses": statuses}


def _condition_verdicts(checks: list[dict]) -> dict[tuple[str, str], str]:
    verdicts = {}
    severity = {"fail": 0, "blocked": 1, "inconclusive": 2, "skipped": 3, "pass": 4}

    def add(target, identifier, status):
        if identifier is None or status not in severity:
            return
        identifier = _identity(str(identifier))["id"]
        key = target, identifier
        if key not in verdicts or severity[status] < severity[verdicts[key]]:
            verdicts[key] = status

    def visit(check, target):
        metrics = check.get("metrics") or {}
        for point in metrics.get("points", []):
            add(target, point.get("id"), point.get("status"))
            if point.get("type") == "interference":
                for suffix in (".baseline", ".probe"):
                    add(target, str(point.get("id")) + suffix, point.get("status"))
        for group in (metrics.get("load_types") or {}).values():
            for point in group.get("points", []):
                add(target, point.get("id"), point.get("status"))
                if point.get("type") == "interference":
                    add(target, str(point.get("id")).replace("_probe_", "_control_"), point.get("status"))
        for identifier, row in (metrics.get("buckets") or {}).items():
            add(target, identifier, row.get("status"))
        for row in (metrics.get("logprobs") or {}).get("conditions", []):
            add(target, f"consistency_c{row['level']}_{row['shape']}", row.get("status"))
        for name, item in (metrics.get("cases") or {}).items():
            for level, row in item.get("levels", {}).items():
                add(target, f"stress_{name}_c{level}", row.get("status"))
        for row in metrics.get("conversations", []):
            add(target, f"tools_{row.get('fixture_id')}_{row.get('mode')}", row.get("status"))
        for scenario in (metrics.get("scenarios") or {}).values():
            visit(scenario, target)

    for check in checks:
        visit(check, check.get("target"))
    return verdicts


def aggregate_conditions(report: Any) -> list[dict[str, Any]]:
    """Build condition aggregates once, with scenario grouping for legacy reports."""
    def plain(value):
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    observations = report.get("observations", {}) if isinstance(report, Mapping) else report.observations
    records = report.get("requests", []) if isinstance(report, Mapping) else report.requests
    checks = report.get("checks", []) if isinstance(report, Mapping) else report.checks
    groups = {}
    for target, observation in observations.get("targets", {}).items():
        for summary in observation.get("conditions", []):
            condition = summary.get("condition") or {}
            groups[target, condition.get("id")] = {**summary, "target": target}
    counts = {}
    for item in records:
        record = plain(item)
        condition = condition_for(record.get("condition_id") or record.get("scenario") or "unknown", record.get("workload"))
        key = record.get("target"), condition["id"]
        if key not in groups:
            groups[key] = {"target": key[0], "condition": condition, "status": "finished", "planned": None,
                           "planned_kind": "unknown", "started": 0, "running": 0, "completed": 0, "failed": 0}
        status = request_status(record)
        statuses = counts.setdefault(key, {})
        statuses[status] = statuses.get(status, 0) + 1
    check_rows = [plain(check) for check in checks]
    verdicts = _condition_verdicts(check_rows)
    # A check verdict is a condition verdict only when it owns one condition.
    # Matrix checks must retain their per-condition evidence instead.
    owners = {}
    for key, summary in groups.items():
        owners.setdefault((key[0], summary["condition"].get("check")), []).append(key)
    for check in check_rows:
        owned = owners.get((check.get("target"), check.get("id")), [])
        if len(owned) == 1 and check.get("status") in {"pass", "fail", "inconclusive", "blocked", "skipped"}:
            verdicts.setdefault(owned[0], check["status"])
    for key, statuses in counts.items():
        summary = groups[key]
        total = sum(statuses.values())
        # Saved lifecycle evidence can describe work absent from retained records.
        summary["started"] = max(summary.get("started", summary.get("sent", 0)) or 0, total)
        summary["completed"] = max(summary.get("completed", 0) or 0, total - statuses.get("running", 0))
        # Executor failure totals already account for intentional negative probes.
        if "failed" not in summary:
            summary["failed"] = statuses.get("failed", 0) + statuses.get("timed_out", 0)
        elif summary.get("planned_kind") == "unknown" and summary.get("planned") is None:
            summary["failed"] = max(summary["failed"], statuses.get("failed", 0) + statuses.get("timed_out", 0))
    return [condition_aggregate(summary, verdict=verdicts.get(key), statuses=counts.get(key))
            for key, summary in groups.items()]


class ConditionRequestIndex:
    """Compact keyed rows for live polling; completed rows survive UI-cache eviction.

    Updates touch only one request and its condition. Sorting/filtering is done
    only for an explicitly requested page, never for each live progress event.
    Bodies, reasoning and arbitrary endpoint error strings are never retained.
    """

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.slots: dict[tuple[str, str], list[int]] = {}
        self.statuses: dict[tuple[str, str], dict[str, set[int]]] = {}
        self.summaries: dict[tuple[str, str], dict] = {}

    def summary(self, value):
        condition = value.get("condition") or {}
        if condition.get("id"):
            self.summaries[value.get("target"), condition["id"]] = dict(value)

    def update(self, event):
        if event.get("condition_summary"):
            self.summary(event["condition_summary"])
        slot = event.get("request_slot")
        if not isinstance(slot, int) or slot < 0 or event.get("event") not in {"request_started", "request_completed"}:
            return
        previous = self.rows.get(slot)
        record = {**(previous or {}), **(event.get("request") or {})}
        for key in ("target", "scenario", "condition_id"):
            if event.get(key) is not None:
                record[key] = event[key]
        if event["event"] == "request_started":
            record["status"] = "running"
        elif not event.get("request"):
            return
        condition_id = record.get("condition_id") or condition_for(record.get("scenario") or "unknown")["id"]
        key = record.get("target"), condition_id
        if previous:
            old_key = previous["_key"]
            self.statuses[old_key][previous["_status"]].discard(slot)
        else:
            from bisect import insort
            insort(self.slots.setdefault(key, []), slot)
        status = request_status(record)
        self.statuses.setdefault(key, {}).setdefault(status, set()).add(slot)
        self.rows[slot] = {key: record.get(key) for key in ("id", "target", "scenario", "condition_id", "status", "valid", "score", "http_status")} | {
            "_key": key, "_status": status, "result_text": request_result_text(record)}

    @classmethod
    def from_report(cls, report):
        result = cls()
        values = report if isinstance(report, Mapping) else report.model_dump(mode="json")
        for summary in aggregate_conditions(values):
            result.summary({**summary, "status": summary["lifecycle_status"]})
        # Reports retain dispatch order; timing evidence refines it if available.
        records = list(enumerate(values.get("requests", [])))
        records.sort(key=lambda item: (item[1].get("dispatch_ms") if item[1].get("dispatch_ms") is not None else float("inf"), item[0]))
        for slot, (_, record) in enumerate(records):
            result.update({"event": "request_completed", "request_slot": slot, "request": record})
        return result

    def page(self, condition_id, *, target=None, status=None, page=1, page_size=10):
        keys = [key for key in self.summaries.keys() | self.slots.keys() if key[1] == condition_id and (target is None or key[0] == target)]
        if not keys:
            raise KeyError(condition_id)
        if len(keys) != 1:
            raise ValueError("Condition IDs are target-local; select a target with ?target=.")
        key = keys[0]
        slots = self.slots.get(key, [])
        statuses = self.statuses.get(key, {})
        summary = self.summaries.get(key) or {
            "target": key[0], "condition": condition_for(key[1]), "status": "running" if statuses.get("running") else "finished",
            "started": len(slots), "running": len(statuses.get("running", set())),
            "completed": len(slots) - len(statuses.get("running", set())),
            "failed": len(statuses.get("failed", set())) + len(statuses.get("timed_out", set()))}
        planned = summary.get("planned")
        missing = max(0, planned - (summary.get("started", summary.get("sent", len(slots))) or 0)) if planned is not None else 0
        exact = summary.get("planned_kind") == "exact" and planned is not None
        placeholder_count = missing if exact else 0
        totals = {name: len(statuses.get(name, set())) for name in ("running", "passed", "failed", "timed_out", "cancelled", "unscored")}
        totals["not_started"] = missing if planned is not None else None
        aliases = {"timeout": "timed_out", "not-started": "not_started", "pass": "passed", "fail": "failed"}
        status = aliases.get(status, status)
        if status and status not in {"all", "running", "passed", "failed", "timed_out", "not_started", "cancelled", "unscored"}:
            raise ValueError("Unknown request status")
        selected = sorted(statuses.get(status, set())) if status and status != "all" and status != "not_started" else slots if status != "not_started" else []
        include_placeholders = status in {None, "", "all", "not_started"}
        total = len(selected) + (placeholder_count if include_placeholders else 0)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        rows = []
        positions = None
        for offset in range(start, min(start + page_size, total)):
            if offset < len(selected):
                slot = selected[offset]
                record = self.rows[slot]
                if positions is None:
                    positions = {slot: number for number, slot in enumerate(slots, 1)}
                rows.append({"number": positions[slot], "request_id": record["id"], "status": record["_status"], "result_text": record["result_text"]})
            else:
                reason = summary.get("reason") or "Not dispatched"
                rows.append({"number": len(slots) + offset - len(selected) + 1, "request_id": None,
                             "status": "not_started", "result_text": f"Not sent: {reason}"})
        aggregate = condition_aggregate(summary, statuses={name: len(values) for name, values in statuses.items()})
        if aggregate["status"] != "running" and summary.get("aggregate_status"):
            aggregate["status"] = aggregate["aggregate_status"] = summary["aggregate_status"]
        return {"condition_id": condition_id, "target": key[0], "page": page, "page_size": page_size,
                "total": total, "pages": pages, "range_start": start + 1 if total else 0,
                "range_end": min(start + len(rows), total), "rows": rows, "totals": totals,
                "number_width": max(4, len(str(max(len(slots) + placeholder_count, planned or 0)))),
                "placeholder_rows": exact, "aggregate": aggregate}
