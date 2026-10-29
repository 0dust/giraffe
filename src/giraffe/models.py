"""Shared on-disk and in-process contracts. No server or database is required."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
import math
import re
from typing import Annotated, Any, Literal

from pydantic_core import PydanticCustomError
from pydantic import BeforeValidator, TypeAdapter, ValidationError, StrictInt, field_validator, BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, StrictBool, ValidationInfo, field_serializer, model_serializer, model_validator


from .caps import (MAX_SIMULTANEOUS_REQUESTS, MAX_ARRIVAL_REQUESTS, MAX_BURST_SIZE,
                   MAX_PENDING_ARRIVALS, MAX_TRAFFIC_RATE, MAX_ARRIVAL_RATE, MAX_STAGES,
                   MAX_STAGE_SECONDS, MAX_SESSIONS,
                   MAX_SESSION_TURNS, MAX_SESSION_TURN_CHARS, MAX_TOOL_CONFIG_BYTES, MAX_TOOL_RESULT_BYTES,
                   MAX_TOOL_CALLS, MAX_TOOL_NAME_CHARS, MAX_DEPLOYMENT_SCOPE_CHARS, MAX_PLANNED_ARRIVALS, MAX_REQUEST_PACK_BYTES, MAX_REQUEST_PACK_CASES,
                   MAX_SANITIZE_ITEMS, MAX_SANITIZE_STRING_CHARS)


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
DEFAULT_CHECKS = ("access", "serving", "correctness", "first_output")
# Saved pre-v2 files with no selection retain their historical workload.
_LEGACY_DEFAULT_CHECKS = ("access", "serving", "first_output", "generation", "load",
                          "context", "correctness", "cancellation", "recovery")
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
# Current check-list names for actionable diagnostics. Saved report and request
# breakdown titles above stay stable for comparisons with earlier runs.
CATALOGUE_NAMES = {
    "access": "Endpoint response", "serving": "Invalid responses",
    "correctness": "Answer correctness", "json": "JSON correctness",
    "context": "Context recall", "tools": "Tool-call correctness",
    "first_output": "First answer", "generation": "Generation",
    "capacity": "Capacity under load", "concurrency": "Concurrent load",
    "arrivals": "Arrival traffic", "recovery": "Recovery after load",
    "sessions": "Concurrent conversations", "consistency": "Answer consistency",
    "cancellation": "Cancellation & timeouts", "prefix": "Prompt cache evidence",
    "gpu": "GPU metrics",
}
for _old_check in ("concurrency", "capacity", "arrivals"):
    CHECK_NAMES.pop(_old_check, None)
    CATALOGUE_NAMES.pop(_old_check, None)
CHECK_NAMES["load"] = CATALOGUE_NAMES["load"] = INTERNAL_CHECK_NAMES["load"] = "Load test"
CORE_CHECKS = tuple(dict.fromkeys("load" if c in {"concurrency", "capacity"} else c for c in CORE_CHECKS))

SESSION_REPEAT_PROMPT = "Repeat exactly the above answer."


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class CleanupCriteria(Model):
    older_than_days: int | None = Field(default=None, ge=0, strict=True)
    keep_latest: int | None = Field(default=None, ge=0, strict=True)
    max_total_bytes: int | None = Field(default=None, strict=True, json_schema_extra={"minimum": 1})

    @field_validator("max_total_bytes")
    @classmethod
    def positive_total(cls, value):
        if value is not None and value <= 0:
            raise ValueError("Cleanup max total must be greater than 0; use --older-than 0 to remove all unprotected runs")
        return value


class Deployment(Model):
    # Explicit categories; arbitrary imports are filtered by deployment.py.
    model: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)
    serving: dict[str, Any] = Field(default_factory=dict)
    backend: dict[str, Any] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)
    software: dict[str, Any] = Field(default_factory=dict)
    extension: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="configured endpoint; other replicas unverified", max_length=MAX_DEPLOYMENT_SCOPE_CHARS)
    collected_at: str | None = None
    metadata_file: str | None = None
    launch_command: str | list[str] | None = None
    discovery: Literal["none", "ollama", "vllm", "sglang", "llamacpp", "tensorrt-llm", "lmdeploy", "tgi"] = "none"
    collector_url: str | None = None
    intended_change: str | Annotated[
        list[Annotated[str, Field(min_length=1, max_length=MAX_SANITIZE_STRING_CHARS)]],
        Field(min_length=1, max_length=MAX_SANITIZE_ITEMS),
    ] | None = None


class Arrival(Model):
    mode: Literal["steady", "burst"] = "steady"
    requests: int = Field(default=8, ge=2, le=MAX_ARRIVAL_REQUESTS)
    requests_per_second: float = Field(default=1, gt=0, le=MAX_ARRIVAL_RATE)
    burst_size: int = Field(default=2, ge=1, le=MAX_BURST_SIZE)
    max_in_flight: int | None = Field(default=None, ge=1, le=MAX_SIMULTANEOUS_REQUESTS)
    interval_seconds: float = Field(default=1, gt=0)
    max_pending: int = Field(default=4, ge=1, le=MAX_PENDING_ARRIVALS)
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


def load_validation_error(fields, message, values=None, actions=None):
    raise ValidationError.from_exception_data("Load test", [{
        "type": PydanticCustomError("load_test", message, {"fields": fields,
            **({"values": values} if values is not None else {}), **({"actions": actions} if actions is not None else {})}),
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
    levels: list[Annotated[StrictInt, Field(ge=1, le=MAX_SIMULTANEOUS_REQUESTS)]] = Field(default_factory=lambda: [1, 4], min_length=1, max_length=MAX_STAGES)
    duration_seconds: StrictInt = Field(default=15, ge=1, le=MAX_STAGE_SECONDS)

    @field_validator("levels")
    @classmethod
    def valid_levels(cls, values):
        if len(values) != len(set(values)) or any(not 1 <= v <= MAX_SIMULTANEOUS_REQUESTS for v in values):
            raise ValueError(f"Concurrency levels must be distinct whole numbers from 1 to {MAX_SIMULTANEOUS_REQUESTS}.")
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


# Issue #45: workload selection and traffic are independent. Legacy models above
# remain importable for decoding saved inputs; execution uses only LoadTest.
class LoadTestProfile(DataProfile):
    short: LoadSize = Field(default_factory=lambda: LoadSize(input_tokens=128, output_tokens=64))
    medium: LoadSize = Field(default_factory=lambda: LoadSize(input_tokens=512, output_tokens=256))
    long: LoadSize = Field(default_factory=lambda: LoadSize(input_tokens=2048, output_tokens=512))

    @model_validator(mode="after")
    def strictly_increasing(self, info: ValidationInfo):
        # Saved token ceilings could collapse two or three profile sizes. Keep
        # those imported targets readable without weakening the new editor.
        if info.context and info.context.get("legacy_load_source"):
            return self
        for field in ("input_tokens", "output_tokens"):
            for previous, size in zip(SIZES, SIZES[1:]):
                if getattr(getattr(self, size), field) <= getattr(getattr(self, previous), field):
                    load_validation_error([f"{size}.{field}"], "Use increasing Short, Medium and Long token sizes.")
        return self


class MixedTraffic(Model):
    enabled: bool = False
    selected: list[Literal[*SHAPES]] = Field(default_factory=lambda: list(SHAPES))
    share_mode: Literal["equal", "custom"] = "equal"
    shares: dict[Literal[*SHAPES], float] = Field(default_factory=lambda: {s: 12 if i == 8 else 11 for i,s in enumerate(SHAPES)})

    @model_validator(mode="after")
    def selections(self):
        if self.enabled:
            if len(set(self.selected)) < 2 or len(set(self.selected)) != len(self.selected):
                load_validation_error(["selected"], "Select at least two distinct request types for mixed traffic.")
            if self.share_mode == "custom" and (any(self.shares.get(s, 0) <= 0 or self.shares.get(s, 0) > 100 for s in self.selected)
                    or sum(Decimal(str(self.shares.get(s, 0))) for s in self.selected) != Decimal(100)):
                load_validation_error(["shares", "share_mode"], "Give each selected mix type a positive share, totaling exactly 100%.")
        return self

    def weights(self):
        return {s: 100 / len(self.selected) if self.share_mode == "equal" else self.shares[s] for s in self.selected}


class StreamInterference(Model):
    enabled: bool = False
    trigger: Literal["streaming_started", "after_five_chunks"] = "streaming_started"
    max_slowdown: float = Field(default=1.5, gt=1)


class TokenStress(Model):
    enabled: bool = False
    cases: list[Literal["long_short", "short_long", "long_long"]] = Field(default_factory=lambda: ["long_short", "short_long", "long_long"])
    min_output_percent: float = Field(default=80, ge=1, le=100)

    @model_validator(mode="after")
    def selected_cases(self):
        if self.enabled and (not self.cases or len(set(self.cases)) != len(self.cases)):
            load_validation_error(["cases"], "Select at least one distinct token-stress case.")
        return self


class LoadWorkloads(Model):
    combinations: LoadCombinations = Field(default_factory=LoadCombinations)
    mixed: MixedTraffic = Field(default_factory=MixedTraffic)
    interference: StreamInterference = Field(default_factory=StreamInterference)
    stress: TokenStress = Field(default_factory=TokenStress)


class PatternToggle(Model):
    enabled: bool = False


class ConcurrentPattern(Model):
    enabled: bool = True
    levels: list[Annotated[StrictInt, Field(ge=1, le=MAX_SIMULTANEOUS_REQUESTS)]] = Field(default_factory=lambda: [1, 4], min_length=1, max_length=MAX_STAGES)

    @field_validator("levels")
    @classmethod
    def increasing(cls, values):
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("Enter positive, increasing whole-number levels.")
        return values


class PoissonPattern(PatternToggle):
    seed: StrictInt = Field(default=42, ge=0, le=2**53 - 1)


class BurstPattern(PatternToggle):
    size: StrictInt = Field(default=32, ge=1, le=MAX_BURST_SIZE)
    first_seconds: float = Field(default=10, ge=0)
    gap_seconds: float = Field(default=10, gt=0)


class LoadPatterns(Model):
    concurrent: ConcurrentPattern = Field(default_factory=ConcurrentPattern)
    constant: PatternToggle = Field(default_factory=lambda: PatternToggle(enabled=True))
    poisson: PoissonPattern = Field(default_factory=PoissonPattern)
    rates: list[Annotated[float, Field(gt=0, le=MAX_TRAFFIC_RATE)]] = Field(default_factory=lambda: [1, 2], min_length=1, max_length=MAX_STAGES)
    bursts: BurstPattern = Field(default_factory=BurstPattern)

    @field_validator("rates")
    @classmethod
    def increasing(cls, values):
        if any(a >= b for a, b in zip(values, values[1:])):
            raise ValueError("Enter positive, increasing rates.")
        return values


class LoadWindow(Model):
    duration_seconds: StrictInt = Field(default=15, ge=1, le=MAX_STAGE_SECONDS)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_finish_limit(cls, value):
        # Older saved runs remain readable; admission windows no longer cancel
        # outstanding requests before their own timeout.
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "finish_seconds"}
        return value


class ResponseCriteria(Model):
    max_first_answer_ms: float | None = Field(default=2000, gt=0)
    max_completion_ms: float | None = Field(default=30000, gt=0)
    max_error_percent: float = Field(default=0, ge=0, le=100)
    min_correct_percent: float = Field(default=100, ge=0, le=100)
    max_stream_pause_ms: float | None = Field(default=None, gt=0)
    min_generation_tokens_per_second: float | None = Field(default=None, gt=0)
    min_valid_responses: StrictInt = Field(default=5, ge=1)

    def timing_limits(self):
        return Limits(first_output_ms=self.max_first_answer_ms, latency_ms=self.max_completion_ms,
                      stream_gap_ms=self.max_stream_pause_ms, min_output_tokens_per_second=self.min_generation_tokens_per_second)


class LoadTestSettings(Model):
    legacy_source: Literal["concurrency", "capacity", "arrivals"] | None = Field(
        default=None, description="Imported schedule provenance; preserves legacy truncation and clamped profile targets.")
    profile: LoadTestProfile = Field(default_factory=LoadTestProfile)
    workloads: LoadWorkloads = Field(default_factory=LoadWorkloads)
    patterns: LoadPatterns = Field(default_factory=LoadPatterns)
    schedule: LoadWindow = Field(default_factory=LoadWindow)
    request_timeout_seconds: float = Field(default=300, gt=0)
    pass_criteria: ResponseCriteria = Field(default_factory=ResponseCriteria)

    @field_validator("profile", mode="plain", json_schema_input_type=LoadTestProfile)
    @classmethod
    def imported_profile(cls, value, info: ValidationInfo):
        return LoadTestProfile.model_validate(value, context={"legacy_load_source": info.data.get("legacy_source")})

    @field_serializer("profile")
    def serialize_profile(self, value) -> dict[str, Any]:
        return value.model_dump()

    @model_validator(mode="before")
    @classmethod
    def sparse_profile(cls, value):
        if isinstance(value, dict) and isinstance(value.get("profile"), dict):
            defaults = LoadTestProfile().model_dump()
            value = {**value, "profile": {size: {**defaults.get(size, {}), **dimensions} if isinstance(dimensions, dict) else dimensions for size,dimensions in value["profile"].items()}}
        return value

    # Calibration remains an internal generator policy, not duplicate UI controls.
    @property
    def calibration_attempts(self):
        return 3

    @property
    def input_token_tolerance(self):
        return .1

    @model_validator(mode="after")
    def selected_work(self):
        w, p = self.workloads, self.patterns
        if not any(getattr(w, k).enabled for k in ("combinations", "mixed", "stress", "interference")):
            load_validation_error(["workloads.combinations.enabled"], "Select at least one workload.")
        standard = any(getattr(w, k).enabled for k in ("combinations", "mixed", "stress"))
        if standard and not any(getattr(p, k).enabled for k in ("concurrent", "constant", "poisson", "bursts")):
            load_validation_error(["patterns.constant.enabled"], "Select a traffic pattern for the selected workloads.")
        if w.interference.enabled and not any(n >= 2 for n in p.concurrent.levels):
            load_validation_error(["patterns.concurrent.levels"], "The long-input / short-answer test needs a concurrency level of at least 2.")
        if standard and p.bursts.enabled and p.bursts.first_seconds >= self.schedule.duration_seconds:
            load_validation_error(["patterns.bursts.first_seconds", "schedule.duration_seconds"], "The first burst must start before the sending window ends.")
        return self


class MigratedLoadTest(Model):
    source: Literal["concurrency", "capacity", "arrivals"]
    settings: LoadTestSettings


class LoadTest(LoadTestSettings):
    # Independent legacy schedules cannot be represented by one shared duration
    # or profile. Preserve additional tests explicitly rather than blending them.
    migrated_tests: list[MigratedLoadTest] = Field(default_factory=list, max_length=3)


def _validated_migration_input(value):
    """Validate fields migrations inspect using the canonical model contract.

    Compatibility migration runs before model validation at HTTP/CLI boundaries.
    Preserve field locations and Pydantic's accepted Python collection inputs,
    without treating arbitrary raw values as iterable or hashable check ids.
    """
    if not isinstance(value, dict):
        return value
    validated, errors = {}, []
    for name, adapter in _MIGRATION_INPUT_ADAPTERS.items():
        if name not in value:
            continue
        try:
            validated[name] = adapter.validate_python(value[name])
        except ValidationError as error:
            errors.extend({**item, "loc": (name, *item["loc"])}
                          for item in error.errors(include_url=False))
    if errors:
        raise ValidationError.from_exception_data("RunConfig", errors, hide_input=True)
    return {**value, **validated}


def migrate_load_test(value):
    """Preserve legacy traffic as separate tests and explain changed semantics."""
    value = _validated_migration_input(value)
    if not isinstance(value, dict):
        return value, []
    value = deepcopy(value)
    notes = []
    old_checks = list(value.get("checks", []))
    older = {"fairness", "buckets", "mixed", "token_stress"}.intersection(old_checks)
    if older or value.get("concurrent_workloads") is not None or value.get("buckets") is not None:
        value, earlier_notes = migrate_concurrent_load(value)
        notes.extend(earlier_notes)
        old_checks = list(value.get("checks", []))
    selected = [c for c in old_checks if c in {"concurrency", "capacity", "arrivals"}]
    if "checks" not in value and value.get("arrivals"):
        selected.append("arrivals")
    if not selected:
        return value, notes
    converted = []
    options = value.get("test_options") or {}
    def plain(v):
        return v.model_dump(exclude_unset=True) if isinstance(v, BaseModel) else v or {}
    def validate_legacy(model, raw, prefix):
        try:
            return model.model_validate(raw)
        except ValidationError as error:
            details = []
            for item in error.errors(include_url=False):
                context = dict(item.get("ctx") or {})
                if context.get("fields"):
                    context["fields"] = [field if field.startswith(prefix + ".") else prefix + "." + field
                                         for field in context["fields"]]
                details.append({"type": PydanticCustomError(item["type"], item["msg"], context),
                                "loc": (prefix, *item["loc"]), "input": item.get("input")})
            raise ValidationError.from_exception_data("Legacy load configuration", details) from None
    timeout = value.get("request_timeout_seconds", 300)
    for check in dict.fromkeys(selected):
        settings = LoadTestSettings().model_dump()
        settings["legacy_source"] = check
        w, patterns = settings["workloads"], settings["patterns"]
        for k in w:
            w[k]["enabled"] = False
        for k in ("concurrent", "constant", "poisson", "bursts"):
            patterns[k]["enabled"] = False
        own = plain(options.get(check))
        settings["request_timeout_seconds"] = own.get("request_timeout_seconds") or timeout
        if check == "concurrency":
            old_input = plain(value.get("concurrent_load"))
            explicit_levels = "levels" in plain(old_input.get("schedule"))
            if isinstance(old_input.get("profile"), dict):
                defaults = DataProfile().model_dump()
                old_input["profile"] = {size: {**defaults.get(size, {}), **dimensions}
                    if isinstance(dimensions, dict) else dimensions
                    for size, dimensions in old_input["profile"].items()}
            old = validate_legacy(ConcurrentLoad, old_input, "concurrent_load").model_dump()
            settings["profile"] = old["profile"]
            w["combinations"] = old["combinations"]
            shares = old["mixed"]["shares"]
            w["mixed"].update(enabled=old["mixed"]["enabled"], selected=[s for s,v in shares.items() if v > 0], share_mode="custom", shares=shares)
            if w["mixed"]["enabled"] and len(w["mixed"]["selected"]) < 2:
                w["mixed"]["enabled"] = False
                notes.append("Legacy one-shape mixed workload disabled; mixed traffic requires at least two request shapes.")
            w["interference"].update(enabled=old["interference"]["enabled"], trigger="after_five_chunks" if old["interference"]["trigger"] == "after_chunks" else "streaming_started", max_slowdown=old["interference"]["max_slowdown"] or 1.5)
            w["stress"].update(enabled=old["stress"]["enabled"], cases=(["long_short"] if old["stress"]["large_input"] else []) + (["short_long"] if old["stress"]["large_output"] else []), min_output_percent=old["stress"]["min_output_percent"])
            patterns["concurrent"].update(enabled=True, levels=old["schedule"]["levels"])
            if not explicit_levels:
                patterns["concurrent"].pop("levels", None)
            patterns["poisson"]["seed"] = old["mixed"]["seed"] if old["mixed"]["seed"] is not None else 42
            settings["schedule"]["duration_seconds"] = old["schedule"]["duration_seconds"]
            settings["pass_criteria"].update({k: v for k,v in old["pass_criteria"].items() if k in settings["pass_criteria"]})
            if old["input_token_tolerance"] != .1 or old["calibration_attempts"] != 3:
                notes.append(f"Legacy input_token_tolerance={old['input_token_tolerance']} and calibration_attempts={old['calibration_attempts']} are replaced by bounded local tokenization with 0.1 tolerance. No inference requests are used for prompt sizing.")
            notes.append("Concurrent load migrated to Load test. Existing per-test request timeout is preserved; outstanding requests finish under that timeout. Old p95 limits were not converted to per-response limits; new per-response defaults (2,000 ms first answer / 30,000 ms completion) apply. Review per-response criteria. Old mixed seed is now the shared schedule/mix seed; interference uses full baseline/probe windows and a five-chunk trigger.")
        elif check == "capacity":
            old = validate_legacy(TrafficConfig, plain(value.get("traffic")), "traffic")
            patterns["poisson" if old.arrival == "poisson" else "constant"]["enabled"] = True
            patterns["rates"], patterns["poisson"]["seed"] = old.rates, old.seed
            settings["schedule"].update(duration_seconds=math.ceil(old.duration_seconds))
            shares = {"short_short": old.mix.short*100, "long_short": old.mix.long_input*100, "short_long": old.mix.long_output*100}
            shapes = [s for s,v in shares.items() if v > 0]
            if len(shapes) == 1:
                w["combinations"].update(enabled=True, selected=shapes)
            else:
                # Decimal-safe normalization makes the persisted shares sum exactly 100.
                shares[shapes[-1]] = float(Decimal(100)-sum(Decimal(str(shares[s])) for s in shapes[:-1]))
                w["mixed"].update(enabled=True, selected=shapes, share_mode="custom", shares=shares)
            notes.append(f"Capacity traffic migrated as an independent {old.arrival} test with the equivalent short/long-input/long-output request shares. long_input_chars={old.long_input_chars} and long_output_words={old.long_output_words} were not converted into tokens; review the explicit data profile. max_in_flight dropped: Run limits concurrency ceiling now governs admission. Fractional durations round up to whole seconds.")
        else:
            old = validate_legacy(Arrival, plain(value.get("arrivals")), "arrivals")
            w["combinations"].update(enabled=True, selected=["short_short"])
            patterns["poisson"]["seed"] = max(0, old.seed)
            if old.mode == "steady":
                patterns["constant"]["enabled"] = True
                patterns["rates"] = [old.requests_per_second]
                duration = old.requests / old.requests_per_second
            else:
                patterns["bursts"].update(enabled=True, size=old.burst_size, first_seconds=0, gap_seconds=old.interval_seconds)
                duration = math.ceil(old.requests / old.burst_size)*old.interval_seconds
            settings["schedule"]["duration_seconds"] = max(1, math.ceil(duration))
            notes.append(f"Legacy arrivals migrated as a separate {old.mode} test; requests={old.requests} became duration={settings['schedule']['duration_seconds']} seconds (rounded up; final bursts may contain more requests). max_pending and max_in_flight dropped: there is no pending queue; full client slots are local drops under the run ceiling.")
        # Preserve old common per-response limits where they already had these semantics.
        if check != "concurrency":
            limits = {**Limits().model_dump(), **plain(value.get("limits")), **plain(own.get("limits"))}
            for old_key,new_key in {"first_output_ms":"max_first_answer_ms", "latency_ms":"max_completion_ms", "stream_gap_ms":"max_stream_pause_ms", "min_output_tokens_per_second":"min_generation_tokens_per_second", "min_samples":"min_valid_responses"}.items():
                if old_key in limits:
                    settings["pass_criteria"][new_key] = limits[old_key]
            for old_key,new_key in (("max_error_rate","max_error_percent"),("min_correctness","min_correct_percent")):
                if old_key in limits:
                    settings["pass_criteria"][new_key] = limits[old_key]*100
        converted.append({"source": check, "settings": settings})
    if "load_test" not in value or "load" not in old_checks:
        value["load_test"] = converted.pop(0)["settings"]
    if isinstance(value["load_test"], BaseModel):
        value["load_test"] = value["load_test"].model_dump()
    if converted:
        value["load_test"].setdefault("migrated_tests", []).extend(converted)
        notes.append("Multiple saved checks remain separate sequential tests in load_test.migrated_tests; their rates, durations and burst schedules are not blended.")
    value["checks"] = list(dict.fromkeys("load" if c in {"concurrency", "capacity", "arrivals"} else c for c in old_checks)) if old_checks else list(_LEGACY_DEFAULT_CHECKS)
    value.pop("concurrent_load", None)
    value.pop("traffic", None)
    value.pop("arrivals", None)
    if options:
        value["test_options"] = {k:v for k,v in options.items() if k not in {"concurrency", "capacity", "arrivals"}}
    return value, notes


class _LegacyBuckets(Model):
    input_chars: dict[str, int] = Field(default_factory=lambda: {"short": 64, "medium": 1024, "long": 12288})
    output_tokens: dict[str, int] = Field(default_factory=lambda: {"short": 16, "medium": 48, "long": 96})
    levels: list[Annotated[int, Field(ge=1, le=MAX_SIMULTANEOUS_REQUESTS)]] = Field(default_factory=lambda: [1, 2], min_length=1, max_length=MAX_STAGES)
    samples_per_bucket: int = Field(default=5, ge=2, le=100)
    window_seconds: float = Field(default=30, gt=0)
    mixed_pairs: int = Field(default=5, ge=1, le=100)
    heavy_weights: dict[str, int] = Field(default_factory=lambda: {"long_input": 1, "long_output": 1})

    @model_validator(mode="after")
    def valid_values(self):
        for values in (self.input_chars, self.output_tokens):
            if set(values) != set(SIZES) or any(not 1 <= v <= 65536 for v in values.values()):
                raise ValueError("Legacy buckets need short/medium/long integer sizes in 1..65536")
        if not self.levels or len(self.levels) > MAX_STAGES or len(set(self.levels)) != len(self.levels) or any(not 1 <= level <= MAX_SIMULTANEOUS_REQUESTS for level in self.levels):
            raise ValueError(f"Legacy bucket levels must be distinct concurrency values in 1..{MAX_SIMULTANEOUS_REQUESTS}")
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


def is_legacy_config(value) -> bool:
    """Only saved inputs without any v2-only setting need compatibility migration."""
    if not isinstance(value, dict) or {"config_version", "default_concurrency"}.intersection(value):
        return False
    arrivals = value.get("arrivals")
    if isinstance(arrivals, BaseModel):
        return "max_in_flight" not in arrivals.model_fields_set
    return not isinstance(arrivals, dict) or "max_in_flight" not in arrivals


def migrate_run_limits(value):
    """Upgrade saved input only; Python-constructed configs already use v2."""
    value = _validated_migration_input(value)
    if not is_legacy_config(value):
        return value, []
    value, notes = deepcopy(value), []
    if "checks" not in value:
        legacy_load = any(value.get(field) is not None for field in ("concurrent_workloads", "buckets"))
        checks = ["concurrency" if check == "load" and legacy_load else check
                  for check in _LEGACY_DEFAULT_CHECKS]
        checks.extend([*(["json"] if value.get("structured_json") else []),
                       *(["gpu"] if value.get("metrics") else [])])
        checks.extend(check for field, check in (("arrivals", "arrivals"), ("prefix", "prefix"),
                                                ("sessions", "sessions"), ("consistency", "consistency"),
                                                ("tool_calling", "tools"), ("tool_checks", "tools"))
                      if value.get(field) is not None and (field != "tool_calling" or value[field]))
        value["checks"] = list(dict.fromkeys(checks))
    descriptions = {
        "max_requests": ("300 requests", "a request limit", "Maximum requests"),
        "max_duration_seconds": ("300 seconds", "a traffic time limit", "Maximum duration"),
        "concurrency": ("4 requests at once", "a simultaneous-request limit", "Simultaneous limit"),
    }
    for field, (old, label, control) in descriptions.items():
        if field not in value:
            value[field] = None
            notes.append(f"Earlier Giraffe versions stopped after {old} by default; this configuration did not set {label}, so none applies now. Enable {control} to keep that bound.")
    if value.get("concurrency") is not None:
        value.setdefault("default_concurrency", value["concurrency"])
        checks = value.get("checks", _LEGACY_DEFAULT_CHECKS)
        users = []
        for check in ("correctness", "json", "recovery", "arrivals", "capacity"):
            if check not in checks:
                continue
            if check in {"correctness", "json", "recovery"}:
                own = (value.get("test_options") or {}).get(check) or {}
                own = own.model_dump(exclude_unset=True) if isinstance(own, BaseModel) else own
                same = own.get("concurrency") in (None, value["concurrency"])
                if check == "correctness" and value.get("request_source") == "request_pack" and value.get("overlap_models"):
                    same = len(value.get("targets") or []) <= 1
            else:
                own = value.get("traffic" if check == "capacity" else "arrivals") or {}
                own = own.model_dump(exclude_unset=True) if isinstance(own, BaseModel) else own
                same = own.get("max_in_flight") in (None, value["concurrency"])
            if same:
                users.append(CHECK_NAMES.get(check, {"capacity": "Capacity", "arrivals": "Arrival traffic"}.get(check, check)))
        kept = (f"{', '.join(users)} retain legacy {value['default_concurrency']}-request parallelism"
                if users else "no selected check inherits that parallelism")
        notes.append(f"concurrency: {value['concurrency']} now limits simultaneous requests; default_concurrency={value['default_concurrency']}; {kept}.")
    if value.get("concurrency") is not None and value.get("overlap_models"):
        parallelism = value["concurrency"]
        checks = value.get("checks", _LEGACY_DEFAULT_CHECKS)
        if "correctness" in checks and value.get("request_source") == "request_pack":
            targets = len(value.get("targets") or []) or 1
            effective = max(1, parallelism // targets)
            if effective != parallelism:
                notes.append(f"Request-pack correctness has no per-check concurrency override; the preserved {parallelism}-request shared limit resolves inherited parallelism to {effective} per endpoint across {targets} overlapping endpoints.")
        options = value.setdefault("test_options", {})
        for check in ("correctness", "json", "recovery"):
            if check in checks and not (check == "correctness" and value.get("request_source") == "request_pack"):
                own = options.get(check, {})
                own = own.model_dump(exclude_unset=True) if isinstance(own, BaseModel) else own
                if own.get("concurrency") is None:
                    options[check] = {**own, "concurrency": parallelism}
        for field, check in (("traffic", "capacity"), ("arrivals", "arrivals")):
            if check in checks or field == "arrivals" and "checks" not in value and value.get(field) is not None:
                own = value.get(field) or {}
                own = own.model_dump(exclude_unset=True) if isinstance(own, BaseModel) else own
                if own.get("max_in_flight") is None:
                    value[field] = {**own, "max_in_flight": parallelism}
    checks = value["checks"]
    parallelism = value.get("concurrency") or 4
    if value.get("custom_fixtures") and "correctness" in checks:
        value["legacy_custom_fixture_max_output_tokens"] = value.get("max_output_tokens", 128)
        notes.append(f"Custom-fixture output budgets retain legacy max_output_tokens={value['legacy_custom_fixture_max_output_tokens']}; the compatibility cap is preserved in the saved configuration.")
    if value.get("overlap_models"):
        for field, default in (("sessions", {"concurrency": min(2, parallelism)}),
                               ("consistency", {"levels": [n for n in (1, 2) if n <= parallelism]})):
            if field in checks:
                own = value.get(field) or {}
                own = own.model_dump(exclude_unset=True) if isinstance(own, BaseModel) else own
                key = next(iter(default))
                if key not in own:
                    value[field] = {**own, **default}
                    notes.append(f"{field} legacy defaults preserved explicitly: {default!r}; endpoints share the simultaneous-request limit.")
    # Preserve the requests formerly produced by the global runtime cap. New v2
    # configurations keep each profile/case's own budget instead.
    cap = value.get("max_output_tokens", 128)
    if type(cap) is int and cap > 0:
        pack = value.get("request_pack")
        if isinstance(pack, BaseModel):
            pack = pack.model_dump(exclude_unset=True)
        if isinstance(pack, dict):
            for case in pack.get("cases", []):
                if isinstance(case, dict) and type(case.get("max_tokens")) is int and case["max_tokens"] > cap:
                    old = case["max_tokens"]
                    case["max_tokens"] = cap
                    notes.append(f"request_pack case {case.get('fixture_id')!r} max_tokens={old} preserved as {cap}, clipped to legacy max_output_tokens={cap}.")
            value["request_pack"] = pack
        if "sessions" in checks:
            own = value.get("sessions") or {}
            own = own.model_dump(exclude_unset=True) if isinstance(own, BaseModel) else own
            old = own.get("max_tokens", 32)
            if type(old) is int and old > cap:
                own["max_tokens"] = cap
                notes.append(f"sessions.max_tokens={old} preserved as {cap}, clipped to legacy max_output_tokens={cap}.")
            for workload in own.get("workloads", []):
                if isinstance(workload, dict) and type(workload.get("max_tokens")) is int and workload["max_tokens"] > cap:
                    old = workload["max_tokens"]
                    workload["max_tokens"] = cap
                    notes.append(f"sessions workload {workload.get('name')!r} max_tokens={old} preserved as {cap}, clipped to legacy max_output_tokens={cap}.")
            value["sessions"] = own
    legacy_load = {"concurrent_workloads", "buckets"}.intersection(value) or set(checks).intersection({"fairness", "buckets", "mixed", "token_stress"})
    if "concurrency" in checks and not legacy_load:
        load = value.get("concurrent_load") or {}
        load = load.model_dump(exclude_unset=True) if isinstance(load, BaseModel) else load
        schedule = load.get("schedule") or {}
        schedule = schedule.model_dump(exclude_unset=True) if isinstance(schedule, BaseModel) else schedule
        if "levels" not in schedule:
            targets = len(value.get("targets") or []) or 1
            ceiling = max(1, parallelism // targets) if value.get("overlap_models") else parallelism
            levels = [n for n in (1, 4) if n <= ceiling] or [1]
            load["schedule"] = {**schedule, "levels": levels}
            notes.append(f"Concurrent load inherited legacy schedule.levels={levels!r}, derived from concurrency={parallelism} and the endpoint count.")
        if type(cap) is int and cap > 0:
            profile = load.get("profile") or {}
            profile = profile.model_dump(exclude_unset=True) if isinstance(profile, BaseModel) else profile
            defaults = DataProfile().model_dump()
            explicit = {}
            for size in SIZES:
                supplied = profile.get(size) or {}
                supplied = supplied.model_dump(exclude_unset=True) if isinstance(supplied, BaseModel) else supplied
                profile[size] = supplied
                if isinstance(supplied, dict) and "output_tokens" in supplied:
                    explicit[size] = supplied["output_tokens"]
            for index, size in enumerate(SIZES):
                supplied = profile[size]
                if not isinstance(supplied, dict):
                    continue
                old = supplied.get("output_tokens", defaults[size]["output_tokens"])
                if size not in explicit:
                    low = max([1, *(explicit[s] for s in SIZES[:index] if type(explicit.get(s)) is int)])
                    high = min([cap, *(explicit[s] for s in SIZES[index + 1:] if type(explicit.get(s)) is int)])
                    old = max(low, min(old, high))
                if type(old) is int:
                    profile[size] = {**supplied, "output_tokens": min(old, cap)}
                    if old > cap or size not in explicit and defaults[size]["output_tokens"] > cap:
                        notes.append(f"concurrent_load.profile.{size}.output_tokens={supplied.get('output_tokens', defaults[size]['output_tokens'])} preserved as {min(old, cap)}, clipped to legacy max_output_tokens={cap}.")
                    elif old != defaults[size]["output_tokens"] and size not in explicit:
                        notes.append(f"concurrent_load.profile.{size}.output_tokens preserved as {old}, using legacy neighboring profile values.")
            load["profile"] = profile
        value["concurrent_load"] = load
    value["config_version"] = 2
    return value, notes


def required_concurrency(config) -> dict[str, Any]:
    """Conservative simultaneous target-request peak, independent of suite limits."""
    by_check = {}
    for check in config.checks:
        peak = 1
        if check in {"correctness", "json", "recovery"}:
            peak = config.effective_check(check)["concurrency"]
        elif check == "capacity":
            peak = config.effective_traffic().max_in_flight
        elif check == "arrivals":
            arrival = config.effective_arrivals()
            peak = arrival.max_in_flight if arrival else config.inherited_concurrency(config.default_concurrency)
        elif check == "sessions":
            peak = min(config.sessions.concurrency, config.sessions.sessions)
        elif check == "consistency":
            peak = max([1, *config.consistency.levels])
        elif check == "load":
            from giraffe.load_test import required_slots
            peak = required_slots(config)
        elif check == "concurrency":
            peak = max(config.concurrent_load.schedule.levels)
        by_check[check] = peak
    per_target = max(by_check.values(), default=1)
    return {"per_target_peak": per_target,
            "peak": per_target * (len(config.targets) if config.overlap_models else 1),
            "by_check": by_check}


def migrate_concurrent_load(value):
    """Convert legacy input without persisting migration metadata in config."""
    value = _validated_migration_input(value)
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
    elif legacy:
        # The final catalogue's Load test defaults must not replace an old
        # implicit workload block before its actual scenarios are converted.
        value["checks"] = list(dict.fromkeys("concurrency" if c == "load" else c for c in _LEGACY_DEFAULT_CHECKS))
    for name in ("concurrency", *legacy_checks):
        removed = options.pop(name, None)
        if removed:
            removed = removed.model_dump(exclude_unset=True) if isinstance(removed, BaseModel) else removed
            if name == "concurrency" and "request_timeout_seconds" in removed:
                options[name] = {"request_timeout_seconds": removed.pop("request_timeout_seconds")}
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
    targets = len(value.get("targets") or []) or 1
    levels = old.get("levels", [1, 2])
    ceiling = max(1, int(number(value.get("concurrency"), 4)))
    ceiling = max(1, ceiling // targets) if value.get("overlap_models") else ceiling
    actual_levels = sorted(set(min(ceiling, max(1, int(n))) for n in levels))[:MAX_STAGES] or [1]
    settings["schedule"]["levels"] = actual_levels
    if "levels" in old:
        notes.append(f"levels={levels!r} moved to schedule.levels={actual_levels!r} (bounded by Simultaneous limit).")
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
        value["checks"] = list(dict.fromkeys(selected_checks))
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
    remaining = (math.inf if value.get("max_duration_seconds") is None else number(value["max_duration_seconds"], math.inf) / sequential - fixed)
    if configured > remaining and "concurrency" in selected_checks:
        duration = math.floor(10 * max(0, remaining) / configured)
        if duration >= 1:
            settings["schedule"]["duration_seconds"] = duration
            notes.append(f"Migrated duration reduced from 10 to {duration} seconds to fit max_duration_seconds={value.get('max_duration_seconds', 300)} with other fixed windows.")
        else:
            for kind in ("combinations", "mixed", "interference", "stress"):
                settings[kind]["enabled"] = False
            notes.append(f"Concurrent load turned off: max_duration_seconds={value.get('max_duration_seconds', 300)} cannot fit its minimum sending windows.")
    if value.get("overlap_models") and value.get("concurrency") is not None and targets > int(number(value["concurrency"], 4)):
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
    turns: list[Annotated[str, Field(min_length=1, max_length=MAX_SESSION_TURN_CHARS)]] = Field(min_length=2, max_length=MAX_SESSION_TURNS)
    max_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def bounded_turns(self):
        if any(not turn.strip() or len(turn) > MAX_SESSION_TURN_CHARS for turn in self.turns):
            raise ValueError(f"workloads need non-empty user turns of at most {MAX_SESSION_TURN_CHARS} characters")
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
    sessions: int = Field(default=5, ge=1, le=MAX_SESSIONS)
    concurrency: int = Field(default=2, ge=1, le=MAX_SIMULTANEOUS_REQUESTS)
    turns: list[Annotated[str, Field(min_length=1, max_length=MAX_SESSION_TURN_CHARS)]] = Field(min_length=2, max_length=MAX_SESSION_TURNS, default_factory=lambda: [
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
        if not 2 <= len(self.turns) <= MAX_SESSION_TURNS or any(not t.strip() or len(t) > MAX_SESSION_TURN_CHARS for t in self.turns):
            raise ValueError(f"sessions need 2..{MAX_SESSION_TURNS} non-empty bounded user turns")
        return self


class LogprobConsistency(Model):
    response_mode: Literal["stream", "complete"] = "stream"
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


DEFAULT_CONSISTENCY_OUTPUT_TOKENS = 192


class Consistency(Model):
    logprobs: LogprobConsistency | None = None
    repetitions: int = Field(default=5, ge=2, le=100,
        description="Text repetitions round up to full load groups; logprob mode uses exactly this many measured rounds.")
    levels: list[Annotated[int, Field(ge=1, le=MAX_SIMULTANEOUS_REQUESTS)]] = Field(default_factory=lambda: [1, 2], min_length=1, max_length=MAX_STAGES)
    shapes: list[Literal["short", "long"]] = Field(default_factory=lambda: ["short", "long"])
    prompts_per_shape: int = Field(default=3, ge=2, le=16)
    temperature: float = Field(default=0, ge=0, le=2)
    seed: int | None = None
    equality: Literal["bytes", "whitespace"] = "bytes"
    strict: bool = False

    @model_validator(mode="after")
    def valid_consistency(self):
        if not self.levels or len(self.levels) > MAX_STAGES or len(set(self.levels)) != len(self.levels) or any(
            level < 1 or level > MAX_SIMULTANEOUS_REQUESTS for level in self.levels
        ) or not self.shapes or len(set(self.shapes)) != len(self.shapes):
            raise ValueError("consistency needs distinct bounded levels and shapes")
        return self


class Target(Model):
    tokenizer: str | None = Field(default=None, min_length=1,
        description="Prompt sizing tokenizer: null or auto resolves the served model name and falls back to builtin; builtin uses tiktoken o200k_base. A model ID or local directory remains an optional CLI override. Tokenizer assets download and cache locally; no inference is used for sizing.")
    _tokenizer_setup: dict = PrivateAttr(default_factory=dict)
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
    host_metrics_url: str | None = Field(default=None, description="Optional node_exporter Prometheus-text URL for server host CPU/RAM; independent of runtime metrics credentials.")
    metrics_api_key_env: str | None = None
    metrics_headers_env: dict[str, str] = Field(default_factory=dict)
    metrics_profile: Literal["unknown", "vllm-v1", "vllm-legacy", "ollama", "sglang", "llamacpp", "tensorrt-llm", "lmdeploy", "tgi"] = "unknown"
    metrics_runtime_version: str | None = None
    instrumentation: Literal["unknown", "enabled", "disabled", "unsupported"] = "unknown"
    deployment: Deployment = Field(default_factory=Deployment)
    restart_command: list[str] | None = None

    @field_validator("tokenizer", mode="before")
    @classmethod
    def trim_tokenizer(cls, value):
        return value.strip() or None if isinstance(value, str) else value

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
        if self.host_metrics_url is not None:
            host_metrics = urlsplit(self.host_metrics_url)
            if host_metrics.scheme not in {"http", "https"} or not host_metrics.hostname:
                raise ValueError("host_metrics_url must be an http(s) endpoint")
            if any(char.isspace() for char in self.host_metrics_url) or host_metrics.port == 0:
                raise ValueError("host_metrics_url must have a valid host and port")
            if host_metrics.username or host_metrics.password or host_metrics.query or host_metrics.fragment:
                raise ValueError("host_metrics_url must not contain credentials, query or fragment")
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
    rates: list[float] = Field(default_factory=lambda: [1, 2, 4], min_length=1, max_length=MAX_STAGES,
        json_schema_extra={"items": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_TRAFFIC_RATE}})
    duration_seconds: float = Field(default=5, gt=0, le=MAX_STAGE_SECONDS)
    arrival: Literal["steady", "poisson"] = "steady"
    max_in_flight: int | None = Field(default=None, ge=1, le=MAX_SIMULTANEOUS_REQUESTS)
    expected_response_seconds: float | None = Field(default=None, gt=0,
        description="Optional response-time estimate for capacity risk; does not change traffic.")
    seed: int = Field(default=42, ge=0)
    mix: TrafficMix = Field(default_factory=TrafficMix)
    scheduler_lag_tolerance_ms: float = Field(default=100, gt=0)
    drain_timeout_seconds: float | None = Field(default=None, gt=0,
        description="Legacy traffic drain limit; null lets each request finish or reach its own timeout.")
    long_input_chars: int = Field(default=4096, ge=128, le=1_000_000)
    long_output_words: int = Field(default=32, ge=2, le=4096)

    @model_validator(mode="after")
    def validate_schedule(self) -> TrafficConfig:
        if any(not math.isfinite(rate) or rate <= 0 or rate > MAX_TRAFFIC_RATE for rate in self.rates):
            raise PydanticCustomError("traffic_rates", f"traffic rates must be positive and at most {MAX_TRAFFIC_RATE} requests/second",
                                      {"fields": ["traffic.rates"], "values": {"traffic.rates": self.rates}})
        if any(left >= right for left, right in zip(self.rates, self.rates[1:])):
            raise PydanticCustomError("traffic_rates", "traffic rates must be unique and strictly increasing",
                                      {"fields": ["traffic.rates"], "values": {"traffic.rates": self.rates}})
        rates = [Decimal(str(rate)) for rate in self.rates]
        rate_total = sum(rates)
        duration = Decimal(str(self.duration_seconds))
        planned = rate_total * duration
        if planned > MAX_PLANNED_ARRIVALS:
            # Whole seconds make the correction useful in the editor. Very
            # high aggregate rates can require a sub-second stage instead.
            maximum_duration = Decimal(MAX_PLANNED_ARRIVALS) / rate_total
            precision = Decimal(1) if maximum_duration >= 1 else Decimal("0.001")
            longest = maximum_duration.quantize(precision, rounding=ROUND_FLOOR)

            def number(value):
                rendered = format(value, ",f")
                return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered

            single = len(rates) == 1
            rate_text = number(rates[0]) if single else "(" + " + ".join(number(rate) for rate in rates) + ")"
            message = (f"{rate_text} requests/second × {number(duration)} seconds = {number(planned)} planned arrivals. "
                f"This traffic plan allows at most {MAX_PLANNED_ARRIVALS:,} per endpoint. ")
            if single:
                message += f"At this rate, the longest stage is {number(longest)} seconds"
                if longest >= 60:
                    minutes, seconds = divmod(int(longest), 60)
                    message += f" ({minutes} minutes {seconds} seconds)"
                message += "."
            else:
                message += f"At these rates, the longest stage that fits is {number(longest)} seconds."
            longest_value = int(longest) if longest == int(longest) else float(longest)
            raise PydanticCustomError("traffic_plan_cap", "{message}", {
                "message": message,
                "fields": ["traffic.rates", "traffic.duration_seconds"],
                "values": {"traffic.rates": self.rates, "traffic.duration_seconds": self.duration_seconds,
                    "planned_arrivals": int(planned) if planned == int(planned) else float(planned),
                    "maximum_planned_arrivals": MAX_PLANNED_ARRIVALS, "longest_stage_seconds": longest_value},
                "actions": [{"label": f"Use {number(longest)} seconds", "field": "traffic.duration_seconds", "value": longest_value},
                            {"label": "Edit sending rate", "field": "traffic.rates"}],
                "note": "This cap covers scheduled traffic only; suite time and request budgets are separate.",
            })
        return self


class CheckOptions(Model):
    samples: int | None = Field(default=None, ge=1)
    concurrency: int | None = Field(default=None, ge=1, le=MAX_SIMULTANEOUS_REQUESTS)
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
    "serving": {"samples", "max_output_tokens", "request_timeout_seconds"},
    "first_output": {"max_output_tokens", "request_timeout_seconds"},
    "generation": {"samples", "max_output_tokens", "request_timeout_seconds"},
    "capacity": {"max_output_tokens", "request_timeout_seconds"},
    "fairness": {"samples", "context_limit"},
    "context": {"samples", "context_limit", "max_output_tokens", "request_timeout_seconds"},
    "correctness": {"samples", "concurrency", "max_output_tokens", "request_timeout_seconds"},
    "json": {"samples", "concurrency", "max_output_tokens", "request_timeout_seconds"},
    "cancellation": {"samples", "max_output_tokens", "request_timeout_seconds", "cancel_after_ms", "deadline_probe_ms", "backend_verification",
                     "trace_file", "server_log", "cancel_after_chunks", "cleanup_seconds", "trace_wait_seconds"},
    "recovery": {"samples", "concurrency", "sustained_seconds", "max_output_tokens", "request_timeout_seconds"},
    "arrivals": {"max_output_tokens", "request_timeout_seconds"},
    "prefix": {"context_limit", "request_timeout_seconds"},
    "sessions": {"context_limit", "request_timeout_seconds"},
    "consistency": {"context_limit", "max_output_tokens", "request_timeout_seconds"},
    "tools": {"context_limit", "max_output_tokens", "request_timeout_seconds"},
    "concurrency": {"request_timeout_seconds"},
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
for _check in ("arrivals", "prefix", "sessions", "consistency", "tools"):
    CHECK_LIMIT_FIELDS[_check] = _TRAFFIC_LIMITS - {"min_correctness"}
CHECK_LIMIT_FIELDS["tools"] = _TRAFFIC_LIMITS - {"min_correctness", "min_samples"}
CHECK_LIMIT_FIELDS["concurrency"] = set()
CHECK_OPTION_FIELDS["load"] = set()
CHECK_LIMIT_FIELDS["load"] = set()
CHECK_OPTION_FIELDS.pop("fairness", None)
CHECK_LIMIT_FIELDS.pop("fairness", None)


JUDGE_PROMPT_VERSION = "reference-correctness-v1"
JUDGE_VOTING_RULE = "complete-panel-strict-majority-v1"

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
    name: str = Field(min_length=1, max_length=MAX_TOOL_NAME_CHARS, pattern=r"^[A-Za-z0-9_-]+$")
    arguments: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_arguments(self):
        finite_tool_json(self.arguments)
        return self


class ToolChoiceRule(ToolContractModel):
    mode: Literal["none", "auto", "required", "named"]
    name: str | None = Field(default=None, min_length=1, max_length=MAX_TOOL_NAME_CHARS, pattern=r"^[A-Za-z0-9_-]+$")
    support: Literal["required", "discover"] = "required"
    max_calls: int = Field(default=MAX_TOOL_CALLS, ge=1, le=MAX_TOOL_CALLS, strict=True)
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
    tools: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_TOOL_CALLS)
    expected_calls: list[ExpectedToolCall] | None = Field(default=None, max_length=MAX_TOOL_CALLS)
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
            if not isinstance(name, str) or (not 1 <= len(name) <= MAX_TOOL_NAME_CHARS or not re.fullmatch(r"[A-Za-z0-9_-]+", name)):
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
        if finite_tool_json(self.result) > MAX_TOOL_RESULT_BYTES:
            raise ValueError("tool result must not exceed 64 KiB")
        bounded_json_tree(self.result)
        return self


class ToolConversationStep(ToolContractModel):
    expected_calls: list[ToolResultCall] = Field(min_length=1, max_length=MAX_TOOL_CALLS)
    user_message: str | None = Field(default=None, min_length=1, max_length=16384)


class ToolConversationFixture(ToolContractModel):
    fixture_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    messages: list[RequestPackMessage] = Field(min_length=1, max_length=64)
    tools: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_TOOL_CALLS)
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
            if finite_tool_json(values) > MAX_TOOL_CONFIG_BYTES:
                raise ValueError("tool_checks must not exceed 64 KiB")
        return value

    @model_validator(mode="after")
    def validate_checks(self):
        if finite_tool_json(self.model_dump(mode="python")) > MAX_TOOL_CONFIG_BYTES:
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
    concurrency: int = Field(default=2, ge=1, le=MAX_SIMULTANEOUS_REQUESTS)
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
    config_version: Annotated[Literal[2], BeforeValidator(TypeAdapter(StrictInt).validate_python)] = Field(default=2,
        description="Supported configuration version. Legacy files omit this field; normalized exports use version 2.")
    default_concurrency: int = Field(default=4, ge=1, le=MAX_SIMULTANEOUS_REQUESTS)
    concurrency: int | None = Field(default=None, ge=1, le=MAX_SIMULTANEOUS_REQUESTS,
                             description="Maximum simultaneous target requests across the run, chosen by the user; not detected hardware capacity.")
    max_requests: int | None = Field(default=None, ge=1,
                              description="Total target request budget shared across all tests and targets; independent of concurrency and separate from AI judge calls.")
    max_duration_seconds: float | None = Field(default=None, gt=0)
    max_memory_mb: int = Field(default=4096, ge=0,
        description="Maximum memory (RAM growth during this run) in MiB, sampled about once per second; 0 disables. Exceeding it stops unfinished work with partial evidence.")
    request_timeout_seconds: float = Field(default=300, gt=0)
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
    load_test: LoadTest = Field(default_factory=LoadTest)
    _migration_notes: list[str] = PrivateAttr(default_factory=list)
    _concurrency_warnings: list[dict[str, Any]] = PrivateAttr(default_factory=list)
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
    legacy_custom_fixture_max_output_tokens: int | None = Field(default=None, ge=1,
        description="Saved legacy fixture compatibility cap; new v2 fixtures keep their own output budgets.")
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
    def migrate_load_input(cls, value, handler):
        if isinstance(value, dict):
            value, notes = migrate_load_test(value)
            result = handler(value)
            result._migration_notes = list(dict.fromkeys([*notes, *result._migration_notes]))
            return result
        return handler(value)

    @model_validator(mode="before")
    @classmethod
    def load_profile_defaults(cls, value):
        # Sparse profiles inherit missing dimensions, never a suite output ceiling.
        if not isinstance(value, dict):
            return value
        load = value.get("concurrent_load")
        if not isinstance(load, dict) or not isinstance(load.get("profile"), dict):
            return value
        defaults = DataProfile().model_dump()
        profile = {size: {**defaults[size], **supplied} if isinstance(supplied, dict) else supplied
                   for size, supplied in load["profile"].items() if size in defaults}
        profile.update({size: supplied for size, supplied in load["profile"].items()
                        if size not in defaults})
        return {**value, "concurrent_load": {**load, "profile": profile}}

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
            if "correctness" in self.checks and "correctness" in self.test_options:
                options = self.test_options["correctness"]
                fields = [f"test_options.correctness.{field}" for field in sorted(options.model_fields_set - {"limits"})]
                if fields:
                    raise PydanticCustomError("request_pack_override",
                        "Request-pack correctness does not support request-setting overrides: {settings}. Per-check pass criteria are supported.",
                        {"settings": ", ".join(fields), "fields": fields,
                         "values": {field: getattr(options, field.rsplit(".", 1)[-1]) for field in fields}})
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
            raise PydanticCustomError("tool_stream_conflict", "Enable streaming on Connect or choose complete tool requests.",
                                      {"fields": ["tool_checks.modes", "stream"], "values": {"stream": self.stream}})
        # Selecting a workload in the UI runs its defaults. Retain unselected
        # settings for later edits without silently selecting their checks.
        if "arrivals" in self.checks and self.arrivals is None:
            self.arrivals = Arrival()
        if "prefix" in self.checks and self.prefix is None:
            self.prefix = Prefix()
        if "sessions" in self.checks and self.sessions is None:
            self.sessions = Sessions(max_tokens=min(32, self.max_output_tokens))
        if "consistency" in self.checks and self.consistency is None:
            self.consistency = Consistency()
        names = [t.name for t in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("target names must be unique")
        if set(self.checks) - set(CHECK_NAMES):
            raise ValueError("Unknown check name(s): " + ", ".join(sorted(set(self.checks) - set(CHECK_NAMES)))
                             + ". Valid names: " + ", ".join(CHECK_NAMES))
        self.resolve_concurrency()
        for check_id, options in self.test_options.items():
            if check_id not in CHECK_OPTION_FIELDS:
                raise PydanticCustomError("check_options", f"unsupported test_options check: {check_id}",
                                          {"fields": [f"test_options.{check_id}"]})
            fields = options.model_fields_set - {"limits"}
            unsupported = fields - CHECK_OPTION_FIELDS[check_id]
            limit_fields = options.limits.model_fields_set if options.limits else set()
            unsupported |= {f"limits.{field}" for field in limit_fields - CHECK_LIMIT_FIELDS[check_id]}
            if unsupported:
                raise PydanticCustomError("check_options", f"unsupported options for {check_id}: {', '.join(sorted(unsupported))}",
                                          {"fields": [f"test_options.{check_id}.{field}" for field in sorted(unsupported)]})
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
        if "load" in self.checks:
            from giraffe.load_test import validate_config
            validate_config(self)
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
                and {"load", "capacity", "correctness"}.intersection(self.checks)
                and any(case.scorer == "judge" for case in self.request_pack.cases)):
            if not self.judging.judges or any(
                not all(value.strip() for value in (judge.name, judge.url, judge.model))
                for judge in self.judging.judges
            ):
                raise ValueError("judge scoring requires at least one judge with a name, URL and model")

    def parallelism_requested(self, check_id: str) -> bool:
        options = self.test_options.get(check_id)
        return ("default_concurrency" in self.model_fields_set
                or bool(options and options.concurrency is not None))

    def effective_check(self, check_id: str) -> dict[str, Any]:
        """Resolve sparse overrides once; suite request settings are defaults."""
        if check_id not in INTERNAL_CHECK_NAMES:
            raise ValueError(f"unsupported check: {check_id}")
        if check_id == "load":
            return self.load_test.model_dump(mode="json")
        options = self.test_options.get(check_id, CheckOptions())
        special_defaults = {"backend_verification": "off", "cancel_after_ms": 100, "deadline_probe_ms": 50,
                            "cancel_after_chunks": 5, "cleanup_seconds": 5,
                            "trace_wait_seconds": 10}
        resolved = {}
        for field in sorted(CHECK_OPTION_FIELDS[check_id]):
            default = (self.inherited_concurrency(self.default_concurrency) if field == "concurrency"
                       else special_defaults.get(field, getattr(self, field, None)))
            override = getattr(options, field)
            value = default if override is None else override
            if check_id == "consistency" and field == "max_output_tokens" and override is None:
                value = min(DEFAULT_CONSISTENCY_OUTPUT_TOKENS, self.max_output_tokens)
            resolved[field] = value
        limits = self.limits.model_dump()
        if options.limits:
            limits.update(options.limits.model_dump(exclude_unset=True))
        supplied = self.limits.model_fields_set | (options.limits.model_fields_set if options.limits else set())
        if check_id == "first_output" and "first_output_ms" not in supplied:
            limits["first_output_ms"] = 10000
        if check_id == "generation" and "stream_gap_ms" not in supplied and not any(
                limits[key] is not None for key in ("latency_ms", "stream_gap_ms", "min_output_tokens_per_second")):
            limits["stream_gap_ms"] = 5000
        resolved["limits"] = limits
        if check_id in {"serving", "first_output"}:
            response = self.effective_response()
            resolved.update({field: response[field] for field in CHECK_OPTION_FIELDS[check_id]})
        if check_id == "concurrency":
            return self.concurrent_load.model_dump(mode="json") | {
                "request_timeout_seconds": resolved["request_timeout_seconds"],
            }
        return resolved

    def default_limit_fields(self, check_id: str) -> list[str]:
        options = self.test_options.get(check_id)
        supplied = self.limits.model_fields_set | (options.limits.model_fields_set if options and options.limits else set())
        return [name for name, value in self.effective_check(check_id).get("limits", {}).items()
                if value is not None and name not in supplied]

    def effective_response(self) -> dict[str, Any]:
        """Endpoint response owns shared initial/warm requests.

        Selected first-answer overrides apply when selected Endpoint response has
        no explicit value for the same setting. Disabled drafts do not change work.
        """
        serving = self.test_options.get("serving", CheckOptions()) if "serving" in self.checks else CheckOptions()
        first = self.test_options.get("first_output", CheckOptions()) if "first_output" in self.checks else CheckOptions()
        resolved = {"samples": self.samples if serving.samples is None else serving.samples}
        for field in ("max_output_tokens", "request_timeout_seconds"):
            value = getattr(serving, field)
            if value is None:
                value = getattr(first, field)
            resolved[field] = getattr(self, field) if value is None else value
        return resolved

    def effective_initial(self) -> dict[str, Any]:
        """Access owns the connection request's timeout, independent of warm samples."""
        resolved = self.effective_response()
        if "access" in self.checks:
            resolved["request_timeout_seconds"] = self.effective_check("access")["request_timeout_seconds"]
        return resolved

    def effective_traffic(self) -> TrafficConfig:
        return self.traffic.model_copy(update={
            "max_in_flight": self.traffic.max_in_flight or self.inherited_concurrency(self.default_concurrency),
        })

    def effective_arrivals(self) -> Arrival | None:
        if self.arrivals is None:
            return None
        return self.arrivals.model_copy(update={
            "max_in_flight": self.arrivals.max_in_flight or self.inherited_concurrency(self.default_concurrency),
        })

    def inherited_concurrency(self, value: int) -> int:
        if self.concurrency is None:
            return value
        targets = len(self.targets) if self.overlap_models else 1
        return max(1, min(value, self.concurrency // targets))

    def concurrency_notices(self) -> list[dict[str, Any]]:
        return deepcopy(self._concurrency_warnings)

    def resolve_concurrency(self) -> None:
        # Only inherited settings can be reduced. Explicit work remains intact.
        self._concurrency_warnings = []
        targets = len(self.targets) if self.overlap_models else 1

        def resolve(check, field, value, explicit, field_value=None, effective_value=None):
            effective = value if explicit else self.inherited_concurrency(value)
            if effective_value is not None:
                effective = effective_value
            if self.concurrency is None:
                return effective
            fields = [field, "concurrency"]
            values = {field: value if field_value is None else field_value, "concurrency": self.concurrency}
            if (explicit and value * targets > self.concurrency or
                    check == "concurrency" and effective * targets > self.concurrency):
                if targets == 1 or check == "concurrency":
                    message = (f"{CATALOGUE_NAMES[check]} runs {effective:,} at once" +
                               (f" × {targets:,} endpoints" if targets > 1 else "") +
                               f", above Simultaneous limit ({self.concurrency:,}).")
                    raise ValidationError.from_exception_data("Run limits", [{
                        "type": PydanticCustomError("concurrency_conflict", message,
                                                    {"fields": fields, "values": values}),
                        "loc": tuple(field.split(".")), "input": None}])
                self._concurrency_warnings.append({"fields": fields, "values": values,
                    "check": check, "msg": f"{CATALOGUE_NAMES[check]}: Shared across {targets:,} endpoints: up to {value * targets:,} requests "
                    + ("may be dropped locally when no shared slot is available" if check in {"arrivals", "capacity"} else "may wait")
                    + f" for the {self.concurrency:,}-request limit."})
            elif not explicit and value * targets > self.concurrency:
                self._concurrency_warnings.append({"fields": fields, "values": values,
                    "check": check, "effective_concurrency": effective,
                    "msg": f"{CATALOGUE_NAMES[check]} inherits {value:,} at once; Simultaneous limit ({self.concurrency:,}) allows {effective:,} per endpoint. Raise limit or Use {effective:,} for {CATALOGUE_NAMES[check]}."})
            return effective

        for check in self.checks:
            if check in CHECK_OPTION_FIELDS and "concurrency" in CHECK_OPTION_FIELDS[check]:
                override = self.test_options.get(check, CheckOptions()).concurrency
                resolve(check, f"test_options.{check}.concurrency" if override is not None else "default_concurrency",
                        override if override is not None else self.default_concurrency, override is not None)
            elif check == "capacity":
                own = self.traffic.max_in_flight
                resolve(check, "traffic.max_in_flight" if own is not None else "default_concurrency",
                        own if own is not None else self.default_concurrency, own is not None)
            elif check == "arrivals":
                own = self.arrivals.max_in_flight
                resolve(check, "arrivals.max_in_flight" if own is not None else "default_concurrency",
                        own if own is not None else self.default_concurrency, own is not None)
            elif check == "sessions":
                own = self.sessions
                explicit = "concurrency" in own.model_fields_set
                actual = resolve(check, "sessions.concurrency", min(own.concurrency if explicit else 2, own.sessions), explicit, own.concurrency)
                self.sessions = own.model_copy(update={"concurrency": own.concurrency if explicit else actual})
                self.sessions.__pydantic_fields_set__ = own.model_fields_set.copy()
            elif check == "consistency":
                own = self.consistency
                explicit = "levels" in own.model_fields_set
                levels = own.levels if explicit else [1, 2]
                resolve(check, "consistency.levels", max([1, *levels]), explicit, levels)
                if not explicit:
                    self.consistency = own.model_copy(update={"levels": sorted(set(self.inherited_concurrency(n) for n in levels))})
                    self.consistency.__pydantic_fields_set__ = own.model_fields_set.copy()
            elif check == "load":
                settings = [("load_test.", self.load_test), *[(f"load_test.migrated_tests.{i}.settings.", v.settings) for i,v in enumerate(self.load_test.migrated_tests)]]
                for prefix, own in settings:
                    pattern = own.patterns.concurrent
                    if (pattern.enabled or own.workloads.interference.enabled) and "levels" not in pattern.model_fields_set:
                        levels = pattern.levels
                        effective_levels = [n for n in levels if n <= self.inherited_concurrency(max(levels))] or [1]
                        if levels != effective_levels:
                            self._concurrency_warnings.append({"fields":[prefix+"patterns.concurrent.levels","concurrency"],"check":"load",
                                "values": {prefix+"patterns.concurrent.levels": levels, "concurrency": self.concurrency},
                                "effective_concurrency": max(effective_levels),
                                "msg":f"Load test inherits concurrency levels {levels}; the run ceiling allows {effective_levels} per endpoint."})
                        pattern.levels = effective_levels
                        pattern.__pydantic_fields_set__.discard("levels")
            elif check == "concurrency":
                own = self.concurrent_load.schedule
                explicit = "levels" in own.model_fields_set
                levels = own.levels if explicit else [1, 4]
                effective_levels = levels if explicit else [n for n in levels if n <= self.inherited_concurrency(max(levels))] or [1]
                resolve(check, "concurrent_load.schedule.levels", max(levels), explicit, levels,
                        effective_value=max(effective_levels))
                if not explicit:
                    schedule = own.model_copy(update={"levels": effective_levels})
                    schedule.__pydantic_fields_set__ = own.model_fields_set.copy()
                    self.concurrent_load = self.concurrent_load.model_copy(update={"schedule": schedule})

    @model_serializer(mode="wrap")
    def serialize_parallelism_intent(self, handler):
        result = handler(self)
        if "default_concurrency" not in self.model_fields_set:
            result.pop("default_concurrency", None)
        return result

    @field_serializer("limits")
    def serialize_sanity_opt_outs(self, value, info):
        result = value.model_dump(mode=info.mode)
        for field in ("first_output_ms", "stream_gap_ms"):
            if field not in value.model_fields_set:
                result.pop(field, None)
        return result

    @field_serializer("load_test")
    def serialize_load_test(self, value, info):
        result = value.model_dump(mode=info.mode)
        for settings, raw in [(value,result), *[(v.settings,raw) for v,raw in zip(value.migrated_tests,[x["settings"] for x in result["migrated_tests"]])]]:
            if "levels" not in settings.patterns.concurrent.model_fields_set:
                raw["patterns"]["concurrent"].pop("levels",None)
        return result

    @field_serializer("sessions", "consistency", "concurrent_load")
    def serialize_parallel_defaults(self, value, info):
        if value is None:
            return None
        result = value.model_dump(mode=info.mode)
        # A resolved default is not an explicit setting on the next I/O round trip.
        if info.field_name == "concurrent_load":
            if "levels" not in value.schedule.model_fields_set:
                result["schedule"].pop("levels", None)
        else:
            field = "concurrency" if info.field_name == "sessions" else "levels"
            if field not in value.model_fields_set:
                result.pop(field, None)
        return result

    @field_serializer("test_options")
    def serialize_test_options(self, options: dict[str, CheckOptions]) -> dict[str, Any]:
        return {name: value.model_dump(exclude_unset=True) for name, value in options.items()}


# Compile the canonical fields once, after RunConfig exists, so parsing does not
# rebuild schemas or invoke Pydantic's environment-dependent plugin discovery.
_MIGRATION_INPUT_ADAPTERS = {
    name: TypeAdapter(RunConfig.model_fields[name].rebuild_annotation())
    for name in ("config_version", "checks", "structured_json", "metrics", "tool_calling")
}


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
    request_timeout_seconds: float | None = None
    termination_reason: str | None = None
    termination_limit_seconds: float | None = None
    server_request_id: str | None = None
    client_cancelled_at_ns: int | None = None
    client_cancelled_monotonic_ns: int | None = None
    client_cancelled_ms: float | None = None
    elapsed_ms: float = 0
    stream_end_ms: float | None = None
    # Version 0 denotes saved records without receipt-event evidence. All times
    # below are monotonic milliseconds relative to the same request timer.
    timing_schema: int = 0
    first_token_ms: float | None = None
    first_token_reason: str | None = "Timing schema predates client content-event collection."
    output_events: list[dict[str, Any]] = Field(default_factory=list)
    output_events_truncated: bool = False
    answer_read_timestamps_ms: list[float] = Field(default_factory=list)
    reasoning_read_timestamps_ms: list[float] = Field(default_factory=list)
    read_timestamps_truncated: bool = False
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
    def read_gaps_ms(self) -> list[float] | None:
        """Answer-bearing network read gaps; these are not token ITL."""
        if not self.timing_schema or not self.stream or self.read_timestamps_truncated:
            return None
        return [right - left for left, right in zip(
            self.answer_read_timestamps_ms, self.answer_read_timestamps_ms[1:])]

    @property
    def reasoning_read_gaps_ms(self) -> list[float] | None:
        if not self.timing_schema or not self.stream or self.read_timestamps_truncated:
            return None
        return [right - left for left, right in zip(
            self.reasoning_read_timestamps_ms, self.reasoning_read_timestamps_ms[1:])]

    @property
    def tpot_estimate_ms(self) -> float | None:
        """Reciprocal of the existing character-allocation generation estimate."""
        pace = self.generation_tokens_per_second if self.timing_schema else None
        return 1000 / pace if pace is not None and pace > 0 else None

    @property
    def tpot_direct_reason(self) -> str | None:
        if not self.timing_schema:
            return "Timing schema predates client content-event collection."
        if not self.stream:
            return "Non-streamed replies do not provide token arrival timing."
        if not self.valid or self.status != "completed":
            return "A valid completed answer and matching usage are required."
        if self.output_tokens is None:
            return "Answer-token usage is missing or includes unseparated reasoning."
        if self.output_tokens <= 1:
            return "Single-token answers have no subsequent-token interval."
        if self.output_events_truncated or self.read_timestamps_truncated:
            return "Receipt-event evidence was truncated; collect a shorter response."
        if self.reasoning_stripped:
            return "Inline reasoning lacks an independently aligned answer-token population."
        events = [event for event in self.output_events if event["kind"] == "answer"]
        if not events or events[0].get("token_count") != 1:
            return "The first answer event is not proven to contain exactly one token; collect aligned streamed logprobs."
        if (any(event.get("token_count") is None for event in events)
                or sum(event["token_count"] for event in events) != self.output_tokens):
            return "Streamed token-boundary evidence does not match answer usage."
        if (len(events) < 2 or len({event["read_id"] for event in events}) != len(events)
                or events[-1]["at_ms"] <= events[0]["at_ms"]):
            return "Buffered answer events do not supply independent token arrival times."
        return None

    @property
    def tpot_direct_ms(self) -> float | None:
        """Mean answer-token interval, excluding one proven first-event token.

        Later multi-token events retain their actual shared receipt timestamp;
        this mean is not an observed individual-token latency distribution.
        """
        if self.tpot_direct_reason is not None:
            return None
        events = [event for event in self.output_events if event["kind"] == "answer"]
        return (events[-1]["at_ms"] - events[0]["at_ms"]) / (self.output_tokens - 1)

    @property
    def itl_reason(self) -> str | None:
        reason = self.tpot_direct_reason
        if reason is not None:
            return reason
        if any(event.get("token_count") != 1 for event in self.output_events
               if event["kind"] == "answer"):
            return "Multi-token answer events lack individual token arrival timestamps."
        return None

    @property
    def itl_ms(self) -> list[float] | None:
        """Only proven one-token, independent-read events yield token ITL."""
        if self.itl_reason is not None:
            return None
        times = [event["at_ms"] for event in self.output_events if event["kind"] == "answer"]
        return [right - left for left, right in zip(times, times[1:])]

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
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    performance: dict[str, Any] = Field(default_factory=dict)
    requests: list[RequestRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    observations: dict[str, Any] = Field(default_factory=dict)
    baseline: dict[str, Any] | None = None
    abort_reason: str | None = None
    limits_reached: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def aggregate_performance(self, info: ValidationInfo):
        if not (info.context or {}).get("metadata_only"):
            from .performance import refresh_performance

            refresh_performance(self)
        return self

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
