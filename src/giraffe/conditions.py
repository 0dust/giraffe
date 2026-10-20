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
        if check == "capacity":
            parameters["concurrency"] = min(config.effective_traffic().max_in_flight or config.concurrency, config.concurrency)
        if check == "arrivals":
            parameters["concurrency"] = config.concurrency
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
