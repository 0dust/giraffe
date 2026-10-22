"""Portable operator cases compiled into the existing request/scoring contract."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from .models import (JUDGE_PROMPT_VERSION, JUDGE_VOTING_RULE, MAX_REQUEST_PACK_BYTES,
                     RequestPack, RequestSpec, RunConfig)

CASE_SELECTOR_VERSION = "largest-remainder-v1"
CONTROL_POLICY = "one-serial-request-per-case-v1"
INPUT_RETENTION_NOTICE = (
    "Requests and expected answers are saved with the run. "
    "Response retention applies only to model outputs."
)


def load_request_pack(path: str | Path) -> RequestPack:
    """Read once and embed the validated contents; execution never needs this path."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_REQUEST_PACK_BYTES + 1)
        if len(raw) > MAX_REQUEST_PACK_BYTES:
            raise ValueError("request pack exceeds 1 MiB")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot read request pack: expected a local UTF-8 JSON file") from error
    return RequestPack.model_validate(value)


def pack_warnings(pack: RequestPack) -> list[str]:
    """Flag weak answer evidence without rejecting intentional copy/extraction cases."""
    return [f"Case {case.fixture_id!r}: expected text appears verbatim in its messages; an echo would pass."
            for case in pack.cases if case.scorer in {"exact", "contains"}
            and any(case.expected in message.content for message in case.messages)]


def active_pack(config: RunConfig) -> bool:
    return (config.request_source == "request_pack" and config.request_pack is not None
            and bool({"load", "capacity", "correctness"}.intersection(config.checks)))


def pack_settings(config: RunConfig) -> dict[str, Any]:
    """Read-only request settings for the dataset correctness panel."""
    return {
        "read_only": True,
        "concurrency": config.inherited_concurrency(config.default_concurrency),
        "fields": ["default_concurrency", "max_output_tokens", "request_timeout_seconds"],
        "explanation": "Dataset correctness uses every case once for serial controls, then default_concurrency for its loaded pass. Case output budgets and suite request defaults supply request settings; per-check request overrides are unsupported. When Capacity is selected, its requests provide the shared loaded pass.",
    }


def pack_specs(config: RunConfig) -> list[RequestSpec]:
    if not active_pack(config):
        return []
    consumer = "load" if "load" in config.checks else "capacity" if "capacity" in config.checks else "correctness"
    effective = config.effective_check(consumer)
    cap = effective.get("max_output_tokens", config.max_output_tokens)
    specs = []
    for case in sorted(config.request_pack.cases, key=lambda case: case.fixture_id):
        options = config.request_options | case.options
        if config.stream:
            options.setdefault("stream_options", {"include_usage": True})
        messages = [message.model_dump() for message in case.messages]
        specs.append(RequestSpec(
            fixture_id=f"pack:{case.fixture_id}", scenario="request_pack",
            check_ids=[check for check in ("load", "capacity", "correctness") if check in config.checks],
            messages=messages, scorer=case.scorer, expected=case.expected, schema=case.schema_,
            expected_provided="expected" in case.model_fields_set, rubric=case.rubric,
            max_tokens=case.max_tokens or cap, stream=config.stream, options=options,
            request_timeout_seconds=effective.get("request_timeout_seconds", config.request_timeout_seconds),
            input_chars=sum(len(message.content) for message in case.messages),
            workload={"case_id": case.fixture_id, "request_source": "request_pack"},
        ))
    return specs


def _weights(config: RunConfig) -> dict[str, float]:
    cases = sorted(config.request_pack.cases, key=lambda case: case.fixture_id)
    scale = max(case.weight for case in cases)
    weights = [case.weight / scale for case in cases]
    total = math.fsum(weights)
    return {case.fixture_id: weight / total for case, weight in zip(cases, weights, strict=True)}


def case_counts(config: RunConfig, count: int) -> dict[str, int]:
    if count < 0:
        raise ValueError("case count must be non-negative")
    if not active_pack(config):
        return {}
    quotas = {key: weight * count for key, weight in _weights(config).items()}
    counts = {key: math.floor(quota) for key, quota in quotas.items()}
    ordered = sorted(quotas, key=lambda key: (-(quotas[key] - counts[key]), key))
    for key in ordered[:count - sum(counts.values())]:
        counts[key] += 1
    return counts


def case_sequence(config: RunConfig, count: int, stage_index: int = 0) -> list[RequestSpec]:
    """Allocate before admission; independent of arrivals, completion order, and drops."""
    counts = case_counts(config, count)
    specs = {spec.workload["case_id"]: spec for spec in pack_specs(config)}
    sequence = [specs[key] for key, amount in counts.items() for _ in range(amount)]
    random.Random((config.traffic.seed + stage_index) ^ 0xBB67AE85).shuffle(sequence)
    return sequence


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _stages(config: RunConfig, *, include_schedule_hash: bool = True) -> list[dict[str, Any]]:
    if not active_pack(config) or "capacity" not in config.checks:
        return []
    from .traffic import arrival_schedule

    stages = []
    for index, rate in enumerate(config.traffic.rates):
        schedule, truncated = arrival_schedule(config.effective_traffic(), rate, index)
        stage = {"stage_index": index, "rate_rps": rate, "planned": len(schedule),
                 "counts": case_counts(config, len(schedule)), "truncated": truncated}
        if include_schedule_hash:
            stage["schedule_hash"] = _digest([offset for offset, _ in schedule])
        stages.append(stage)
    return stages


def _effective(spec: RequestSpec) -> dict[str, Any]:
    return {"max_tokens": spec.max_tokens, "stream": spec.stream,
            "request_timeout_seconds": spec.request_timeout_seconds,
            "options": spec.options}


def judging_identity(config: RunConfig) -> dict[str, Any] | None:
    """Effective evaluator semantics, absent when no selected case uses a judge."""
    if not active_pack(config):
        return None
    judged = [case for case in config.request_pack.cases if case.scorer == "judge"]
    if not judged:
        return None
    settings = config.judging.model_dump(mode="json")
    settings["judges"] = sorted(settings["judges"], key=lambda judge: judge["name"])
    identity = {"prompt_version": JUDGE_PROMPT_VERSION, "voting_rule": JUDGE_VOTING_RULE,
                "settings": settings,
                "cases": [{"fixture_id": case.fixture_id, "messages": [message.model_dump() for message in case.messages],
                           "expected": case.expected, "rubric": case.rubric}
                          for case in sorted(judged, key=lambda case: case.fixture_id)]}
    return {"sha256": _digest(identity), "prompt_version": JUDGE_PROMPT_VERSION,
            "voting_rule": JUDGE_VOTING_RULE, "settings": settings}


def pack_identity(config: RunConfig) -> dict[str, Any] | None:
    if not active_pack(config):
        return None
    weights = _weights(config)
    cases = [{"fixture_id": spec.workload["case_id"], "weight": weights[spec.workload["case_id"]],
              "messages": spec.messages, "scorer": spec.scorer, "expected": spec.expected,
              "expected_provided": spec.expected_provided,
              "schema": spec.schema_, "effective": _effective(spec)} for spec in pack_specs(config)]
    identity = {"version": 1, "case_selector_version": CASE_SELECTOR_VERSION,
                "control_policy": CONTROL_POLICY, "cases": cases}
    evaluation = judging_identity(config)
    if evaluation is not None:
        identity["judging"] = evaluation
    if "capacity" in config.checks:
        identity.update(seed=config.traffic.seed, stages=_stages(config))
    return {"sha256": _digest(identity), "case_selector_version": CASE_SELECTOR_VERSION,
            "control_policy": CONTROL_POLICY, "case_ids": list(weights),
            "stages": identity.get("stages", []),
            **({"judging": evaluation} if evaluation is not None else {})}


def _pack_summary(config: RunConfig, stages: list[dict[str, Any]]) -> dict[str, Any]:
    cases = config.request_pack.cases if active_pack(config) else []
    quiet = len(cases) if "load" not in config.checks or "correctness" in config.checks else 0
    planned = quiet + sum(stage["planned"] for stage in stages)
    if config.max_requests is None:
        quotas = {target.name: None for target in config.targets}
    else:
        base, extra = divmod(config.max_requests, len(config.targets))
        quotas = {target.name: base + (index < extra) for index, target in enumerate(config.targets)}
    judge_ids = {case.fixture_id for case in cases if case.scorer == "judge"}
    planned_judged = (len(judge_ids) if quiet else 0) + sum(
        sum(stage["counts"].get(case_id, 0) for case_id in judge_ids) for stage in stages)
    load_plan = None
    if "load" in config.checks and active_pack(config):
        from giraffe.load_test import load_time_plan
        load_plan = load_time_plan(config.load_test)
        planned += load_plan["expected_scheduled_requests"]
        # Random weighted selection can choose a judged case on every offer.
        # A proportional expectation is not a safe evaluator-call upper bound.
        if judge_ids:
            planned_judged += load_plan["expected_scheduled_requests"]
    judge_count = len(config.judging.judges) if judge_ids else 0
    readiness_errors = []
    if judge_ids:
        if not config.judging.judges:
            readiness_errors.append("Add at least one judge.")
        for index, judge in enumerate(config.judging.judges):
            missing = [field for field in ("name", "url", "model") if not getattr(judge, field).strip()]
            if missing:
                readiness_errors.append(f"Judge {index + 1} needs: {', '.join(missing)}.")
    judging = {"active": bool(judge_ids), "judge_count": judge_count,
               "ready": not readiness_errors, "readiness_errors": readiness_errors,
               "planned_requests_per_target": planned_judged,
               "planned_call_upper_bound": planned_judged * len(config.targets) * judge_count,
               "quota_call_upper_bound": sum(planned_judged if quota is None else min(planned_judged, quota) for quota in quotas.values()) * judge_count}
    if load_plan:
        judging["planned_requests_approximate"] = load_plan["approximate"]
        judging["unknown_completion_driven_requests"] = load_plan["unknown_requests"]
        # The shared hard budget bounds evaluator work even for adaptive traffic;
        # without it the count must stay unknown rather than claiming zero calls.
        if load_plan["unknown_requests"] or load_plan["approximate"]:
            judging["planned_call_upper_bound"] = config.max_requests * judge_count if config.max_requests is not None else None
            judging["quota_call_upper_bound"] = judging["planned_call_upper_bound"]
    return {"quiet_requests": quiet, "stages": stages, "load_test": load_plan, "planned_requests_per_target": planned,
            "per_target_request_quota": quotas, "request_cap": config.max_requests, "judging": judging}


def pack_summary(config: RunConfig) -> dict[str, Any]:
    """Count seeded dataset work and judge calls without payloads or identity hashes."""
    return _pack_summary(config, _stages(config, include_schedule_hash=False))


def pack_preview(config: RunConfig) -> dict[str, Any]:
    specs = pack_specs(config)
    active = active_pack(config)
    cases = {case.fixture_id: case for case in config.request_pack.cases} if config.request_pack else {}
    summary = _pack_summary(config, _stages(config))
    stages = summary["stages"]
    planned = summary["planned_requests_per_target"]
    quotas = summary["per_target_request_quota"]
    judging = summary["judging"] | {"identity": judging_identity(config)}
    warnings = pack_warnings(config.request_pack) if config.request_pack else []
    if config.request_source == "request_pack" and not active:
        warnings.append("Request pack is inactive: select Answer correctness or Load test to exercise it.")
    if active:
        warnings.append("Repeated, warmed workload: quiet controls and traffic repeat the same requests and may benefit caches.")
        if any(case.scorer == "none" for case in cases.values()):
            warnings.append("Unscored cases prevent a full-pack correct-capacity claim, including cases with zero offers.")
        if any(any(amount == 0 for amount in stage["counts"].values()) for stage in stages):
            warnings.append("Some stages do not offer every case; full-pack coverage cannot be established at those rates.")
        if any(quota is not None and planned > quota for quota in quotas.values()):
            warnings.append("Planned pack requests exceed a target request quota; the bounded run may have incomplete coverage.")
        if config.max_duration_seconds is not None and sum(config.traffic.duration_seconds for _ in stages) > config.max_duration_seconds:
            warnings.append("Planned traffic duration exceeds the run time budget before quiet requests or draining.")
        if not config.stream and config.effective_check("capacity" if stages else "correctness")["limits"].get("stream_gap_ms") is not None:
            warnings.append("Streaming is disabled; the configured stream-gap limit will lack evidence.")
        if set(config.checks) - {"load", "capacity", "correctness"} and (config.max_requests is not None or config.max_duration_seconds is not None):
            warnings.append("Other selected checks consume additional shared request and time budget; their requests are not included in the pack subtotal.")
    if judging["active"]:
        warnings.append("AI judging runs after target traffic. Messages, golden answers and generated answers are sent to every configured judge.")
        if judging["readiness_errors"]:
            warnings.append("Configure at least one judge with a name, URL and model before running judge-scored cases.")
    effective_requests = {_digest({"messages": spec.messages, **_effective(spec)}) for spec in specs}
    return {"source": config.request_source, "active": active,
            "consumers": [check for check in ("load", "capacity", "correctness") if active and check in config.checks],
            "case_count": len(cases), "scorer_counts": dict(Counter(case.scorer for case in cases.values())),
            "cases": [{"fixture_id": spec.workload["case_id"], "scorer": spec.scorer,
                       "weight": cases[spec.workload["case_id"]].weight,
                       "input_chars": spec.input_chars,
                       "requested": {"max_tokens": cases[spec.workload["case_id"]].max_tokens,
                                     "options": cases[spec.workload["case_id"]].options},
                       "effective": _effective(spec)} for spec in specs],
            "quiet_requests": summary["quiet_requests"], "stages": stages,
            "planned_requests_per_target": planned, "known_selected_check_subtotal": planned,
            "load_test": summary.get("load_test"),
            "per_target_request_quota": quotas, "request_cap": config.max_requests,
            "unique_effective_requests": len(effective_requests),
            "repeated_requests_per_target": max(0, planned - len(effective_requests)),
            "inapplicable_settings": ["correctness.samples", "correctness.concurrency",
                                      "correctness.max_output_tokens", "correctness.request_timeout_seconds"] if active else [],
            "correctness_settings": pack_settings(config) if active else None,
            "identity": pack_identity(config), "judging": judging, "warnings": warnings,
            "input_retention_notice": INPUT_RETENTION_NOTICE}
