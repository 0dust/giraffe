from __future__ import annotations

import hashlib
import json
import re

from giraffe.client import REASONING_BUDGET_ERROR, _ProtocolError, _text, _token_count
from giraffe.identities import LITELLM_NATIVE_KEY_HEADERS, gateway_identities_only, identity_headers, litellm_key_fingerprint
from giraffe.models import CheckResult, RequestSpec


# Version 4 also requires the client-side bounds on gateway response usage.
EVIDENCE_VERSION = 4
IDENTITY_CHANGED = "Test identity credentials changed after access preflight."
_IDENTITY_FAILURE_REASONS = {
    IDENTITY_CHANGED: "identity_changed",
    "Test identity credentials are unavailable.": "identity_unavailable",
}
QUALIFICATION = ("Only the declared identities and endpoint/model were exercised. "
                 "These fixtures do not certify tenant isolation or comprehensive gateway security.")


def configured_probe_count(config, target):
    settings = config.gateway_access
    if "gateway_access" not in config.checks or settings is None or settings.profile != "litellm":
        return 0
    return sum(case.identity in target.test_identities for case in settings.cases)


def deferred_for_shared_targets(config):
    return (not gateway_identities_only(config.checks)
            and any(configured_probe_count(config, target) for target in config.targets))


def credential_digest(headers: dict[str, str]) -> bytes:
    """Bind a probe to its qualified header values without retaining them."""
    return hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).digest()


def assistant_reply(message, finish_reason, usage, record):
    """Validate the fields used to qualify a plain assistant completion."""
    if message.get("role") != "assistant" or finish_reason not in ("stop", "length"):
        raise _ProtocolError("gateway access probe requires a completed assistant answer")
    if message.get("tool_calls") not in (None, []) or message.get("function_call") is not None:
        raise _ProtocolError("gateway access probe received an unexpected tool call")
    answer = _text(message.get("content"))
    reasoning = _text(message.get("reasoning_content"))
    alternate_reasoning = _text(message.get("reasoning"))
    has_refusal = bool(_text(message.get("refusal")).strip())
    metered_reasoning = (finish_reason == "length" and not has_refusal and message.get("audio") is None
                         and message.get("images") in (None, [])
                         and _metered_reasoning_usage(usage, record))
    return answer, reasoning or alternate_reasoning, finish_reason == "stop" and has_refusal, metered_reasoning


def _metered_reasoning_usage(usage, record):
    """Require the output cap to be accounted for entirely by reasoning."""
    completion = record.completion_tokens
    if not isinstance(usage, dict) or not (record.reasoning_tokens == completion == record.requested_max_tokens > 0):
        return False
    prompt = record.input_tokens
    if "prompt_tokens" in usage and prompt is None:
        return False
    if "total_tokens" in usage:
        total = _token_count(usage["total_tokens"])
        if total is None or total < completion or (prompt is not None and total != prompt + completion):
            return False
        prompt = total - completion
    prompt_details = usage.get("prompt_tokens_details")
    if prompt_details is not None:
        if not isinstance(prompt_details, dict):
            return False
        modalities = ("audio_tokens", "image_tokens", "text_tokens", "video_tokens")
        modality_total = 0
        for field in (*modalities, "cache_write_tokens", "cached_tokens"):
            if (value := prompt_details.get(field)) is None:
                continue
            count = _token_count(value)
            if count is None or (prompt is not None and count > prompt):
                return False
            # Cache counts can overlap modality counts.
            if field in modalities:
                modality_total += count
        if prompt is not None and modality_total > prompt:
            return False
    details = usage["completion_tokens_details"]
    return all(details.get(field) is None or _token_count(details[field]) == 0 for field in (
        "accepted_prediction_tokens", "rejected_prediction_tokens", "audio_tokens", "image_tokens",
        "text_tokens", "video_tokens",
    ))


def routing_issue(headers, model):
    if headers.get("x-litellm-attempted-fallbacks") not in (None, "0"):
        return "fallback_reported"
    if headers.get("x-litellm-model-group") not in (None, model):
        return "model_group_mismatch"
    return None


def denial_kind(status, body, *, anonymous=False):
    """Qualify only explicit LiteLLM authentication or model-access denials."""
    from giraffe.gateway_json import parse_gateway_json

    data = parse_gateway_json(body)
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "unknown"
    error_type, message = error.get("type"), error.get("message", "")
    code = error.get("code", status)
    if (not isinstance(error_type, str) or not isinstance(message, str)
            or re.search(r"\b\w+Exception\b", message)
            or type(code) not in (int, str) or code not in (status, str(status))):
        return "unknown"
    if status == 401:
        if error_type in {"token_not_found_in_db", "expired_key"}:
            return "authentication"
        if (anonymous and error_type == "auth_error" and error.get("param") is None
                and message == "Authentication Error, No api key passed in."):
            return "authentication"
    if status == 403 and error.get("param") in (None, "model") and error_type in {
        "key_model_access_denied", "team_model_access_denied", "user_model_access_denied",
        "org_model_access_denied", "project_model_access_denied",
    }:
        return "model_access"
    return "unknown"


def observation(record):
    if record is None:
        return "unverified", "not_run"
    if record.status == "cancelled":
        return "unverified", "request_cancelled"
    if reason := _IDENTITY_FAILURE_REASONS.get(record.error):
        return "unverified", reason
    if record._gateway_access_routing_issue:
        return "unverified", record._gateway_access_routing_issue
    if record.status == "completed" and record.http_status == 200:
        if record.valid:
            return "allow", "valid_completion"
        if record._gateway_access_refusal:
            return "allow", "assistant_refusal"
        if record.error == REASONING_BUDGET_ERROR and record._gateway_access_metered_reasoning:
            return "allow", "metered_reasoning_completion"
    evidence = record.gateway_evidence
    if (record.status == "failed" and record.error == f"HTTP {record.http_status}" and evidence
            and ((record.http_status == 401 and evidence.error_kind == "authentication")
                 or (record.http_status == 403 and evidence.error_kind == "model_access"))):
        return "deny", "gateway_" + evidence.error_kind
    return "unverified", "incomplete_or_unsupported_response"


def _row(case):
    return {**case.model_dump(), "observed": "unverified", "status": "inconclusive",
            "reason": "not_run", "http_status": None, "request_id": None}


def _record_case(row, record):
    observed, reason = observation(record)
    row.update(observed=observed, reason=reason,
               status="inconclusive" if observed == "unverified" else
                      "pass" if observed == row["expected"] else "fail",
               http_status=record.http_status, request_id=record.id)


def _qualify_identities(target, rows, auth_header):
    by_credentials = {}
    digests = {}
    for row in rows:
        try:
            headers = identity_headers(target, row["identity"])
        except ValueError:
            row["reason"] = "identity_unavailable"
            continue
        names = {name.lower() for name in headers} - {"accept"}
        if auth_header is None and names.difference(LITELLM_NATIVE_KEY_HEADERS):
            row["reason"] = "auth_header_unverified"
            continue
        credential = litellm_key_fingerprint(headers, auth_header=auth_header)
        if credential is None and names:
            row["reason"] = "auth_header_unverified"
            continue
        digests[row["name"]] = credential_digest(headers)
        by_credentials.setdefault(credential, []).append(row)

    for cases in by_credentials.values():
        if {row["expected"] for row in cases} == {"allow", "deny"}:
            for row in cases:
                row["reason"] = "identity_conflict"
    return digests


def _prepare_cases(config, target):
    settings = config.gateway_access
    if settings is None:
        return [], {}
    rows = [_row(case) for case in settings.cases]
    if settings.profile != "litellm":
        for row in rows:
            row["reason"] = "unsupported_profile"
        return rows, {}
    return rows, _qualify_identities(target, rows, settings.auth_header)


def eligible_probe_count(config, target):
    if not configured_probe_count(config, target):
        return 0
    rows, _ = _prepare_cases(config, target)
    return sum(row["reason"] == "not_run" for row in rows)


def _record_cutoff(run, target, rows, *, inference_finished=False):
    pending = [row for row in rows if row["reason"] == "not_run"]
    if not pending:
        return
    # A deadline expiring after inference cannot explain a probe that did not run.
    stopped = ((run.endpoint_health[target]["abort_reason"] or run.reason)
               if inference_finished else run.stop_reason(target))
    if stopped == "maximum run duration reached":
        reason = "run_deadline"
    elif stopped and stopped != "maximum request budget reached":
        reason = "run_stopped"
    elif stopped or run.remaining(target) == 0:
        reason = "request_budget_exhausted"
    else:
        return
    for row in pending:
        row["reason"] = reason
    run.mark_unfinished(target, ["gateway_access"])


def record_unstarted(run, target):
    observation = run.observations[target.name]
    if "gateway_access" in observation:
        return
    rows, _ = _prepare_cases(run.config, target)
    observation["gateway_access"] = {"cases": rows}
    _record_cutoff(run, target.name, rows, inference_finished=True)


async def execute(run, target, client):
    settings = run.config.gateway_access
    rows, digests = _prepare_cases(run.config, target)
    run.observations[target.name]["gateway_access"] = {"cases": rows}
    if settings is None or settings.profile != "litellm":
        return
    try:
        for case, row in zip(settings.cases, rows):
            if row["reason"] != "not_run":
                continue
            if not run.allowed(target.name, gateway_probe=True):
                break
            spec = RequestSpec(fixture_id="gateway-access-" + case.name,
                scenario="gateway_access", check_ids=["gateway_access"], identity=case.identity,
                messages=[{"role": "user", "content": "Reply with OK."}], stream=False,
                options={"disable_fallbacks": True},
                max_tokens=settings.max_output_tokens,
                request_timeout_seconds=settings.request_timeout_seconds,
                workload={"case": case.name, "expected": case.expected})
            spec._gateway_access_credential_digest = digests.pop(case.name)
            record = await run.request(target, client, spec, admission_deadline=run.deadline,
                prepared_admission=True, finalize_record=lambda record: _record_case(row, record),
                on_preparation_failure=lambda record: row.update(reason=_IDENTITY_FAILURE_REASONS.get(
                    record.error, "request_not_dispatched")))
            if record is None and row["reason"] == "not_run":
                if not run.allowed(target.name, gateway_probe=True):
                    break
                row["reason"] = "request_not_dispatched"
    finally:
        _record_cutoff(run, target.name, rows)


def result(run, target):
    settings = run.config.gateway_access
    data = run.observations[target.name].get("gateway_access", {})
    rows = data.get("cases", [])
    matrix = {row["expected"] for row in rows} == {"allow", "deny"}
    complete = bool(rows) and matrix and all(row["observed"] != "unverified" for row in rows)
    status = ("fail" if any(row["status"] == "fail" for row in rows) else
              "pass" if complete else "inconclusive")
    summary = ("Observed access differs from the declared policy." if status == "fail" else
               "Declared allow and deny cases matched the observed responses." if status == "pass" else
               "Access policy unverified: configure a supported profile and complete allow/deny cases with available test identities.")
    metrics = {"evidence_version": EVIDENCE_VERSION,
               "profile": settings.profile if settings else "unknown", "cases": rows,
               "coverage_complete": complete, "has_allow_and_deny": matrix,
               "qualification": QUALIFICATION}
    return CheckResult(id="gateway_access", title="Gateway access policy", target=target.name,
                       status=status, summary=summary, metrics={"gateway_access": metrics},
                       required=bool(settings and settings.profile == "litellm" and settings.cases)
                                or any(row["request_id"] for row in rows) or run.config.checks == ["gateway_access"],
                       evidence_ids=[row["request_id"] for row in rows if row["request_id"]])
