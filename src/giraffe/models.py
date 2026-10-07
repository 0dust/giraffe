"""Shared on-disk and in-process contracts. No server or database is required."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Status = Literal["pass", "fail", "inconclusive", "skipped", "blocked"]
CHECK_NAMES = {
    "access": "Network and access",
    "serving": "Serving success",
    "first_output": "First useful output",
    "generation": "Generation pace",
    "capacity": "Concurrency and capacity",
    "fairness": "Mixed long/short fairness",
    "context": "Long-context behavior",
    "correctness": "Known-answer correctness",
    "json": "Structured JSON",
    "cancellation": "Limits and cancellation",
    "recovery": "Sustained load and recovery",
    "gpu": "GPU observations",
}
CORE_CHECKS = list(CHECK_NAMES)
CHECK_NAMES.update({
    "arrivals": "Scheduled arrivals", "prefix": "Shared prefixes and cache evidence",
    "buckets": "Input/output and concurrency coverage", "mixed": "Mixed workload interference",
    "sessions": "Multi-turn sessions", "consistency": "Repeated-request consistency",
    "tools": "Tool-calling capability",
})


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
    discovery: Literal["none", "ollama", "vllm"] = "none"
    collector_url: str | None = None
    intended_change: str | None = None


class Arrival(Model):
    mode: Literal["steady", "burst"] = "steady"
    requests: int = Field(default=8, ge=2, le=10000)
    requests_per_second: float = Field(default=1, gt=0, le=1000)
    burst_size: int = Field(default=2, ge=1, le=256)
    interval_seconds: float = Field(default=1, gt=0)
    max_pending: int = Field(default=4, ge=1, le=256)
    seed: int = 0


class Prefix(Model):
    prefix_chars: int = Field(default=256, ge=32, le=65536)
    repeats: int = Field(default=4, ge=2, le=100)
    history_turns: int = Field(default=3, ge=2, le=32)


class Buckets(Model):
    input_chars: dict[str, int] = Field(default_factory=lambda: {
        "short": 64, "medium": 256, "long": 512})
    output_tokens: dict[str, int] = Field(default_factory=lambda: {
        "short": 16, "medium": 48, "long": 96})
    levels: list[int] = Field(default_factory=lambda: [1, 2])
    samples_per_bucket: int = Field(default=4, ge=2, le=100)
    window_seconds: float = Field(default=30, gt=0)
    mixed_pairs: int = Field(default=2, ge=1, le=100)
    heavy_weights: dict[str, int] = Field(default_factory=lambda: {
        "long_input": 1, "long_output": 1})

    @model_validator(mode="after")
    def valid_buckets(self):
        for values in (self.input_chars, self.output_tokens):
            if set(values) != {"short", "medium", "long"} or any(
                not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 65536
                for v in values.values()
            ):
                raise ValueError("buckets need short/medium/long integer sizes in 1..65536")
        if not self.levels or len(self.levels) > 16 or len(set(self.levels)) != len(self.levels) or any(
            level < 1 or level > 256 for level in self.levels
        ):
            raise ValueError("bucket levels must be distinct concurrency values in 1..256")
        if set(self.heavy_weights) != {"long_input", "long_output"} or any(
            not 1 <= weight <= 16 for weight in self.heavy_weights.values()
        ):
            raise ValueError("mixed heavy_weights need long_input/long_output weights in 1..16")
        return self


class Sessions(Model):
    modes: list[Literal["fixed", "live"]] = Field(default_factory=lambda: ["fixed", "live"])
    sessions: int = Field(default=2, ge=1, le=32)
    concurrency: int = Field(default=2, ge=1, le=32)
    turns: list[str] = Field(default_factory=lambda: [
        "Suggest a name for a bakery. Reply with just the name.",
        "Repeat exactly the bakery name you just suggested, with no extra text."])
    saved_answers: list[str] = Field(default_factory=lambda: ["Golden Crust", "Golden Crust"])
    delay_seconds: float = Field(default=0, ge=0, le=60)
    max_tokens: int = Field(default=32, ge=1)
    # Stop rather than inventing an answer or silently cropping required history.
    failure_policy: Literal["stop"] = "stop"

    @model_validator(mode="after")
    def valid_session(self):
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("select distinct fixed/live session modes")
        if not 2 <= len(self.turns) <= 32 or any(not t.strip() or len(t) > 8192 for t in self.turns):
            raise ValueError("sessions need 2..32 non-empty bounded user turns")
        if "fixed" in self.modes and len(self.saved_answers) < len(self.turns) - 1:
            raise ValueError("fixed history needs a saved assistant answer for each prior turn")
        return self


class Consistency(Model):
    repetitions: int = Field(default=4, ge=2, le=100)
    levels: list[int] = Field(default_factory=lambda: [1, 2])
    shapes: list[Literal["short", "long"]] = Field(default_factory=lambda: ["short", "long"])
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
    metrics_profile: Literal["unknown", "vllm-v1", "vllm-legacy", "ollama"] = "unknown"
    metrics_runtime_version: str | None = None
    instrumentation: Literal["unknown", "enabled", "disabled", "unsupported"] = "unknown"
    deployment: Deployment = Field(default_factory=Deployment)
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
    regression_percent: float = Field(default=20, ge=0)
    min_samples: int = Field(default=5, ge=2)


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
    checks: list[str] = Field(default_factory=lambda: list(CORE_CHECKS))
    structured_json: bool = False
    metrics: bool = False
    metrics_interval_seconds: float = Field(default=1, ge=.1, le=60)
    metrics_timeout_seconds: float = Field(default=2, gt=0, le=10)
    metrics_max_samples: int = Field(default=256, ge=2, le=4096)
    metrics_max_series: int = Field(default=128, ge=1, le=1024)
    cache_pressure_threshold: float = Field(default=.9, gt=0, le=1)
    arrivals: Arrival | None = None
    prefix: Prefix | None = None
    buckets: Buckets | None = None
    sessions: Sessions | None = None
    consistency: Consistency | None = None
    tool_calling: bool = False
    forced_tool_diagnostic: bool = False
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

    @model_validator(mode="after")
    def validate_run(self) -> RunConfig:
        names = [t.name for t in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("target names must be unique")
        if not self.checks or set(self.checks) - set(CHECK_NAMES):
            raise ValueError("checks must contain supported check names")
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
        for settings in (self.buckets, self.consistency):
            if settings and max(settings.levels) > self.concurrency:
                raise ValueError("workload levels must not exceed the global concurrency ceiling")
        if self.sessions and self.sessions.concurrency > self.concurrency:
            raise ValueError("session concurrency must not exceed the global ceiling")
        if self.buckets and max(self.buckets.output_tokens.values()) > self.max_output_tokens:
            raise ValueError("bucket output budgets must stay within max_output_tokens")
        if self.sessions and self.sessions.max_tokens > self.max_output_tokens:
            raise ValueError("session output budget must stay within max_output_tokens")
        for field, check in (("arrivals", "arrivals"), ("prefix", "prefix"),
                             ("buckets", "buckets"), ("sessions", "sessions"),
                             ("consistency", "consistency"), ("tool_calling", "tools")):
            if getattr(self, field) and check not in self.checks:
                self.checks.append(check)
        if self.buckets and "mixed" not in self.checks:
            self.checks.append("mixed")
        from giraffe.deployment import sanitize
        self.request_options = sanitize(self.request_options)
        return self


class RequestSpec(Model):
    fixture_id: str
    scenario: str
    check_ids: list[str]
    messages: list[dict[str, Any]]
    scorer: Literal["none", "exact", "contains", "json", "tool"] = "none"
    expected: Any = None
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = True
    options: dict[str, Any] = Field(default_factory=dict)
    cancel_after_ms: float | None = Field(default=None, gt=0)
    context_position: str | None = None
    input_chars: int = 0
    workload: dict[str, Any] = Field(default_factory=dict)


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

    completed_at: str | None = None
    dispatch_ms: float | None = None
    completed_ms: float | None = None
    scheduled_ms: float | None = None
    client_wait_ms: float | None = None
    overlap: int = 1
    workload: dict[str, Any] = Field(default_factory=dict)
    answer_hash: str | None = None
    request_hash: str | None = None
    history_hash: str | None = None
    cached_prompt_tokens: int | None = None
    cache_source: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

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
