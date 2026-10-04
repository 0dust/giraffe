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


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    checks: list[str] = Field(default_factory=lambda: list(CHECK_NAMES))
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
        return self


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
    first_reasoning_ms: float | None = None
    max_stream_gap_ms: float | None = None
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
    schema_version: str = "1"
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
