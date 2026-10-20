"""Shared on-disk and in-process contracts. No server or database is required."""

from __future__ import annotations

import json
from copy import deepcopy
import math
import re
from typing import Any, Literal

from pydantic_core import PydanticCustomError
from pydantic import ValidationError, StrictInt, field_validator, BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, StrictBool, TypeAdapter, ValidationInfo, field_serializer, model_serializer, model_validator

_LOAD_INTEGER = TypeAdapter(int)

Status = Literal["pass", "fail", "inconclusive", "skipped", "blocked"]
CHECK_NAMES = {
    "access": "Network and access",
    "serving": "Serving success",
    "first_output": "First useful output",
    "generation": "Generation pace",
    "capacity": "Arrival traffic capacity",
    "fairness": "Mixed long/short fairness",
    "context": "Long-context behavior",
    "correctness": "Known-answer correctness",
    "json": "Structured JSON",
    "cancellation": "Limits and cancellation",
    "recovery": "Sustained load and recovery",
    "gpu": "GPU observations",
}
CORE_CHECKS = tuple("concurrency" if name == "fairness" else name for name in CHECK_NAMES)
DEFAULT_CHECKS = tuple("concurrency" if name == "fairness" else name
                       for name in CORE_CHECKS if name not in {"json", "gpu"})
CHECK_NAMES.update({
    "arrivals": "Scheduled arrivals", "prefix": "Shared prefixes and cache evidence",
    "buckets": "Input/output and concurrency coverage", "mixed": "Mixed workload interference",
    "sessions": "Multi-turn sessions", "consistency": "Repeated-request consistency",
    "tools": "Tool-calling capability",
})
# Legacy titles remain readable for saved reports; execution uses public check ids.
INTERNAL_CHECK_NAMES = {**CHECK_NAMES, "token_stress": "Token stress cases",
                        "concurrency": "Concurrent load"}
CHECK_NAMES = {("concurrency" if key == "fairness" else key):
               ("Concurrent load" if key == "fairness" else title)
               for key, title in CHECK_NAMES.items() if key not in {"buckets", "mixed"}}
SESSION_REPEAT_PROMPT = "Repeat exactly the above answer."


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Deployment(Model):
    # Explicit categories; arbitrary imports are filtered by deployment.py.
    model: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    serving: dict[str, Any] = Field(default_factory=dict)
    backend: dict[str, Any] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)
    software: dict[str, Any] = Field(default_factory=dict)
    extension: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="configured endpoint; other replicas unverified", max_length=512)
    collected_at: str | None = None
    metadata_file: str | None = None
    launch_command: str | list[str] | None = None
    discovery: Literal["none", "ollama", "vllm", "sglang", "llamacpp", "tensorrt-llm", "lmdeploy", "tgi"] = "none"
    collector_url: str | None = None
    intended_change: str | None = None


class Arrival(Model):
    mode: Literal["steady", "burst"] = "steady"
    requests: int = Field(default=8, ge=2, le=10000)
    requests_per_second: float = Field(default=1, gt=0, le=1000)
    burst_size: int = Field(default=2, ge=1, le=256)
    interval_seconds: float = Field(default=1, gt=0)
    max_pending: int = Field(default=4, ge=1, le=256)
    max_client_wait_ms: float = Field(default=50, ge=0)
    seed: int = 0


class Prefix(Model):
    prefix_chars: int = Field(default=8192, ge=32, le=65536)
    repeats: int = Field(default=6, ge=2, le=100)
    history_turns: int = Field(default=3, ge=2, le=32)


SIZES = ("short", "medium", "long")
SHAPES = tuple(f"{a}_{b}" for a in SIZES for b in SIZES)


def load_shape_label(shape):
    a, b = shape.split("_")
    return f"{a.capitalize()} input → {b.capitalize()} output"


def load_validation_error(fields, message):
    raise ValidationError.from_exception_data("Concurrent load", [{
        "type": PydanticCustomError("concurrent_load", message, {"fields": fields}),
        "loc": tuple(fields[0].split(".")), "input": None}])


class LoadSize(Model):
    input_tokens: StrictInt = Field(ge=1, le=131072)
    output_tokens: StrictInt = Field(ge=1, le=65536)


class DataProfile(Model):
    short: LoadSize = Field(default_factory=lambda: LoadSize(input_tokens=128, output_tokens=32))
    medium: LoadSize = Field(default_factory=lambda: LoadSize(input_tokens=512, output_tokens=64))
    long: LoadSize = Field(default_factory=lambda: LoadSize(input_tokens=2048, output_tokens=128))

    @model_validator(mode="after")
    def ordered(self, info: ValidationInfo):
        if info.context and not info.context.get("active_load", True):
            return self
        for field in ("input_tokens", "output_tokens"):
            for previous, size in zip(SIZES, SIZES[1:]):
                if getattr(getattr(self, size), field) < getattr(getattr(self, previous), field):
                    label = "input target" if field == "input_tokens" else "output cap"
                    load_validation_error([f"{size}.{field}"], f"{size.capitalize()} {label} must be at least the {previous.capitalize()} {label} ({getattr(getattr(self, previous), field)} tokens).")
        return self


class LoadCombinations(Model):
    enabled: bool = True
    selected: list[str] = Field(default_factory=lambda: ["short_short", "long_short", "short_long"])

    @field_validator("selected")
    @classmethod
    def valid_shapes(cls, values):
        if len(values) != len(set(values)) or set(values) - set(SHAPES):
            raise ValueError("Choose each input/output combination at most once, using Short, Medium or Long sizes.")
        return values

    @model_validator(mode="after")
    def nonempty(self, info: ValidationInfo):
        if info.context and not info.context.get("active_load", True):
            return self
        if self.enabled and not self.selected:
            load_validation_error(["selected"], "Select at least one input/output combination.")
        return self


class LoadMixed(Model):
    enabled: bool = True
    shares: dict[str, float] = Field(default_factory=lambda: {"short_short": 60, "long_short": 20, "short_long": 20})
    seed: int | None = Field(default=None, ge=0, le=2**53 - 1)

    @field_validator("shares")
    @classmethod
    def valid_shares(cls, values):
        if set(values) - set(SHAPES) or any(not 0 <= v <= 100 for v in values.values()):
            raise ValueError("Share of requests must be between 0 and 100% for the nine input/output combinations.")
        return values

    @model_validator(mode="after")
    def total(self, info: ValidationInfo):
        if info.context and not info.context.get("active_load", True):
            return self
        if self.enabled and not math.isclose(sum(self.shares.values()), 100, abs_tol=1e-6, rel_tol=0):
            shape = next((shape for shape in SHAPES if self.shares.get(shape, 0)), SHAPES[0])
            load_validation_error([f"shares.{shape}"], "Share of requests must total 100%.")
        return self


class LoadInterference(Model):
    enabled: bool = False
    trigger: Literal["first_output", "after_chunks"] = "first_output"
    trigger_chunks: StrictInt = Field(default=5, ge=1, le=1000)
    max_slowdown: float | None = Field(default=1.5, gt=1)


class LoadStress(Model):
    enabled: bool = False
    large_input: bool = True
    large_output: bool = True
    min_output_percent: float = Field(default=80, ge=1, le=100)

    @model_validator(mode="after")
    def cases(self, info: ValidationInfo):
        if info.context and not info.context.get("active_load", True):
            return self
        if self.enabled and not (self.large_input or self.large_output):
            load_validation_error(["large_input"], "Select at least one stress workload.")
        return self


class LoadSchedule(Model):
    levels: list[StrictInt] = Field(default_factory=lambda: [1, 4], min_length=1, max_length=16)
    duration_seconds: StrictInt = Field(default=15, ge=1, le=3600)

    @field_validator("levels")
    @classmethod
    def valid_levels(cls, values):
        if len(values) != len(set(values)) or any(not 1 <= v <= 256 for v in values):
            raise ValueError("Concurrency levels must be distinct whole numbers from 1 to 256.")
        return sorted(values)


class LoadPassCriteria(Model):
    first_output_p95_ms: StrictInt | None = Field(default=None, ge=1)
    response_p95_ms: StrictInt | None = Field(default=None, ge=1)
    max_error_percent: float = Field(default=0, ge=0, le=100)
    min_correct_percent: float = Field(default=100, ge=0, le=100)
    min_valid_responses: StrictInt = Field(default=5, ge=2)


class ConcurrentLoad(Model):
    profile: DataProfile = Field(default_factory=DataProfile)
    combinations: LoadCombinations = Field(default_factory=LoadCombinations)
    mixed: LoadMixed = Field(default_factory=LoadMixed)
    interference: LoadInterference = Field(default_factory=LoadInterference)
    stress: LoadStress = Field(default_factory=LoadStress)
    schedule: LoadSchedule = Field(default_factory=LoadSchedule)
    pass_criteria: LoadPassCriteria = Field(default_factory=LoadPassCriteria)
    input_token_tolerance: float = Field(default=.1, ge=0, lt=.5)
    calibration_attempts: StrictInt = Field(default=3, ge=1, le=5)


class _LegacyBuckets(Model):
    input_chars: dict[str, int] = Field(default_factory=lambda: {"short": 64, "medium": 1024, "long": 12288})
    output_tokens: dict[str, int] = Field(default_factory=lambda: {"short": 16, "medium": 48, "long": 96})
    levels: list[int] = Field(default_factory=lambda: [1, 2])
    samples_per_bucket: int = Field(default=5, ge=2, le=100)
    window_seconds: float = Field(default=30, gt=0)
    mixed_pairs: int = Field(default=5, ge=1, le=100)
    heavy_weights: dict[str, int] = Field(default_factory=lambda: {"long_input": 1, "long_output": 1})

    @model_validator(mode="after")
    def valid_values(self):
        for values in (self.input_chars, self.output_tokens):
            if set(values) != set(SIZES) or any(not 1 <= v <= 65536 for v in values.values()):
                raise ValueError("Legacy buckets need short/medium/long integer sizes in 1..65536")
        if not self.levels or len(self.levels) > 16 or len(set(self.levels)) != len(self.levels) or any(not 1 <= level <= 256 for level in self.levels):
            raise ValueError("Legacy bucket levels must be distinct concurrency values in 1..256")
        if set(self.heavy_weights) != {"long_input", "long_output"} or any(not 1 <= weight <= 16 for weight in self.heavy_weights.values()):
            raise ValueError("Legacy heavy_weights need long_input/long_output weights in 1..16")
        return self


class _LegacyTokenStressCase(Model):
    name: Literal["long_input_summary", "short_input_long_output"]
    input_tokens: int | None = Field(default=None, ge=128, le=65536)
    max_input_tokens: int | None = Field(default=None, ge=1, le=65536)
    output_tokens: int = Field(ge=1, le=65536)
    min_output_tokens: int | None = Field(default=None, ge=1, le=65536)

    @model_validator(mode="after")
    def valid_lengths(self):
        if self.name == "long_input_summary" and self.input_tokens is None:
            raise ValueError("long_input_summary requires an input token target")
        if self.min_output_tokens is not None and self.min_output_tokens > self.output_tokens:
            raise ValueError("minimum observed output must not exceed the output cap")
        if self.name == "short_input_long_output" and self.input_tokens is not None:
            raise ValueError("short_input_long_output uses max_input_tokens, not an input token target")
        return self


class _LegacyConcurrentWorkloads(_LegacyBuckets):
    scenarios: list[Literal["size_matrix", "mixed_interference", "streaming_fairness", "token_stress"]] = Field(
        default_factory=lambda: ["size_matrix", "mixed_interference", "streaming_fairness", "token_stress"], min_length=1)
    input_token_tolerance: float = Field(default=.1, ge=0, lt=.5)
    calibration_attempts: int = Field(default=3, ge=1, le=5)
    stress_cases: list[_LegacyTokenStressCase] = Field(default_factory=lambda: [
        _LegacyTokenStressCase(name="long_input_summary", input_tokens=10000, output_tokens=50),
        _LegacyTokenStressCase(name="short_input_long_output", max_input_tokens=128, output_tokens=10000)])

    @model_validator(mode="after")
    def distinct_cases(self):
        if "token_stress" in self.scenarios and not self.stress_cases:
            raise ValueError("Select at least one legacy token stress case")
        if len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("Legacy concurrent workload scenarios must be distinct")
        if len({case.name for case in self.stress_cases}) != len(self.stress_cases):
            raise ValueError("Legacy token stress case names must be distinct")
        return self


class _LegacyLoadInput(Model):
    buckets: _LegacyBuckets | None = None
    concurrent_workloads: _LegacyConcurrentWorkloads | None = None


def migrate_concurrent_load(value):
    """Convert legacy input without persisting migration metadata in config."""
    if not isinstance(value, dict):
        return value, []
    value, notes = deepcopy(value), []
    for key in ("concurrent_workloads", "buckets"):
        if value.get(key) is None:
            value.pop(key, None)
    _LegacyLoadInput.model_validate({key: value[key].model_dump(exclude_unset=True) if isinstance(value[key], BaseModel) else value[key]
                                    for key in ("concurrent_workloads", "buckets") if key in value})
    for field in ("limits", "arrivals", "traffic"):
        if isinstance(value.get(field), BaseModel):
            value[field] = value[field].model_dump(exclude_unset=True)
    if isinstance(value.get("test_options"), dict):
        value["test_options"] = {k: v.model_dump(exclude_unset=True) if isinstance(v, BaseModel) else v for k, v in value["test_options"].items()}
    if "test_options" in value:
        _LegacyOptionsInput.model_validate({"test_options": value["test_options"]})

    legacy_checks = {"fairness": "streaming_fairness", "buckets": "size_matrix",
                     "mixed": "mixed_interference", "token_stress": "token_stress"}
    checks = value.get("checks")
    legacy = "concurrent_workloads" in value or "buckets" in value or any(c in legacy_checks for c in checks or [])
    options = value.get("test_options") or {}
    if not legacy and "concurrency" not in options:
        return value, notes
    if value.get("sustained_seconds", 5) is None:
        value["sustained_seconds"] = 5
    old = value.pop("concurrent_workloads", None) or {}
    buckets = value.pop("buckets", None) or {}
    old = old.model_dump(exclude_unset=True) if isinstance(old, BaseModel) else old
    buckets = buckets.model_dump(exclude_unset=True) if isinstance(buckets, BaseModel) else buckets
    old = {**buckets, **old}
    selected = [legacy_checks[c] for c in checks or [] if c in legacy_checks]
    scenarios = old.get("scenarios", selected or list(legacy_checks.values()))
    old = {"levels": [1, 2], "input_chars": {"short": 64, "medium": 1024, "long": 12288},
           "output_tokens": {"short": 16, "medium": 48, "long": 96}, "samples_per_bucket": 5,
           "mixed_pairs": 5, "heavy_weights": {"long_input": 1, "long_output": 1}, "window_seconds": 30, **old}
    if checks is not None:
        value["checks"] = list(dict.fromkeys("concurrency" if c in legacy_checks else c for c in checks))
    for name in ("concurrency", *legacy_checks):
        removed = options.pop(name, None)
        if removed:
            removed = removed.model_dump(exclude_unset=True) if isinstance(removed, BaseModel) else removed
            for field, actual in removed.items():
                fields = {f"limits.{key}": setting for key, setting in actual.items()} if field == "limits" and isinstance(actual, dict) else {field: actual}
                for key, setting in fields.items():
                    notes.append(f"test_options.{name}.{key}={setting!r} dropped; Concurrent load uses its own schedule and p95/percent criteria.")
    value["test_options"] = options
    if not legacy:
        return value, notes
    settings = ConcurrentLoad().model_dump()
    settings["mixed"]["enabled"] = False
    notes.append("Mixed load is new; enable it if wanted")
    def number(raw, default):
        try:
            result = float(raw)
            return result if math.isfinite(result) else default
        except (ValueError, TypeError):
            return default
    ceiling = max(1, int(number(value.get("concurrency", 4), 4)))
    targets = len(value.get("targets") or []) or 1
    ceiling = max(1, ceiling // targets) if value.get("overlap_models") else ceiling
    levels = old.get("levels", [1, 2])
    actual_levels = sorted(set(min(ceiling, max(1, int(n))) for n in levels))[:16] or [1]
    settings["schedule"]["levels"] = actual_levels
    if "levels" in old:
        notes.append(f"levels={levels!r} moved to schedule.levels={actual_levels!r} (bounded by Maximum at once).")
    settings["combinations"].update(enabled="size_matrix" in scenarios, selected=list(SHAPES))
    if "size_matrix" in scenarios:
        notes.append("size_matrix/buckets mapped to all 9 combinations; duration replaces sample counts.")
    for scenario in ("streaming_fairness", "mixed_interference"):
        if scenario not in scenarios:
            continue
        enabled = min(actual_levels) >= 2 and value.get("stream", True)
        settings["interference"]["enabled"] |= enabled
        notes.append(f"{scenario} ran at 2 concurrent requests; interference {'enabled' if enabled else 'left off; enable streaming and use only levels >= 2 to enable it'}.")
        if scenario == "mixed_interference":
            notes.append("mixed_interference long-input heavy partners map to interference; long-output heavy partners and before-output ordering have no equivalent, so that coverage is dropped.")
    cases = old.get("stress_cases", [{"name": "long_input_summary", "input_tokens": 10000, "output_tokens": 50},
                                    {"name": "short_input_long_output", "max_input_tokens": 128, "output_tokens": 10000}])
    if "token_stress" in scenarios:
        settings["stress"].update(enabled=bool(cases), large_input=any(c["name"] == "long_input_summary" for c in cases),
                                  large_output=any(c["name"] == "short_input_long_output" for c in cases))
        notes.append(f"token_stress stress_cases={cases!r} mapped to stress cases; old token targets, caps and min_output_tokens are not copied. Set the data profile and Minimum output length.")
    cap = max(1, int(number(value.get("max_output_tokens", 128), 128)))
    previous = 1
    for size in SIZES:
        old_cap = old.get("output_tokens", {}).get(size, settings["profile"][size]["output_tokens"])
        old_cap = int(number(old_cap, settings["profile"][size]["output_tokens"]))
        new_cap = max(previous, min(cap, old_cap))
        settings["profile"][size]["output_tokens"] = previous = new_cap
        if "output_tokens" in old or new_cap != old_cap:
            notes.append(f"output_tokens.{size}={old_cap} copied to profile.{size}.output_tokens={new_cap}" + (f"; clipped to max_output_tokens={cap}." if old_cap > cap else "; kept non-decreasing." if new_cap != old_cap else "."))
    if "input_chars" in old:
        notes.append(f"input_chars={old['input_chars']!r} not converted; inputs now use token targets, with default profile targets.")
    for field in ("samples_per_bucket", "mixed_pairs", "heavy_weights", "window_seconds"):
        if field in old:
            why = "the old window was a ceiling, not a duration" if field == "window_seconds" else "sample/pair counts and weights were stopping targets; the schedule is now duration-based"
            notes.append(f"{field}={old[field]!r} dropped; {why}.")
    for field in ("input_token_tolerance", "calibration_attempts"):
        if field in old:
            settings[field] = int(old[field]) if field == "calibration_attempts" else old[field]
            notes.append(f"{field}={old[field]!r} copied with the same meaning.")
    defaults = Limits().model_dump()
    for field in ("first_output_ms", "latency_ms", "stream_gap_ms", "min_output_tokens_per_second", "max_error_rate", "min_correctness", "min_samples", "fairness_max_ratio"):
        actual = (value.get("limits") or {}).get(field, defaults[field])
        if actual != defaults[field]:
            guidance = {"fairness_max_ratio": "set Maximum slowdown vs short-only control",
                        "stream_gap_ms": "there is no replacement criterion here",
                        "min_output_tokens_per_second": "output tokens/s has no replacement criterion here"}.get(field, "set the new p95/percent pass criteria explicitly")
            notes.append(f"limits.{field}={actual!r} no longer gates Concurrent load; {guidance}. It still applies to other checks as before.")
    windows = [(t.get("deployment", {}).get("serving", {}) if isinstance(t, dict) else t.deployment.serving).get("context_limit")
               for t in value.get("targets", [])]
    windows = [w.get("value") if isinstance(w, dict) else w for w in windows]
    windows = [w for w in windows if isinstance(w, (int, float)) and w > 0]
    if windows:
        window = min(windows)
        def fits(shape):
            a, b = shape.split("_")
            return settings["profile"][a]["input_tokens"] + settings["profile"][b]["output_tokens"] <= window
        selected_shapes = settings["combinations"]["selected"]
        settings["combinations"]["selected"] = [shape for shape in selected_shapes if fits(shape)]
        if settings["combinations"]["enabled"] and len(settings["combinations"]["selected"]) != len(selected_shapes):
            notes.append(f"Combinations exceeding known server context window {window} were turned off.")
            settings["combinations"]["enabled"] = bool(settings["combinations"]["selected"])
        if settings["interference"]["enabled"] and not all(fits(shape) for shape in ("short_short", "long_short")):
            settings["interference"]["enabled"] = False
            notes.append(f"Interference turned off: profile exceeds known server context window {window}.")
        if settings["stress"]["enabled"]:
            for case, shape in (("large_input", "long_short"), ("large_output", "short_long")):
                if settings["stress"][case] and not fits(shape):
                    settings["stress"][case] = False
                    notes.append(f"Stress {case} turned off: profile exceeds known server context window {window}.")
            settings["stress"]["enabled"] = settings["stress"]["large_input"] or settings["stress"]["large_output"]
    from giraffe.concurrent_load import load_time_plan
    configured = sum(row["configured_load_seconds"] for row in load_time_plan(ConcurrentLoad.model_validate(settings)).values())
    selected_checks = list(value.get("checks", DEFAULT_CHECKS))
    if checks is None:
        selected_checks.extend([*(["json"] if value.get("structured_json") else []),
                                *(["gpu"] if value.get("metrics") else [])])
        for field, check in (("arrivals", "arrivals"), ("prefix", "prefix"), ("sessions", "sessions"),
                             ("consistency", "consistency"), ("tool_calling", "tools"), ("tool_checks", "tools")):
            if value.get(field) is not None and (field != "tool_calling" or value[field]):
                selected_checks.append(check)
    fixed = 0
    if "capacity" in selected_checks:
        traffic = value.get("traffic") or {}
        fixed += len(traffic.get("rates", [1, 2, 4])) * traffic.get("duration_seconds", 5)
    if "recovery" in selected_checks:
        fixed += number(options.get("recovery", {}).get("sustained_seconds"), number(value.get("sustained_seconds", 5), 5))
    if "arrivals" in selected_checks and value.get("arrivals"):
        arrivals = value["arrivals"]
        count = arrivals.get("requests", 8) - 1
        fixed += count / arrivals.get("requests_per_second", 1) if arrivals.get("mode", "steady") == "steady" else (count // arrivals.get("burst_size", 2)) * arrivals.get("interval_seconds", 1)
    sequential = 1 if value.get("overlap_models") else targets
    remaining = number(value.get("max_duration_seconds", 300), 300) / sequential - fixed
    if configured > remaining and "concurrency" in selected_checks:
        duration = math.floor(10 * max(0, remaining) / configured)
        if duration >= 1:
            settings["schedule"]["duration_seconds"] = duration
            notes.append(f"Migrated duration reduced from 10 to {duration} seconds to fit max_duration_seconds={value.get('max_duration_seconds', 300)} with other fixed windows.")
        else:
            for kind in ("combinations", "mixed", "interference", "stress"):
                settings[kind]["enabled"] = False
            notes.append(f"Concurrent load turned off: max_duration_seconds={value.get('max_duration_seconds', 300)} cannot fit its minimum sending windows.")
    if value.get("overlap_models") and targets > int(number(value.get("concurrency", 4), 4)):
        for kind in ("combinations", "mixed", "interference", "stress"):
            settings[kind]["enabled"] = False
        notes.append(f"Concurrent load turned off: {targets} overlapping targets need at least {targets} request slots; concurrency={value.get('concurrency', 4)}.")
    # A legacy fairness-only selection may have no runnable equivalent.
    if not any(settings[k]["enabled"] for k in ("combinations", "mixed", "interference", "stress")):
        value["checks"] = list(dict.fromkeys(c for c in selected_checks if c != "concurrency"))
        notes.append("Concurrent load unselected because no migrated load type can run with these settings.")
    if "concurrent_load" not in value:
        value["concurrent_load"] = settings
    return value, notes


class SessionWorkload(Model):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    weight: float = Field(default=1, gt=0, le=10000)
    turns: list[str] = Field(min_length=2, max_length=32)
    max_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def bounded_turns(self):
        if any(not turn.strip() or len(turn) > 8192 for turn in self.turns):
            raise ValueError("workloads need non-empty user turns of at most 8192 characters")
        return self


def session_workload_presets() -> list[SessionWorkload]:
    return [
        SessionWorkload(name="coding", turns=[
            "Write a Python function that removes duplicates from a list while preserving order. Explain how it works.",
            "Extend your previous solution to handle unhashable items. Explain the trade-offs.",
            "Add tests for your previous solution, including empty input and repeated values. Explain each test.",
        ]),
        SessionWorkload(name="essay", turns=[
            "Write three paragraphs explaining the benefits and drawbacks of public transport in cities.",
            "Expand your previous discussion with a concrete example and a counterargument in three paragraphs.",
            "Revise your argument into three paragraphs aimed at a city council. Include a practical recommendation.",
        ]),
        SessionWorkload(name="chat", turns=[
            "Explain in a full paragraph how to plan a weekend picnic with friends.",
            "Using your previous plan, explain in a full paragraph how you would adapt it for rainy weather.",
            "Using our discussion, give a detailed checklist and explain the most important preparations.",
        ]),
    ]


class Sessions(Model):
    profile: Literal["correctness", "performance"] = "correctness"
    modes: list[Literal["fixed", "live"]] = Field(default_factory=lambda: ["fixed", "live"])
    sessions: int = Field(default=5, ge=1, le=32)
    concurrency: int = Field(default=2, ge=1, le=32)
    turns: list[str] = Field(default_factory=lambda: [
        "Suggest a name for a bakery. Reply with only the name, without quotation marks or a trailing full stop.",
        SESSION_REPEAT_PROMPT])
    saved_answers: list[str] = Field(default_factory=lambda: ["Golden Crust", "Golden Crust"],
        description="Retained for config compatibility; seeded name-recall histories use the session name at every turn.")
    delay_seconds: float = Field(default=0, ge=0, le=60)
    max_tokens: int = Field(default=32, ge=1)
    # Non-empty workloads select live performance conversations. Empty retains
    # the existing fixed/live exact-repeat correctness test and its saved config.
    workloads: list[SessionWorkload] = Field(default_factory=list, max_length=16)
    # Stop rather than inventing an answer or silently cropping required history.
    failure_policy: Literal["stop"] = "stop"

    @model_validator(mode="after")
    def valid_session(self):
        if self.profile == "performance":
            if not self.workloads:
                raise ValueError("select at least one performance workload")
            if self.modes != ["live"]:
                raise ValueError("mixed performance workloads require modes: [live]")
            names = [workload.name for workload in self.workloads]
            if len(names) != len(set(names)):
                raise ValueError("session workload names must be unique")
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("select distinct fixed/live session modes")
        if not 2 <= len(self.turns) <= 32 or any(not t.strip() or len(t) > 8192 for t in self.turns):
            raise ValueError("sessions need 2..32 non-empty bounded user turns")
        return self


class LogprobConsistency(Model):
    response_mode: Literal["complete"] = "complete"
    top_logprobs: int = Field(default=5, ge=0, le=20)
    strict: bool = False
    tolerance: float | None = Field(default=None, ge=0)
    support: Literal["discover", "required"] = "discover"
    priming_rounds: int = Field(default=0, ge=0, le=2)
    compare_levels: bool = True

    @model_validator(mode="after")
    def strict_tolerance(self):
        if self.strict and self.tolerance is None:
            raise ValueError("strict logprob consistency requires an explicit tolerance in logprob units")
        return self


class LogprobAlternative(Model):
    token: str
    bytes: list[int] | None
    token_id: int | None = None
    logprob: float


class LogprobToken(LogprobAlternative):
    position: int
    top_logprobs: list[LogprobAlternative]


class LogprobEvidence(Model):
    status: Literal["available", "missing", "invalid", "incomplete", "rejected"]
    reason: str
    requested_top_logprobs: int
    tokens: list[LogprobToken] = Field(default_factory=list)


class Consistency(Model):
    logprobs: LogprobConsistency | None = None
    repetitions: int = Field(default=5, ge=2, le=100,
        description="Text repetitions round up to full load groups; logprob mode uses exactly this many measured rounds.")
    levels: list[int] = Field(default_factory=lambda: [1, 2])
    shapes: list[Literal["short", "long"]] = Field(default_factory=lambda: ["short", "long"])
    prompts_per_shape: int = Field(default=3, ge=2, le=16)
    temperature: float = Field(default=0, ge=0, le=2)
    seed: int | None = None
    equality: Literal["bytes", "whitespace"] = "bytes"
    strict: bool = False

    @model_validator(mode="after")
    def valid_consistency(self):
        if not self.levels or len(self.levels) > 16 or len(set(self.levels)) != len(self.levels) or any(
            level < 1 or level > 256 for level in self.levels
        ) or not self.shapes or len(set(self.shapes)) != len(self.shapes):
            raise ValueError("consistency needs distinct bounded levels and shapes")
        return self


class Target(Model):
    # Supplied separately by the local UI; never part of config/schema/report serialization.
    _api_key: SecretStr | None = PrivateAttr(default=None)
    name: str = Field(min_length=1)
    url: str
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)
    route: Literal["direct", "gateway", "replica"] = "direct"
    parent: str | None = None
    identity: dict[str, str] = Field(default_factory=dict)
    metrics_url: str | None = None
    metrics_api_key_env: str | None = None
    metrics_headers_env: dict[str, str] = Field(default_factory=dict)
    metrics_profile: Literal["unknown", "vllm-v1", "vllm-legacy", "ollama", "sglang", "llamacpp", "tensorrt-llm", "lmdeploy", "tgi"] = "unknown"
    metrics_runtime_version: str | None = None
    instrumentation: Literal["unknown", "enabled", "disabled", "unsupported"] = "unknown"
    deployment: Deployment = Field(default_factory=Deployment)
    restart_command: list[str] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Target:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target url must start with http:// or https:// (for example http://localhost:11434/v1)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("use an endpoint URL without credentials, query or fragment")
        if self.metrics_url:
            metrics = urlsplit(self.metrics_url)
            if metrics.scheme not in {"http", "https"} or not metrics.hostname:
                raise ValueError("metrics_url must be an http(s) endpoint")
            if metrics.username or metrics.password or metrics.query or metrics.fragment:
                raise ValueError("metrics_url must not contain credentials, query or fragment")
        if self.route == "replica" and not self.parent:
            raise ValueError("replica targets require parent service name")
        if self.deployment.collector_url:
            collector = urlsplit(self.deployment.collector_url)
            if collector.scheme not in {"http", "https"} or not collector.hostname or any(
                (collector.username, collector.password, collector.query, collector.fragment)
            ):
                raise ValueError("collector_url must be an http(s) URL without credentials/query/fragment")
        from giraffe.deployment import sanitize
        self.identity = sanitize(self.identity)
        for category in ("model", "runtime", "serving", "backend", "hardware", "software", "extension"):
            setattr(self.deployment, category, sanitize(getattr(self.deployment, category)))
        self.deployment.scope = sanitize(self.deployment.scope)
        self.deployment.intended_change = sanitize(self.deployment.intended_change)
        if self.deployment.launch_command:
            from giraffe.deployment import safe_launch
            self.deployment.launch_command = safe_launch(self.deployment.launch_command)
        return self


class Limits(Model):
    first_output_ms: float | None = Field(default=None, gt=0)
    latency_ms: float | None = Field(default=None, gt=0)
    stream_gap_ms: float | None = Field(default=None, gt=0)
    min_output_tokens_per_second: float | None = Field(default=None, gt=0)
    max_error_rate: float = Field(default=0, ge=0, le=1)
    min_correctness: float = Field(default=1, ge=0, le=1)
    fairness_max_ratio: float | None = Field(default=None, gt=1)
    recovery_max_ratio: float | None = Field(default=None, gt=1)
    gpu_max_temperature_c: float | None = Field(default=None, gt=0)
    regression_percent: float = Field(default=20, ge=0)
    min_samples: int = Field(default=5, ge=2)


MAX_PLANNED_ARRIVALS = 100_000


class TrafficMix(Model):
    short: float = Field(default=.6, ge=0)
    long_input: float = Field(default=.2, ge=0)
    long_output: float = Field(default=.2, ge=0)

    @model_validator(mode="after")
    def normalize(self) -> TrafficMix:
        weights = (self.short, self.long_input, self.long_output)
        scale = max(weights)
        if scale <= 0:
            raise ValueError("traffic mix must contain a positive weight")
        if math.isclose(sum(weights), 1, rel_tol=0, abs_tol=1e-12):
            return self
        scaled = tuple(weight / scale for weight in weights)
        total = sum(scaled)
        self.short, self.long_input, self.long_output = (
            weight / total for weight in scaled
        )
        return self


class TrafficConfig(Model):
    rates: list[float] = Field(default_factory=lambda: [1, 2, 4], min_length=1, max_length=16)
    duration_seconds: float = Field(default=5, gt=0, le=3600)
    arrival: Literal["steady", "poisson"] = "steady"
    max_in_flight: int | None = Field(default=None, ge=1, le=256)
    seed: int = Field(default=42, ge=0)
    mix: TrafficMix = Field(default_factory=TrafficMix)
    scheduler_lag_tolerance_ms: float = Field(default=100, gt=0)
    drain_timeout_seconds: float = Field(default=30, gt=0)
    long_input_chars: int = Field(default=4096, ge=128, le=1_000_000)
    long_output_words: int = Field(default=32, ge=2, le=4096)

    @model_validator(mode="after")
    def validate_schedule(self) -> TrafficConfig:
        if any(rate <= 0 or rate > 10_000 for rate in self.rates):
            raise ValueError("traffic rates must be positive and at most 10000 requests/second")
        if any(left >= right for left, right in zip(self.rates, self.rates[1:])):
            raise ValueError("traffic rates must be unique and strictly increasing")
        if sum(self.rates) * self.duration_seconds > MAX_PLANNED_ARRIVALS:
            raise ValueError("traffic plan exceeds 100000 expected arrivals per target")
        return self


class CheckOptions(Model):
    samples: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1, le=256)
    context_limit: int | None = Field(default=None, ge=128)
    max_output_tokens: int | None = Field(default=None, ge=1)
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    sustained_seconds: float | None = Field(default=None, ge=0)
    metrics_max_age_seconds: float | None = Field(default=None, gt=0)
    cancel_after_ms: float | None = Field(default=None, gt=0)
    deadline_probe_ms: float | None = Field(default=None, gt=0)
    backend_verification: Literal["off", "required"] | None = None
    server_log: str | None = Field(default=None, max_length=4096)
    trace_file: str | None = Field(default=None, max_length=4096)
    cancel_after_chunks: int | None = Field(default=None, ge=1, le=128)
    cleanup_seconds: float | None = Field(default=None, gt=0, le=60)
    trace_wait_seconds: float | None = Field(default=None, gt=0, le=60)
    limits: Limits | None = None


class _LegacyOptionsInput(Model):
    test_options: dict[str, CheckOptions]


# These maps are shared by validation, execution and the local UI. A check only
# exposes settings that affect its workload or verdict.
CHECK_OPTION_FIELDS = {
    "access": {"request_timeout_seconds"},
    "serving": set(),
    "first_output": set(),
    "generation": {"samples", "max_output_tokens"},
    "capacity": set(),
    "fairness": {"samples", "context_limit"},
    "context": {"samples", "context_limit"},
    "correctness": {"samples", "concurrency"},
    "json": {"samples", "concurrency"},
    "cancellation": {"samples", "cancel_after_ms", "deadline_probe_ms", "backend_verification",
                     "trace_file", "server_log", "cancel_after_chunks", "cleanup_seconds", "trace_wait_seconds"},
    "recovery": {"samples", "concurrency", "sustained_seconds"},
    "gpu": {"metrics_max_age_seconds"},
}
_TRAFFIC_LIMITS = {"first_output_ms", "latency_ms", "stream_gap_ms",
                   "min_output_tokens_per_second", "max_error_rate", "min_correctness", "min_samples"}
CHECK_LIMIT_FIELDS = {
    "access": set(), "serving": {"max_error_rate"},
    "first_output": {"first_output_ms", "min_samples"},
    "generation": {"latency_ms", "stream_gap_ms", "min_output_tokens_per_second", "min_samples"},
    "capacity": _TRAFFIC_LIMITS,
    "fairness": {"fairness_max_ratio", "stream_gap_ms", "min_correctness"},
    "context": {"min_correctness"}, "correctness": {"min_correctness"},
    "json": {"min_correctness"}, "cancellation": set(),
    "recovery": (_TRAFFIC_LIMITS - {"min_samples"}) | {"recovery_max_ratio"}, "gpu": {"gpu_max_temperature_c"},
}

# Extended workloads have dedicated top-level configuration blocks.
for _check in CHECK_NAMES.keys() - CHECK_OPTION_FIELDS.keys():
    CHECK_OPTION_FIELDS[_check] = set()
    CHECK_LIMIT_FIELDS[_check] = set()
CHECK_OPTION_FIELDS.pop("concurrency", None)
CHECK_OPTION_FIELDS.pop("fairness", None)
CHECK_LIMIT_FIELDS.pop("concurrency", None)
CHECK_LIMIT_FIELDS.pop("fairness", None)


JUDGE_PROMPT_VERSION = "reference-correctness-v1"
JUDGE_VOTING_RULE = "complete-panel-strict-majority-v1"

MAX_REQUEST_PACK_BYTES = 1024 * 1024
MAX_REQUEST_PACK_CASES = 1000
_RESERVED_PACK_OPTIONS = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
_CREDENTIAL_OPTION = re.compile(
    r"(?i)(api[ _-]?key|authorization|password|passwd|credential|secret|access[ _-]?token|bearer|cookie)"
)


def validate_pack_options(options: dict[str, Any]) -> None:
    """Reject credential fields; leave request text and response schemas untouched."""
    if _RESERVED_PACK_OPTIONS.intersection(options):
        raise ValueError("options cannot override model/messages/stream/token budget/n")

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _CREDENTIAL_OPTION.search(key) or key.lower() in {
                    "token", "tokens", "headers", "env", "environment"
                }:
                    raise ValueError("credential-bearing request options are not allowed")
                # Field names inside output schemas describe answers, not credentials.
                if key != "schema":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(options)


def validated_json_schema(schema: dict[str, Any], expected: Any = None, *,
                          expected_provided: bool = False):
    """Build the shared local-only validator, optionally checking an explicit assertion."""
    from jsonschema import SchemaError, ValidationError
    from jsonschema.validators import validator_for
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    from referencing.exceptions import CannotDetermineSpecification, Unresolvable

    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = Registry().with_resource("urn:giraffe:request-pack", resource)

        def check(resource, resolver):
            if isinstance(resource.contents, dict):
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if key in resource.contents:
                        reference = resource.contents[key]
                        if not isinstance(reference, str) or not reference.startswith("#"):
                            raise ValueError("references must be local")
                        resolver.lookup(reference)
            for child in resource.subresources():
                check(child, resolver.in_subresource(child))

        check(resource, registry.resolver("urn:giraffe:request-pack"))
        checked = validator(schema, registry=registry)
        if expected_provided:
            checked.validate(expected)
        return checked
    except (SchemaError, ValidationError, Unresolvable, CannotDetermineSpecification,
            ValueError, TypeError, RecursionError) as error:
        # jsonschema and referencing errors can embed an entire private schema/answer.
        raise ValueError("invalid JSON schema, expected value, or local reference; no downloads") from error


class RequestPackMessage(Model):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def nonempty_text(self):
        if not self.content.strip():
            raise ValueError("message content must contain text")
        return self


class ToolContractModel(Model):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


def finite_tool_json(value: Any) -> int:
    """Validate arbitrary tool JSON without including private inputs in errors."""
    try:
        return len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ValueError("tool configuration must contain finite JSON values") from error


class ToolContentExpectation(ToolContractModel):
    mode: Literal["exact", "contains"] = "exact"
    value: str = Field(max_length=1_000_000)
    normalization: Literal["none", "whitespace"] = "none"

    @model_validator(mode="after")
    def validate_content(self):
        if self.mode == "contains" and not self.value.strip():
            raise ValueError("contains content expectation must contain non-empty text")
        return self


class ExpectedToolCall(ToolContractModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    arguments: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_arguments(self):
        finite_tool_json(self.arguments)
        return self


class ToolChoiceRule(ToolContractModel):
    mode: Literal["none", "auto", "required", "named"]
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    support: Literal["required", "discover"] = "required"
    max_calls: int = Field(default=16, ge=1, le=16, strict=True)
    parallel_tool_calls: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def named_choice(self):
        if (self.mode == "named") != (self.name is not None):
            raise ValueError("only named tool choice requires a function name")
        return self

    def request_options(self) -> dict[str, Any]:
        options = {"tool_choice": {"type": "function", "function": {"name": self.name}}
                   if self.mode == "named" else self.mode}
        if self.parallel_tool_calls is not None:
            options["parallel_tool_calls"] = self.parallel_tool_calls
        return options

    @property
    def call_limit(self) -> int:
        return 0 if self.mode == "none" else 1 if self.parallel_tool_calls is False else self.max_calls


class ToolExpectation(ToolContractModel):
    tools: list[dict[str, Any]] = Field(min_length=1, max_length=16)
    expected_calls: list[ExpectedToolCall] | None = Field(default=None, max_length=16)
    choice: ToolChoiceRule | None = None
    expected_content: ToolContentExpectation | None = None

    @model_validator(mode="after")
    def validate_tools(self):
        from jsonschema import ValidationError as SchemaValidationError
        from giraffe.tool_schema import bounded_json_tree, bounded_tool_schema

        finite_tool_json(self.tools)
        schemas = {}
        for tool in self.tools:
            function = tool.get("function")
            if tool.get("type") != "function" or not isinstance(function, dict):
                raise ValueError("tools require function definitions")
            if set(tool) - {"type", "function"} or set(function) - {"name", "description", "parameters", "strict"}:
                raise ValueError("unsupported tool function definition fields")
            if "description" in function and not isinstance(function["description"], str):
                raise ValueError("tool descriptions must be text")
            if "strict" in function and not isinstance(function["strict"], bool):
                raise ValueError("tool strict must be a boolean")
            name = function.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise ValueError("tool function names must contain 1..64 letters, digits, underscores or hyphens")
            if name in schemas:
                raise ValueError("tool function names must be unique")
            schema = function.get("parameters")
            if not isinstance(schema, dict):
                raise ValueError("tool function parameters require a JSON schema object")
            bounded_tool_schema(schema)
            schemas[name] = schema
        if self.choice is None and not self.expected_calls:
            raise ValueError("tool fixtures require expected_calls or an explicit choice rule")
        if self.choice:
            rule = self.choice
            if rule.mode == "named" and rule.name not in schemas:
                raise ValueError("named tool choice must reference a declared function")
            if self.expected_calls is not None:
                count = len(self.expected_calls)
                if count > rule.call_limit or (rule.mode in {"required", "named"} and count == 0):
                    raise ValueError("expected_calls contradict the tool choice count rule")
                if rule.mode == "named" and any(call.name != rule.name for call in self.expected_calls):
                    raise ValueError("expected_calls contradict the named tool choice")
        for call in self.expected_calls or []:
            if call.name not in schemas:
                raise ValueError("every expected function must exist in tools")
            if call.arguments is not None:
                bounded_json_tree(call.arguments)
                try:
                    bounded_tool_schema(schemas[call.name], instance=call.arguments).validate(call.arguments)
                except SchemaValidationError as error:
                    raise ValueError("expected tool arguments do not satisfy the bounded schema") from error
        return self


class ToolFixture(ToolExpectation):
    fixture_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    messages: list[RequestPackMessage] = Field(min_length=1, max_length=64)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_fixture(self):
        if not self.fixture_id.strip():
            raise ValueError("fixture_id must contain text")
        if {"tools", "tool_choice"}.intersection(self.options):
            raise ValueError("options cannot override tools/tool_choice")
        if self.choice and "parallel_tool_calls" in self.options:
            raise ValueError("configure parallel_tool_calls in the choice rule")
        finite_tool_json(self.options)
        validate_pack_options(self.options)
        return self


class ToolResultCall(ExpectedToolCall):
    arguments: dict[str, Any]
    result: Any
    outcome: Literal["success", "error"] = "success"

    @model_validator(mode="after")
    def bounded_result(self):
        from giraffe.tool_schema import bounded_json_tree
        if finite_tool_json(self.result) > 65536:
            raise ValueError("tool result must not exceed 64 KiB")
        bounded_json_tree(self.result)
        return self


class ToolConversationStep(ToolContractModel):
    expected_calls: list[ToolResultCall] = Field(min_length=1, max_length=16)
    user_message: str | None = Field(default=None, min_length=1, max_length=16384)


class ToolConversationFixture(ToolContractModel):
    fixture_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    messages: list[RequestPackMessage] = Field(min_length=1, max_length=64)
    tools: list[dict[str, Any]] = Field(min_length=1, max_length=16)
    steps: list[ToolConversationStep] = Field(min_length=1, max_length=4)
    final_content: list[ToolContentExpectation] = Field(min_length=1, max_length=16)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_conversation(self):
        from giraffe.tool_schema import tool_call_fingerprint
        satisfied = set()
        for step in self.steps:
            ToolExpectation(tools=self.tools, expected_calls=[
                ExpectedToolCall(name=call.name, arguments=call.arguments) for call in step.expected_calls])
            if step.user_message is not None:
                if not step.user_message.strip():
                    raise ValueError("conversation user_message must contain text")
                satisfied = set()
            for call in step.expected_calls:
                fingerprint = tool_call_fingerprint(call.name, call.arguments)
                if fingerprint in satisfied:
                    raise ValueError("a successful operation cannot repeat without a new user_message")
                if call.outcome == "success":
                    satisfied.add(fingerprint)
        if {"tools", "tool_choice", "parallel_tool_calls"}.intersection(self.options):
            raise ValueError("conversation options cannot override tool selection")
        finite_tool_json(self.options)
        validate_pack_options(self.options)
        marker = "{{observation_id}}"
        if not any(marker in json.dumps(call.result) for step in self.steps for call in step.expected_calls):
            raise ValueError("a conversation result must include {{observation_id}}")
        if not any(marker in item.value for item in self.final_content):
            raise ValueError("final_content must check {{observation_id}} from the result")
        if marker in json.dumps([m.model_dump() for m in self.messages]) or any(
                marker in (step.user_message or "") or marker in json.dumps(call.arguments)
                for step in self.steps for call in step.expected_calls):
            raise ValueError("observation marker is only allowed in results and final_content")
        return self


class ToolChecks(ToolContractModel):
    fixtures: list[ToolFixture] = Field(default_factory=list, max_length=16)
    conversations: list[ToolConversationFixture] = Field(default_factory=list, max_length=16)
    modes: list[Literal["complete", "stream"]] | None = Field(default=None, min_length=1, max_length=2)

    @model_validator(mode="before")
    @classmethod
    def bounded_input(cls, value):
        if isinstance(value, dict):
            values = dict(value)
            if isinstance(values.get("fixtures"), list):
                values["fixtures"] = [item.model_dump(mode="python") if isinstance(item, ToolFixture) else item
                                      for item in values["fixtures"]]
            if isinstance(values.get("conversations"), list):
                values["conversations"] = [item.model_dump(mode="python") if isinstance(item, ToolConversationFixture) else item
                                           for item in values["conversations"]]
            if finite_tool_json(values) > 65536:
                raise ValueError("tool_checks must not exceed 64 KiB")
        return value

    @model_validator(mode="after")
    def validate_checks(self):
        if finite_tool_json(self.model_dump(mode="python")) > 65536:
            raise ValueError("tool_checks must not exceed 64 KiB")
        ids = [fixture.fixture_id for fixture in [*self.fixtures, *self.conversations]]
        if len(ids) != len(set(ids)):
            raise ValueError("tool fixture IDs must be unique")
        if self.modes is not None and len(self.modes) != len(set(self.modes)):
            raise ValueError("tool modes must be unique")
        return self


class ToolCheckOutcome(ToolContractModel):
    status: Literal["pass", "fail", "inconclusive", "not_checked"]
    message: str


class ToolEvaluation(ToolContractModel):
    choice: ToolCheckOutcome | None = None
    arguments: ToolCheckOutcome | None = None
    task: ToolCheckOutcome | None = None
    support: Literal["observed", "rejected", "unverified"] | None = None
    protocol: ToolCheckOutcome
    calls: ToolCheckOutcome
    content: ToolCheckOutcome


class RequestPackCase(Model):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True,
                              serialize_by_alias=True)
    fixture_id: str = Field(min_length=1)
    messages: list[RequestPackMessage] = Field(min_length=1)
    weight: float = Field(default=1, gt=0)
    scorer: Literal["none", "exact", "contains", "json", "judge"]
    expected: Any = None
    rubric: str | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    max_tokens: int | None = Field(default=None, ge=1)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def serialize_case(self, handler):
        value = handler(self)
        if "expected" not in self.model_fields_set:
            value.pop("expected", None)
        elif self.expected is None:
            # Null is an assertion; ordinary default exclusion must not erase it.
            value["expected"] = None
        return value

    @model_validator(mode="after")
    def validate_case(self):
        if not self.fixture_id.strip():
            raise ValueError("fixture_id must contain text")
        if self.scorer in {"exact", "contains", "judge"} and (
            not isinstance(self.expected, str) or not self.expected.strip()
        ):
            raise ValueError("exact/contains/judge requires a non-empty expected answer")
        if self.scorer == "json":
            if self.schema_ is None:
                raise ValueError("json scorer requires a schema")
            validated_json_schema(self.schema_, self.expected,
                                  expected_provided="expected" in self.model_fields_set)
        elif self.schema_ is not None:
            raise ValueError("schema is only supported for the json scorer")
        if self.scorer == "none" and self.expected is not None:
            raise ValueError("unscored cases cannot have an expected answer")
        if self.rubric is not None:
            if self.scorer != "judge" or not self.rubric.strip():
                raise ValueError("rubric requires a judge scorer and non-empty text")
        validate_pack_options(self.options)
        return self


class RequestPack(Model):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    version: Literal[1]
    cases: list[RequestPackCase] = Field(min_length=1, max_length=MAX_REQUEST_PACK_CASES)

    @model_validator(mode="after")
    def validate_pack(self):
        ids = [case.fixture_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("request pack case fixture_id values must be unique")
        try:
            encoded = json.dumps(self.model_dump(by_alias=True, exclude_defaults=True),
                                 ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("request pack must contain finite JSON values") from error
        if len(encoded) > MAX_REQUEST_PACK_BYTES:
            raise ValueError("request pack exceeds 1 MiB")
        return self


class JudgeEndpoint(Model):
    """Shared endpoint draft; execution additionally requires all identity fields."""

    name: str = ""
    url: str = ""
    model: str = ""
    api_key_env: str | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_endpoint(self):
        from urllib.parse import urlsplit

        if self.url:
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or any(
                (parsed.username, parsed.password, parsed.query, parsed.fragment)
            ):
                raise ValueError("judge url must be an http(s) endpoint without credentials/query/fragment")
        references = [*self.headers_env.values(), *([self.api_key_env] if self.api_key_env else [])]
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in references):
            raise ValueError("judge credentials must name environment variables")
        return self


class JudgingConfig(Model):
    judges: list[JudgeEndpoint] = Field(default_factory=list, max_length=8)
    concurrency: int = Field(default=2, ge=1, le=256)
    request_timeout_seconds: float = Field(default=30, gt=0)
    max_duration_seconds: float = Field(default=300, gt=0)
    max_output_tokens: int = Field(default=256, ge=1)
    ca_bundle: str | None = None
    proxy: str | None = None

    @model_validator(mode="after")
    def validate_judging(self):
        from urllib.parse import urlsplit

        names = [judge.name for judge in self.judges if judge.name]
        if len(names) != len(set(names)):
            raise ValueError("judge names must be unique")
        if self.proxy:
            parsed = urlsplit(self.proxy)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or any(
                (parsed.username, parsed.password, parsed.query, parsed.fragment)
            ):
                raise ValueError("judge proxy must be a proxy URL without credentials/query/fragment")
        return self


class JudgeVote(Model):
    judge: str
    url: str
    model: str
    verdict: StrictBool | None = None
    status: Literal["completed", "error"]
    error_category: str | None = None
    reason: str | None = None
    observed_model: str | None = None


class RequestJudgement(Model):
    status: Literal["pass", "fail", "inconclusive"]
    votes: list[JudgeVote] = Field(default_factory=list)


class RunConfig(Model):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    targets: list[Target] = Field(min_length=1)
    concurrency: int = Field(default=4, ge=1, le=256,
                             description="Maximum simultaneous target requests across the run, chosen by the user; not detected hardware capacity.")
    max_requests: int = Field(default=300, ge=1,
                              description="Total target request budget shared across all tests and targets; independent of concurrency and separate from AI judge calls.")
    max_duration_seconds: float = Field(default=300, gt=0)
    max_memory_mb: int = Field(default=4096, ge=0,
        description="Memory limit (RAM growth during this run) in MiB, sampled about once per second; 0 disables. Exceeding it stops unfinished work with partial evidence.")
    request_timeout_seconds: float = Field(default=30, gt=0)
    readiness_timeout_seconds: float = Field(default=180, gt=0)
    max_output_tokens: int = Field(default=128, ge=1)
    context_limit: int = Field(default=4096, ge=128)
    samples: int = Field(default=6, ge=1)
    sustained_seconds: float = Field(default=5, ge=0)
    stop_after_errors: int = Field(default=20, ge=1)
    unresponsive_after_failures: int | None = Field(default=5, ge=0,
        description="Stop a target after this many consecutive no-body inference failures and unresponsive_for_seconds of silence. 0 or null disables. Readiness and intentional deadlines/cancellations are excluded; stress timeouts do not count.")
    unresponsive_for_seconds: float = Field(default=45, gt=0,
        description="Minimum seconds without response body data before the target stops (default 45s; 5 failures). Body bytes, including error bodies and partial streams, reset the policy; headers alone do not.")
    checks: list[str] = Field(default_factory=lambda: list(DEFAULT_CHECKS))
    structured_json: bool = False
    metrics: bool = False
    metrics_interval_seconds: float = Field(default=1, ge=.1, le=60)
    gpu_min_observation_seconds: float = Field(default=30, ge=0,
        description="Minimum GPU observation window; match exporter refresh, independently of scrape interval. Bounded by run duration.")
    metrics_timeout_seconds: float = Field(default=2, gt=0, le=10)
    metrics_max_samples: int = Field(default=256, ge=2, le=4096)
    metrics_max_series: int = Field(default=128, ge=1, le=1024)
    cache_pressure_threshold: float = Field(default=.9, gt=0, le=1)
    arrivals: Arrival | None = None
    prefix: Prefix | None = None
    concurrent_load: ConcurrentLoad = Field(default_factory=ConcurrentLoad)
    _migration_notes: list[str] = PrivateAttr(default_factory=list)
    sessions: Sessions | None = None
    consistency: Consistency | None = None
    tool_calling: bool = False
    forced_tool_diagnostic: bool = False
    tool_checks: ToolChecks | None = None
    metrics_max_age_seconds: float = Field(default=60, gt=0)
    overlap_models: bool = False
    stream: bool = True
    request_options: dict[str, Any] = Field(default_factory=dict)
    custom_fixtures: str | None = None
    request_source: Literal["builtin", "request_pack"] = "builtin"
    request_pack: RequestPack | None = None
    judging: JudgingConfig = Field(default_factory=JudgingConfig)
    retention: Literal["all", "failures", "none"] = "failures"
    ca_bundle: str | None = None
    proxy: str | None = None
    restart_target: str | None = None
    limits: Limits = Field(default_factory=Limits)
    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    test_options: dict[str, CheckOptions] = Field(default_factory=dict)

    @model_validator(mode="wrap")
    @classmethod
    def migration_metadata(cls, value, handler):
        migrated, notes = migrate_concurrent_load(value)
        resolved = handler(migrated)
        if notes:
            resolved._migration_notes = notes
        return resolved

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_load(cls, value):
        value = migrate_concurrent_load(value)[0]
        if not isinstance(value, dict):
            return value
        load = value.get("concurrent_load", {})
        if isinstance(load, BaseModel):
            load = load.model_dump(exclude_unset=True)
        if not isinstance(load, dict):
            return value
        load = deepcopy(load)
        def integer(field, default):
            try:
                return _LOAD_INTEGER.validate_python(value.get(field, default))
            except ValidationError:
                return None
        schedule = load.get("schedule", {})
        if isinstance(schedule, BaseModel):
            schedule = schedule.model_dump(exclude_unset=True)
        if isinstance(schedule, dict) and "levels" not in schedule:
            ceiling = integer("concurrency", 4)
            if ceiling is not None and ceiling >= 1:
                targets = len(value.get("targets") or []) or 1
                ceiling = max(1, ceiling // targets) if value.get("overlap_models") else ceiling
                load["schedule"] = {**schedule, "levels": [n for n in (1, 4) if n <= ceiling] or [1]}
        profile = load.get("profile", {})
        if isinstance(profile, BaseModel):
            profile = profile.model_dump(exclude_unset=True)
        cap = integer("max_output_tokens", 128)
        if isinstance(profile, dict) and cap is not None and cap >= 1:
            defaults = DataProfile().model_dump()
            explicit = {}
            for size in SIZES:
                supplied = profile.get(size, {})
                if isinstance(supplied, BaseModel):
                    supplied = supplied.model_dump(exclude_unset=True)
                if isinstance(supplied, dict):
                    profile[size] = {**defaults[size], **supplied}
                    if "output_tokens" in supplied:
                        explicit[size] = supplied["output_tokens"]
                else:
                    profile[size] = supplied
            for index, size in enumerate(SIZES):
                if size in explicit or not isinstance(profile[size], dict):
                    continue
                low = max([1, *(explicit[s] for s in SIZES[:index] if type(explicit.get(s)) is int)])
                high = min([cap, *(explicit[s] for s in SIZES[index + 1:] if type(explicit.get(s)) is int)])
                profile[size]["output_tokens"] = max(low, min(defaults[size]["output_tokens"], high))
            load["profile"] = profile
        return {**value, "concurrent_load": load}

    @field_validator("concurrent_load", mode="before")
    @classmethod
    def validate_load_relationships(cls, value, info: ValidationInfo):
        return ConcurrentLoad.model_validate(value, context={"active_load": "concurrency" in info.data.get("checks", DEFAULT_CHECKS)})

    @model_validator(mode="after")
    def validate_run(self) -> RunConfig:
        if self.request_source == "request_pack":
            self.validate_request_pack_presence()
            if self.custom_fixtures:
                raise ValueError("request_pack cannot be combined with custom_fixtures")
            validate_pack_options(self.request_options)
        if "checks" not in self.model_fields_set:
            self.checks = [*self.checks, *(["json"] if self.structured_json else []),
                           *(["gpu"] if self.metrics else [])]
            for field, check in (("arrivals", "arrivals"), ("prefix", "prefix"),
                                 ("sessions", "sessions"),
                                 ("consistency", "consistency"), ("tool_calling", "tools"),
                                 ("tool_checks", "tools")):
                if getattr(self, field):
                    self.checks.append(check)
        self.validate_active_judges()
        self.checks = list(dict.fromkeys(self.checks))
        self.structured_json, self.metrics = "json" in self.checks, "gpu" in self.checks
        self.tool_calling = "tools" in self.checks
        if (self.tool_calling and self.tool_checks and self.tool_checks.modes
                and "stream" in self.tool_checks.modes and not self.stream):
            raise ValueError("tool_checks stream mode requires global stream=true")
        # Selecting a workload in the UI runs its defaults. Retain unselected
        # settings for later edits without silently selecting their checks.
        if "arrivals" in self.checks and self.arrivals is None:
            self.arrivals = Arrival()
        if "prefix" in self.checks and self.prefix is None:
            self.prefix = Prefix()
        if "sessions" in self.checks and self.sessions is None:
            self.sessions = Sessions(concurrency=min(2, self.concurrency),
                                     max_tokens=min(32, self.max_output_tokens))
        if "consistency" in self.checks and self.consistency is None:
            self.consistency = Consistency(
                levels=[level for level in (1, 2) if level <= self.concurrency])
        names = [t.name for t in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("target names must be unique")
        if set(self.checks) - set(CHECK_NAMES):
            raise ValueError("Unknown check name(s): " + ", ".join(sorted(set(self.checks) - set(CHECK_NAMES)))
                             + ". Valid names: " + ", ".join(CHECK_NAMES))
        if ("capacity" in self.checks and self.traffic.max_in_flight is not None
                and self.traffic.max_in_flight > self.concurrency):
            raise ValueError(f"traffic.max_in_flight must not exceed global concurrency ({self.concurrency})")
        for check_id, options in self.test_options.items():
            if check_id not in CHECK_OPTION_FIELDS:
                raise ValueError(f"unsupported test_options check: {check_id}")
            fields = options.model_fields_set - {"limits"}
            unsupported = fields - CHECK_OPTION_FIELDS[check_id]
            limit_fields = options.limits.model_fields_set if options.limits else set()
            unsupported |= {f"limits.{field}" for field in limit_fields - CHECK_LIMIT_FIELDS[check_id]}
            if unsupported:
                raise ValueError(f"unsupported options for {check_id}: {', '.join(sorted(unsupported))}")
            if check_id in self.checks:
                for field in {"concurrency", "max_output_tokens", "request_timeout_seconds"}:
                    value = getattr(options, field)
                    if value is not None and value > getattr(self, field):
                        raise ValueError(f"test_options.{check_id}.{field} must not exceed global "
                                         f"{field} ({getattr(self, field)})")
        for target in self.targets:
            if target.parent and target.parent not in names:
                raise ValueError(f"replica parent {target.parent!r} is not a configured service")
            if target.parent == target.name:
                raise ValueError("a target cannot be its own parent")
        if self.restart_target and self.restart_target not in names:
            raise ValueError("restart_target must name one configured target")
        reserved = {"model", "messages", "stream", "max_tokens", "max_completion_tokens", "n"}
        if reserved.intersection(self.request_options):
            raise ValueError("request_options cannot override model/messages/stream/token budget/n")
        if self.proxy:
            from urllib.parse import urlsplit

            proxy = urlsplit(self.proxy)
            if proxy.username or proxy.password:
                raise ValueError("put proxy credentials in HTTPS_PROXY/HTTP_PROXY, not config")
        if "consistency" in self.checks and max(self.consistency.levels) > self.concurrency:
            raise ValueError(f"consistency.levels must not exceed concurrency ({self.concurrency})")
        if "concurrency" in self.checks:
            load = self.concurrent_load
            prefix = "concurrent_load."
            def invalid(fields, message):
                load_validation_error(fields, message)
            if not any(getattr(load, kind).enabled for kind in ("combinations", "mixed", "interference", "stress")):
                invalid([prefix + "combinations.enabled"], "Enable at least one load type.")
            allocated = max(load.schedule.levels) * (len(self.targets) if self.overlap_models else 1)
            if allocated > self.concurrency:
                level = str(max(load.schedule.levels)) + (f" × {len(self.targets)} endpoints" if self.overlap_models and len(self.targets) > 1 else "")
                invalid([prefix + "schedule.levels", "concurrency"], f"Concurrency level {level} exceeds Maximum at once ({self.concurrency}).")
            for size in SIZES:
                if getattr(load.profile, size).output_tokens > self.max_output_tokens:
                    invalid([prefix + f"profile.{size}.output_tokens", "max_output_tokens"], f"{size.capitalize()} output cap {getattr(load.profile, size).output_tokens} tokens exceeds Output cap ({self.max_output_tokens} token{'s' if self.max_output_tokens != 1 else ''}).")
            if load.interference.enabled:
                if not self.stream:
                    invalid([prefix + "interference.enabled", "stream"], "Long input during an active short stream needs streaming responses. Turn on Streaming.")
                if min(load.schedule.levels) < 2:
                    invalid([prefix + "schedule.levels", prefix + "interference.enabled"], "Long input during an active short stream needs at least 2 concurrent requests at every level. Remove level 1 from Concurrency levels.")
            from giraffe.concurrent_load import running_shapes, context_window
            for target in self.targets:
                window, source = context_window(target)
                if window is not None:
                    for shape in running_shapes(load):
                        input_size, output_size = shape.split("_")
                        if getattr(load.profile, input_size).input_tokens + getattr(load.profile, output_size).output_tokens > window:
                            invalid([prefix + f"profile.{input_size}.input_tokens"], f"{load_shape_label(shape)} needs more than the known server context window ({window} token{'s' if window != 1 else ''}).")
        if "sessions" in self.checks and self.sessions:
            if self.sessions.concurrency > self.concurrency:
                raise ValueError("session concurrency must not exceed the global ceiling")
            if self.sessions.max_tokens > self.max_output_tokens:
                raise ValueError("session output budget must stay within max_output_tokens")
            if any(workload.max_tokens is not None and workload.max_tokens > self.max_output_tokens
                   for workload in self.sessions.workloads):
                raise ValueError("session workload output budgets must stay within max_output_tokens")
        if "concurrency" in self.checks:
            from giraffe.planning import request_plan
            errors = request_plan(self)["errors"]
            if errors:
                load_validation_error(errors[0]["fields"], errors[0]["msg"])
        from giraffe.deployment import sanitize
        if self.request_source != "request_pack":
            self.request_options = sanitize(self.request_options)
        return self

    def validate_request_pack_presence(self) -> None:
        if self.request_pack is None:
            raise ValueError("request_pack is required for the selected request source")

    def validate_active_judges(self) -> None:
        if (self.request_source == "request_pack" and self.request_pack
                and {"capacity", "correctness"}.intersection(self.checks)
                and any(case.scorer == "judge" for case in self.request_pack.cases)):
            if not self.judging.judges or any(
                not all(value.strip() for value in (judge.name, judge.url, judge.model))
                for judge in self.judging.judges
            ):
                raise ValueError("judge scoring requires at least one judge with a name, URL and model")

    def effective_check(self, check_id: str) -> dict[str, Any]:
        """Resolve sparse overrides once, retaining global safety ceilings."""
        if check_id not in INTERNAL_CHECK_NAMES:
            raise ValueError(f"unsupported check: {check_id}")
        if check_id == "concurrency":
            return self.concurrent_load.model_dump(mode="json")
        options = self.test_options.get(check_id, CheckOptions())
        special_defaults = {"backend_verification": "off", "cancel_after_ms": 100, "deadline_probe_ms": 50,
                            "cancel_after_chunks": 5, "cleanup_seconds": 5,
                            "trace_wait_seconds": 10}
        resolved = {}
        for field in sorted(CHECK_OPTION_FIELDS[check_id]):
            default = special_defaults.get(field, getattr(self, field, None))
            override = getattr(options, field)
            value = default if override is None else override
            if field in {"concurrency", "max_output_tokens", "request_timeout_seconds"}:
                value = min(value, getattr(self, field))
            resolved[field] = value
        limits = self.limits.model_dump()
        if options.limits:
            limits.update(options.limits.model_dump(exclude_unset=True))
        resolved["limits"] = limits
        return resolved

    def effective_traffic(self) -> TrafficConfig:
        return self.traffic.model_copy(update={
            "max_in_flight": min(self.traffic.max_in_flight or self.concurrency, self.concurrency),
        })

    @field_serializer("test_options")
    def serialize_test_options(self, options: dict[str, CheckOptions]) -> dict[str, Any]:
        return {name: value.model_dump(exclude_unset=True) for name, value in options.items()}


class DraftTarget(Target):
    """An endpoint being configured may not have a model selected yet."""

    model: str


class ConfigDraft(RunConfig):
    """Editable configuration; validate as RunConfig before any execution."""

    targets: list[DraftTarget] = Field(min_length=1)
    checks: list[str] = Field(default_factory=lambda: list(DEFAULT_CHECKS))


    def validate_request_pack_presence(self) -> None:
        # Selecting an empty dataset is a valid editing state, never a runnable one.
        pass

    def validate_active_judges(self) -> None:
        # Preview/export retain incomplete judge endpoint drafts without inference.
        pass


class RequestSpec(Model):
    condition_id: str | None = None
    fixture_id: str
    scenario: str
    check_ids: list[str]
    messages: list[dict[str, Any]]
    scorer: Literal["none", "exact", "contains", "json", "tool", "judge", "label"] = "none"
    expected: Any = None
    expected_provided: bool = False
    rubric: str | None = None
    tool_expectation: ToolExpectation | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    cancel_after_ms: float | None = Field(default=None, gt=0)
    cancel_after_chunks: int | None = Field(default=None, ge=1, le=128)
    context_position: str | None = None
    input_chars: int = 0
    request_timeout_seconds: float | None = Field(default=None, gt=0)
    load_point: dict[str, Any] | None = None
    requested_input_tokens: int | None = None
    requested_output_tokens: int | None = None
    workload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_tool_expectation(self):
        if self.tool_expectation is not None and self.scorer != "tool":
            raise ValueError("tool_expectation requires the tool scorer")
        if self.scorer == "tool" and (self.expected is not None or self.schema_ is not None or self.rubric is not None):
            raise ValueError("tool scoring requires tool_expectation instead of expected/schema/rubric")
        return self


class EvaluationEvidence(Model):
    """The actual input and answer check used for one request; never credentials."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True, serialize_by_alias=True)
    messages: list[dict[str, Any]]
    scorer: str
    expected: Any = None
    expected_provided: bool = False
    rubric: str | None = None
    tool_expectation: ToolExpectation | None = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


# Coarse bursts cannot establish a reliable answer-token generation rate.
MAX_TOKENS_PER_READ = 16


class RequestRecord(Model):
    _timing_started_monotonic: float | None = PrivateAttr(default=None)
    id: str
    target: str
    fixture_id: str
    scenario: str
    condition_id: str | None = None
    intentional: bool = False
    check_ids: list[str]
    started_at: str
    status: Literal["completed", "failed", "timeout", "cancelled"]
    http_status: int | None = None
    response_body_bytes: int = Field(default=0, ge=0)
    error: str | None = None
    server_request_id: str | None = None
    client_cancelled_at_ns: int | None = None
    client_cancelled_monotonic_ns: int | None = None
    client_cancelled_ms: float | None = None
    elapsed_ms: float = 0
    stream_end_ms: float | None = None
    first_output_ms: float | None = None
    last_output_ms: float | None = None
    answer_chunks: int = 0
    generation_chunks: int = 0
    answer_reads: int = 0
    answer_chars: int = 0
    first_read_answer_chars: int = 0
    first_reasoning_ms: float | None = None
    max_stream_gap_ms: float | None = None
    completion_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    input_tokens: int | None = None
    output_chars: int = 0
    output: str = ""
    reasoning: str = ""
    reasoning_stripped: bool = False
    chunks: int = 0
    stream_terminated: bool = False
    finish_reason: str | None = None
    valid: bool = False
    score: bool | None = None
    score_message: str | None = None
    evaluation: EvaluationEvidence | None = None
    evaluation_error: str | None = None
    logprob_evidence: LogprobEvidence | None = None
    tool_evaluation: ToolEvaluation | None = None
    tool_request_options: dict[str, Any] | None = None
    assistant_message: dict[str, Any] | None = None
    tool_conversation: dict[str, Any] | None = None
    response_events: list[dict[str, Any]] = Field(default_factory=list)
    response_events_truncated: bool = False
    judgement: RequestJudgement | None = None
    observed_model: str | None = None
    backend_id: str | None = None
    input_chars: int = 0
    requested_max_tokens: int = 0
    stream: bool = True
    traffic_stage: int | None = None
    traffic_rate_rps: float | None = None
    traffic_class: str | None = None
    scheduled_offset_ms: float | None = None
    dispatch_lag_ms: float | None = None
    arrival_first_output_ms: float | None = None
    arrival_elapsed_ms: float | None = None

    completed_at: str | None = None
    dispatch_ms: float | None = None
    completed_ms: float | None = None
    timing_start_ms: float | None = None
    client_preparation_ms: float = 0
    scheduled_ms: float | None = None
    client_wait_ms: float | None = None
    overlap: int = 1
    load_point: dict[str, Any] | None = None
    requested_input_tokens: int | None = None
    requested_output_tokens: int | None = None
    workload: dict[str, Any] = Field(default_factory=dict)
    answer_hash: str | None = None
    request_hash: str | None = None
    history_hash: str | None = None
    cached_prompt_tokens: int | None = None
    cache_source: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def stream_gap_applicable(self) -> bool:
        return not (self.stream and self.answer_reads <= 1 and self.completion_tokens in (1, 2))

    @property
    def first_read_tokens(self) -> float | None:
        chars = self.answer_chars or (self.output_chars if not self.reasoning_stripped else 0)
        if self.output_tokens is None or chars <= 0 or self.first_read_answer_chars <= 0:
            return None
        return max(1.0, self.output_tokens * self.first_read_answer_chars / chars)

    @property
    def measured_stream_gap(self) -> bool:
        first = self.first_read_tokens
        return (self.stream and self.answer_reads >= 2 and self.max_stream_gap_ms is not None
                and first is not None and first <= MAX_TOKENS_PER_READ)

    @property
    def generation_tokens_per_second(self) -> float | None:
        """Client-observed estimate; streamed reads are not individual tokens.

        Exclude tokens in the first answer read and time before it. Waiting for
        usage, finish markers or stream cleanup must not slow generation.
        Single-read, non-streamed, buffered (> MAX_TOKENS_PER_READ per read) or
        reasoning answers without a reported reasoning token count lack this evidence.
        """
        reads = self.answer_reads
        if (not self.valid or self.status != "completed" or not self.stream
                or self.output_tokens is None or self.output_tokens <= 1
                or reads < 2 or self.first_output_ms is None
                or self.last_output_ms is None or self.last_output_ms <= self.first_output_ms
                or ((self.reasoning_stripped or self.reasoning.strip()) and not self.reasoning_tokens)):
            return None
        first = self.first_read_tokens
        if first is None or first > MAX_TOKENS_PER_READ:
            return None
        later = self.output_tokens - first
        if later <= 0 or later / (reads - 1) > MAX_TOKENS_PER_READ:
            return None
        return later * 1000 / (self.last_output_ms - self.first_output_ms)


class CheckResult(Model):
    id: str
    target: str
    title: str
    status: Status
    required: bool = True
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class RunReport(Model):
    schema_version: str = "3"
    suite_version: str = "0.1.0"
    run_id: str
    started_at: str
    finished_at: str
    overall: Status
    manifest: dict[str, Any]
    checks: list[CheckResult] = Field(default_factory=list)
    requests: list[RequestRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    observations: dict[str, Any] = Field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    abort_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def rehydrate_inputs(cls, value):
        if not isinstance(value, dict) or "report_format" not in value:
            return value
        if value["report_format"] != 2 or not isinstance(value.get("inputs"), dict):
            raise ValueError("Unsupported saved report format")
        from copy import deepcopy

        value = dict(value)
        value.pop("report_format")
        inputs = value.pop("inputs")
        from .report_inputs import verify_input

        for reference, messages in inputs.items():
            verify_input(reference, messages)
        records = value.get("requests", [])
        if not isinstance(records, list):
            raise ValueError("Saved requests must be an array")
        requests = []
        for record in records:
            if isinstance(record, dict) and "input_ref" in record:
                record = dict(record)
                reference = record.pop("input_ref")
                if (not isinstance(reference, str) or reference not in inputs
                        or not isinstance(inputs[reference], list)
                        or not isinstance(record.get("evaluation"), dict)
                        or "messages" in record["evaluation"]):
                    raise ValueError("Invalid saved input reference")
                record["evaluation"] = {**record["evaluation"],
                                        "messages": deepcopy(inputs[reference])}
            requests.append(record)
        value["requests"] = requests
        return value


def overall_status(checks: list[CheckResult], abort_reason: str | None = None, *,
                   memory_limit_interrupted: bool = False) -> Status:
    required = [check for check in checks if check.required]
    if any(check.status == "fail" for check in checks):
        return "fail"
    if memory_limit_interrupted:
        return "inconclusive"
    if any(check.status == "blocked" for check in required):
        return "blocked"
    if abort_reason or not required or any(check.status == "inconclusive" for check in required):
        return "inconclusive"
    if all(check.status == "skipped" for check in required):
        return "skipped"
    return "pass"
