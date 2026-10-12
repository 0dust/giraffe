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


def active_pack(config: RunConfig) -> bool:
    return (config.request_source == "request_pack" and config.request_pack is not None
            and bool({"capacity", "correctness"}.intersection(config.checks)))


def pack_specs(config: RunConfig) -> list[RequestSpec]:
    if not active_pack(config):
        return []
    consumer = "capacity" if "capacity" in config.checks else "correctness"
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
            check_ids=[check for check in ("capacity", "correctness") if check in config.checks],
            messages=messages, scorer=case.scorer, expected=case.expected, schema=case.schema_,
            expected_provided="expected" in case.model_fields_set, rubric=case.rubric,
            max_tokens=min(case.max_tokens or cap, cap), stream=config.stream, options=options,
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


def _stages(config: RunConfig) -> list[dict[str, Any]]:
    if not active_pack(config) or "capacity" not in config.checks:
        return []
    from .traffic import arrival_schedule

    stages = []
    for index, rate in enumerate(config.traffic.rates):
        schedule, truncated = arrival_schedule(config.effective_traffic(), rate, index)
        stages.append({"stage_index": index, "rate_rps": rate, "planned": len(schedule),
                       "counts": case_counts(config, len(schedule)), "truncated": truncated,
                       "schedule_hash": _digest([offset for offset, _ in schedule])})
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


def pack_preview(config: RunConfig) -> dict[str, Any]:
    specs = pack_specs(config)
    active = active_pack(config)
    cases = {case.fixture_id: case for case in config.request_pack.cases} if config.request_pack else {}
    stages = _stages(config)
    quiet = len(specs)
    planned = quiet + sum(stage["planned"] for stage in stages)
    base, extra = divmod(config.max_requests, len(config.targets))
    quotas = {target.name: base + (index < extra) for index, target in enumerate(config.targets)}
    warnings = []
    if config.request_source == "request_pack" and not active:
        warnings.append("Request pack is inactive: select correctness or capacity to exercise it.")
    if active:
        warnings.append("Repeated, warmed workload: quiet controls and traffic repeat the same requests and may benefit caches.")
        if any(case.scorer == "none" for case in cases.values()):
            warnings.append("Unscored cases prevent a full-pack correct-capacity claim, including cases with zero offers.")
        if any(any(amount == 0 for amount in stage["counts"].values()) for stage in stages):
            warnings.append("Some stages do not offer every case; full-pack coverage cannot be established at those rates.")
        if any(planned > quota for quota in quotas.values()):
            warnings.append("Planned pack requests exceed a target request quota; the bounded run may have incomplete coverage.")
        if sum(config.traffic.duration_seconds for _ in stages) > config.max_duration_seconds:
            warnings.append("Planned traffic duration exceeds the run time budget before quiet requests or draining.")
        if not config.stream and config.effective_check("capacity" if stages else "correctness")["limits"].get("stream_gap_ms") is not None:
            warnings.append("Streaming is disabled; the configured stream-gap limit will lack evidence.")
        if set(config.checks) - {"capacity", "correctness"}:
            warnings.append("Other selected checks consume additional shared request and time budget; their requests are not included in the pack subtotal.")
    judge_ids = {spec.workload["case_id"] for spec in specs if spec.scorer == "judge"}
    planned_judged = len(judge_ids) + sum(
        sum(stage["counts"].get(case_id, 0) for case_id in judge_ids) for stage in stages)
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
               "quota_call_upper_bound": sum(min(planned_judged, quota) for quota in quotas.values()) * judge_count,
               "identity": judging_identity(config)}
    if judge_ids:
        warnings.append("AI judging runs after target traffic. Messages, golden answers and generated answers are sent to every configured judge.")
        if readiness_errors:
            warnings.append("Configure at least one judge with a name, URL and model before running judge-scored cases.")
    effective_requests = {_digest({"messages": spec.messages, **_effective(spec)}) for spec in specs}
    return {"source": config.request_source, "active": active,
            "consumers": [check for check in ("capacity", "correctness") if active and check in config.checks],
            "case_count": len(cases), "scorer_counts": dict(Counter(case.scorer for case in cases.values())),
            "cases": [{"fixture_id": spec.workload["case_id"], "scorer": spec.scorer,
                       "weight": cases[spec.workload["case_id"]].weight,
                       "input_chars": spec.input_chars,
                       "requested": {"max_tokens": cases[spec.workload["case_id"]].max_tokens,
                                     "options": cases[spec.workload["case_id"]].options},
                       "effective": _effective(spec)} for spec in specs],
            "quiet_requests": quiet, "stages": stages,
            "planned_requests_per_target": planned, "known_selected_check_subtotal": planned,
            "per_target_request_quota": quotas, "request_cap": config.max_requests,
            "unique_effective_requests": len(effective_requests),
            "repeated_requests_per_target": max(0, planned - len(effective_requests)),
            "inapplicable_settings": ["correctness.samples", "correctness.concurrency"] if active else [],
            "identity": pack_identity(config), "judging": judging, "warnings": warnings,
            "input_retention_notice": INPUT_RETENTION_NOTICE}
