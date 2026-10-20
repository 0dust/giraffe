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
}


def _plain(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").capitalize()


def condition_for(scenario: str, workload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a stable condition and plain label without changing request metadata."""
    workload = workload or {}
    if cell := re.fullmatch(r"bucket_c(\d+)_(short|medium|long)_(short|medium|long)", scenario):
        level, input_shape, output_shape = cell.groups()
        return {"id": scenario, "label": f"{input_shape.capitalize()} input / {output_shape} output · {int(level)} at once",
                "check": "buckets", "level": int(level), "input": input_shape, "output": output_shape}
    if consistency := re.fullmatch(r"consistency_c(\d+)_(.+?)(?:_p\d+)?", scenario):
        level, shape = consistency.groups()
        return {"id": f"consistency_c{int(level)}_{shape}",
                "label": f"{_plain(shape)} input consistency · {int(level)} at once",
                "check": "consistency", "level": int(level), "input": shape}
    if mixed := re.fullmatch(r"mixed_(?:short|heavy)_(.+)_(before_output|active_stream)", scenario):
        kind, phase = mixed.groups()
        phase_label = "before output" if phase == "before_output" else "during output"
        return {"id": f"mixed_{kind}_{phase}", "label": f"{_plain(kind)} interference · {phase_label}",
                "check": "mixed", "level": 2}
    if session := re.fullmatch(r"session_(.+)_turn\d+", scenario):
        mode = session[1]
        return {"id": f"session_{mode}", "label": f"{_plain(mode)} sessions", "check": "sessions"}
    if conversation := re.fullmatch(r"tools_(.+)_(complete|stream)_turn\d+", scenario):
        fixture, mode = conversation.groups()
        return {"id": f"tools_{fixture}_{mode}", "label": f"{_plain(fixture)} conversation · {mode}",
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
    if stage := re.fullmatch(r"traffic_stage_(\d+)", scenario):
        return {"id": scenario, "label": f"Traffic stage {int(stage[1]) + 1}", "check": "capacity"}
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
