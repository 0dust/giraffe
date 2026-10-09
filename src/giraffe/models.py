"""Shared on-disk and in-process contracts. No server or database is required."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

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
DEFAULT_CHECKS = tuple(name for name in CHECK_NAMES if name not in {"json", "gpu"})


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Target(Model):
    name: str = Field(min_length=1)
    url: str
    model: str = Field(min_length=1)
    api_key_env: str | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)
    route: Literal["direct", "gateway", "replica"] = "direct"
    parent: str | None = None
    identity: dict[str, str] = Field(default_factory=dict)
    metrics_url: str | None = None
    restart_command: list[str] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Target:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("target url must be an http(s) endpoint")
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
        return self


class Limits(Model):
    first_output_ms: float | None = Field(default=None, gt=0)
    latency_ms: float | None = Field(default=None, gt=0)
    stream_gap_ms: float | None = Field(default=None, gt=0)
    min_output_tokens_per_second: float | None = Field(default=None, gt=0)
    max_error_rate: float = Field(default=0, ge=0, le=1)
    min_correctness: float = Field(default=1, ge=0, le=1)
    fairness_max_ratio: float | None = Field(default=None, gt=1)
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
    limits: Limits | None = None


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
    "cancellation": {"samples", "cancel_after_ms", "deadline_probe_ms"},
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
    "recovery": _TRAFFIC_LIMITS - {"min_samples"}, "gpu": set(),
}


class RunConfig(Model):
    targets: list[Target] = Field(min_length=1)
    concurrency: int = Field(default=4, ge=1, le=256)
    max_requests: int = Field(default=160, ge=1)
    max_duration_seconds: float = Field(default=300, gt=0)
    request_timeout_seconds: float = Field(default=30, gt=0)
    max_output_tokens: int = Field(default=128, ge=1)
    context_limit: int = Field(default=4096, ge=128)
    samples: int = Field(default=6, ge=1)
    sustained_seconds: float = Field(default=5, ge=0)
    stop_after_errors: int = Field(default=20, ge=1)
    checks: list[str] = Field(default_factory=lambda: list(DEFAULT_CHECKS), min_length=1)
    structured_json: bool = False
    metrics: bool = False
    metrics_max_age_seconds: float = Field(default=60, gt=0)
    overlap_models: bool = False
    stream: bool = True
    request_options: dict[str, Any] = Field(default_factory=dict)
    custom_fixtures: str | None = None
    retention: Literal["all", "failures", "none"] = "failures"
    ca_bundle: str | None = None
    proxy: str | None = None
    restart_target: str | None = None
    limits: Limits = Field(default_factory=Limits)
    traffic: TrafficConfig = Field(default_factory=TrafficConfig)
    test_options: dict[str, CheckOptions] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_run(self) -> RunConfig:
        if "checks" not in self.model_fields_set:
            self.checks = [*self.checks, *(["json"] if self.structured_json else []),
                           *(["gpu"] if self.metrics else [])]
        self.checks = list(dict.fromkeys(self.checks))
        self.structured_json, self.metrics = "json" in self.checks, "gpu" in self.checks
        names = [t.name for t in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("target names must be unique")
        if set(self.checks) - set(CHECK_NAMES):
            raise ValueError("checks must contain supported check names")
        if ("capacity" in self.checks and self.traffic.max_in_flight is not None
                and self.traffic.max_in_flight > self.concurrency):
            raise ValueError(f"traffic.max_in_flight must not exceed global concurrency ({self.concurrency})")
        for check_id, options in self.test_options.items():
            if check_id not in CHECK_NAMES:
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
        return self

    def effective_check(self, check_id: str) -> dict[str, Any]:
        """Resolve sparse overrides once, retaining global safety ceilings."""
        if check_id not in CHECK_NAMES:
            raise ValueError(f"unsupported check: {check_id}")
        options = self.test_options.get(check_id, CheckOptions())
        special_defaults = {"cancel_after_ms": 100, "deadline_probe_ms": 50}
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


class RequestSpec(Model):
    fixture_id: str
    scenario: str
    check_ids: list[str]
    messages: list[dict[str, Any]]
    scorer: Literal["none", "exact", "contains", "json"] = "none"
    expected: Any = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    cancel_after_ms: float | None = Field(default=None, gt=0)
    context_position: str | None = None
    input_chars: int = 0
    request_timeout_seconds: float | None = Field(default=None, gt=0)


class RequestRecord(Model):
    id: str
    target: str
    fixture_id: str
    scenario: str
    check_ids: list[str]
    started_at: str
    status: Literal["completed", "failed", "timeout", "cancelled"]
    http_status: int | None = None
    error: str | None = None
    elapsed_ms: float = 0
    first_output_ms: float | None = None
    last_output_ms: float | None = None
    answer_chunks: int = 0
    first_reasoning_ms: float | None = None
    max_stream_gap_ms: float | None = None
    completion_tokens: int | None = None
    output_tokens: int | None = None
    input_tokens: int | None = None
    output_chars: int = 0
    output: str = ""
    reasoning: str = ""
    chunks: int = 0
    stream_terminated: bool = False
    finish_reason: str | None = None
    valid: bool = False
    score: bool | None = None
    score_message: str | None = None
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

    @property
    def generation_tokens_per_second(self) -> float | None:
        """Client-observed estimate; streamed chunks are not individual tokens.

        Exclude the first token and time before the first useful answer. Waiting
        for usage, finish markers or stream cleanup must not slow generation.
        Legacy records and single-chunk/non-streamed answers lack this evidence.
        """
        if (not self.valid or self.status != "completed" or not self.stream
                or self.output_tokens is None or self.output_tokens <= 1
                or self.answer_chunks < 2 or self.first_output_ms is None
                or self.last_output_ms is None or self.last_output_ms <= self.first_output_ms):
            return None
        return (self.output_tokens - 1) * 1000 / (self.last_output_ms - self.first_output_ms)


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
    schema_version: str = "2"
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


def overall_status(checks: list[CheckResult], abort_reason: str | None = None) -> Status:
    required = [check for check in checks if check.required]
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "blocked" for check in required):
        return "blocked"
    if abort_reason or not required or any(check.status == "inconclusive" for check in required):
        return "inconclusive"
    if all(check.status == "skipped" for check in required):
        return "skipped"
    return "pass"
