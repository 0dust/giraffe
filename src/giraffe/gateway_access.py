from __future__ import annotations

import hashlib
import json
import re

from giraffe import states
from giraffe.client import REASONING_BUDGET_ERROR, _ProtocolError, _text, _token_count
from giraffe.identities import LITELLM_NATIVE_KEY_HEADERS, gateway_identities_only, identity_headers, litellm_key_fingerprint
from giraffe.models import CheckResult, RequestSpec
from giraffe.request_outcome import preparation_failure_reason, request_outcome


# Version 4 also requires the client-side bounds on gateway response usage.
EVIDENCE_VERSION = 4
IDENTITY_CHANGED = "Test identity credentials changed after access preflight."
_IDENTITY_FAILURE_REASONS = {
    IDENTITY_CHANGED: states.GATEWAY_ACCESS_REASON_IDENTITY_CHANGED,
    "Test identity credentials are unavailable.": states.GATEWAY_ACCESS_REASON_IDENTITY_UNAVAILABLE,
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
        return states.GATEWAY_ACCESS_REASON_FALLBACK_REPORTED
    if headers.get("x-litellm-model-group") not in (None, model):
        return states.GATEWAY_ACCESS_REASON_MODEL_GROUP_MISMATCH
    return None


def denial_kind(status, body, *, anonymous=False):
    """Qualify only explicit LiteLLM authentication or model-access denials."""
    from giraffe.gateway_json import parse_gateway_json

    data = parse_gateway_json(body)
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return states.GATEWAY_ACCESS_DENIAL_UNKNOWN
    error_type, message = error.get("type"), error.get("message", "")
    code = error.get("code", status)
    if (not isinstance(error_type, str) or not isinstance(message, str)
            or re.search(r"\b\w+Exception\b", message)
            or type(code) not in (int, str) or code not in (status, str(status))):
        return states.GATEWAY_ACCESS_DENIAL_UNKNOWN
    if status == 401:
        if error_type in {"token_not_found_in_db", "expired_key"}:
            return states.GATEWAY_ACCESS_DENIAL_AUTHENTICATION
        if (anonymous and error_type == "auth_error" and error.get("param") is None
                and message == "Authentication Error, No api key passed in."):
            return states.GATEWAY_ACCESS_DENIAL_AUTHENTICATION
    if status == 403 and error.get("param") in (None, "model") and error_type in {
        "key_model_access_denied", "team_model_access_denied", "user_model_access_denied",
        "org_model_access_denied", "project_model_access_denied",
    }:
        return states.GATEWAY_ACCESS_DENIAL_MODEL_ACCESS
    return states.GATEWAY_ACCESS_DENIAL_UNKNOWN


def observation(record):
    # Access proof uses HTTP and protocol evidence, including metered budget-only replies.
    if record is None:
        return states.GATEWAY_ACCESS_UNVERIFIED, states.GATEWAY_ACCESS_REASON_NOT_RUN
    if record.status == "cancelled":
        return states.GATEWAY_ACCESS_UNVERIFIED, states.GATEWAY_ACCESS_REASON_REQUEST_CANCELLED
    if reason := _IDENTITY_FAILURE_REASONS.get(record.error):
        return states.GATEWAY_ACCESS_UNVERIFIED, reason
    if record._gateway_access_routing_issue:
        return states.GATEWAY_ACCESS_UNVERIFIED, record._gateway_access_routing_issue
    if record.status == "completed" and record.http_status == 200:
        if record.valid:
            return states.GATEWAY_ACCESS_ALLOW, states.GATEWAY_ACCESS_REASON_VALID_COMPLETION
        if record._gateway_access_refusal:
            return states.GATEWAY_ACCESS_ALLOW, states.GATEWAY_ACCESS_REASON_ASSISTANT_REFUSAL
        if record.error == REASONING_BUDGET_ERROR and record._gateway_access_metered_reasoning:
            return states.GATEWAY_ACCESS_ALLOW, states.GATEWAY_ACCESS_REASON_METERED_REASONING_COMPLETION
    evidence = record.gateway_evidence
    if (record.status == "failed" and record.error == f"HTTP {record.http_status}" and evidence
            and ((record.http_status == 401 and evidence.error_kind == states.GATEWAY_ACCESS_DENIAL_AUTHENTICATION)
                 or (record.http_status == 403 and evidence.error_kind == states.GATEWAY_ACCESS_DENIAL_MODEL_ACCESS))):
        return states.GATEWAY_ACCESS_DENY, (states.GATEWAY_ACCESS_REASON_GATEWAY_AUTHENTICATION
            if evidence.error_kind == states.GATEWAY_ACCESS_DENIAL_AUTHENTICATION
            else states.GATEWAY_ACCESS_REASON_GATEWAY_MODEL_ACCESS)
    return states.GATEWAY_ACCESS_UNVERIFIED, states.GATEWAY_ACCESS_REASON_INCOMPLETE_OR_UNSUPPORTED_RESPONSE


def _row(case):
    states.check_gateway_access_observation(states.GATEWAY_ACCESS_UNVERIFIED, states.GATEWAY_ACCESS_REASON_NOT_RUN)
    return {**case.model_dump(), "observed": states.GATEWAY_ACCESS_UNVERIFIED,
            "status": states.gateway_access_case_status(case.expected, states.GATEWAY_ACCESS_UNVERIFIED),
            "reason": states.GATEWAY_ACCESS_REASON_NOT_RUN, "http_status": None, "request_id": None}


def _record_case(row, record):
    if request_outcome(record) == "not_dispatched":
        reason = (row["reason"] if row["reason"] in _IDENTITY_FAILURE_REASONS.values()
                  else states.GATEWAY_ACCESS_REASON_REQUEST_NOT_DISPATCHED)
        states.check_gateway_access_observation(states.GATEWAY_ACCESS_UNVERIFIED, reason)
        row.update(status=states.gateway_access_case_status(row["expected"], states.GATEWAY_ACCESS_UNVERIFIED),
                   observed=states.GATEWAY_ACCESS_UNVERIFIED, reason=reason,
                   http_status=None, request_id=record.id)
        return
    observed, reason = observation(record)
    states.check_gateway_access_observation(observed, reason)
    row.update(observed=observed, reason=reason,
               status=states.gateway_access_case_status(row["expected"], observed),
               http_status=record.http_status, request_id=record.id)


def _qualify_identities(target, rows, auth_header):
    by_credentials = {}
    digests = {}
    for row in rows:
        try:
            headers = identity_headers(target, row["identity"])
        except ValueError:
            row["reason"] = states.GATEWAY_ACCESS_REASON_IDENTITY_UNAVAILABLE
            continue
        names = {name.lower() for name in headers} - {"accept"}
        if auth_header is None and names.difference(LITELLM_NATIVE_KEY_HEADERS):
            row["reason"] = states.GATEWAY_ACCESS_REASON_AUTH_HEADER_UNVERIFIED
            continue
        credential = litellm_key_fingerprint(headers, auth_header=auth_header)
        if credential is None and names:
            row["reason"] = states.GATEWAY_ACCESS_REASON_AUTH_HEADER_UNVERIFIED
            continue
        digests[row["name"]] = credential_digest(headers)
        by_credentials.setdefault(credential, []).append(row)

    for cases in by_credentials.values():
        if {row["expected"] for row in cases} == {states.GATEWAY_ACCESS_ALLOW, states.GATEWAY_ACCESS_DENY}:
            for row in cases:
                row["reason"] = states.GATEWAY_ACCESS_REASON_IDENTITY_CONFLICT
                del digests[row["name"]]
    return digests


def _prepare_cases(config, target):
    settings = config.gateway_access
    if settings is None:
        return [], {}
    rows = [_row(case) for case in settings.cases]
    if settings.profile != "litellm":
        for row in rows:
            row["reason"] = states.GATEWAY_ACCESS_REASON_UNSUPPORTED_PROFILE
        return rows, {}
    return rows, _qualify_identities(target, rows, settings.auth_header)


def eligible_probe_count(config, target):
    if not configured_probe_count(config, target):
        return 0
    _, digests = _prepare_cases(config, target)
    return len(digests)


def _record_cutoff(run, target, rows, *, inference_finished=False):
    pending = [row for row in rows if row["reason"] == states.GATEWAY_ACCESS_REASON_NOT_RUN]
    if not pending:
        return
    # A deadline expiring after inference cannot explain a probe that did not run.
    stopped = ((run.endpoint_health[target]["abort_reason"] or run.reason)
               if inference_finished else run.stop_reason(target))
    if stopped == states.STOP_MAX_DURATION:
        reason = states.GATEWAY_ACCESS_REASON_RUN_DEADLINE
    elif stopped and stopped != states.STOP_REQUEST_BUDGET:
        reason = states.GATEWAY_ACCESS_REASON_RUN_STOPPED
    elif stopped or run.remaining(target) == 0:
        reason = states.GATEWAY_ACCESS_REASON_REQUEST_BUDGET_EXHAUSTED
    else:
        return
    for row in pending:
        row["reason"] = reason
    run.mark_unfinished(target, ["gateway_access"])


def record_unstarted(run, target):
    observation = run.observations[target.name]
    if "gateway_access" in observation:
        return
    rows, digests = _prepare_cases(run.config, target)
    observation["gateway_access"] = {"cases": rows}
    run.plan_condition(target.name, "gateway_access", planned=len(digests))
    _record_cutoff(run, target.name, rows, inference_finished=True)
    run.finish_condition(target.name, "gateway_access")


async def execute(run, target, client):
    settings = run.config.gateway_access
    rows, digests = _prepare_cases(run.config, target)
    run.observations[target.name]["gateway_access"] = {"cases": rows}
    run.plan_condition(target.name, "gateway_access", planned=len(digests))
    try:
        if not digests:
            return
        for case, row in zip(settings.cases, rows):
            if row["reason"] != states.GATEWAY_ACCESS_REASON_NOT_RUN:
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
                    record.error, states.GATEWAY_ACCESS_REASON_REQUEST_NOT_DISPATCHED)))
            if record is None and row["reason"] == states.GATEWAY_ACCESS_REASON_NOT_RUN:
                if not run.allowed(target.name, gateway_probe=True):
                    break
                row["reason"] = states.GATEWAY_ACCESS_REASON_REQUEST_NOT_DISPATCHED
    finally:
        _record_cutoff(run, target.name, rows)
        run.finish_condition(target.name, "gateway_access")


def result(run, target):
    settings = run.config.gateway_access
    data = run.observations[target.name].get("gateway_access", {})
    rows = data.get("cases", [])
    for row in rows:
        states.check_gateway_access_observation(row["observed"], row.get("reason"))
        states.gateway_access_case_status(row["expected"], row["observed"])
    matrix = {row["expected"] for row in rows} == {states.GATEWAY_ACCESS_ALLOW, states.GATEWAY_ACCESS_DENY}
    complete = bool(rows) and matrix and all(row["observed"] != states.GATEWAY_ACCESS_UNVERIFIED for row in rows)
    status = ("fail" if any(row["status"] == "fail" for row in rows) else
              "pass" if complete else "inconclusive")
    summary = ("Observed access differs from the declared policy." if status == "fail" else
               "Declared allow and deny cases matched the observed responses." if status == "pass" else
               "Access policy unverified: configure a supported profile and complete allow/deny cases with available test identities.")
    records = [record for record in run.records if record.target == target.name
               and "gateway_access" in record.check_ids]
    if (reason := preparation_failure_reason(records)) and status == "pass":
        status, summary = "inconclusive", reason
    metrics = {"evidence_version": EVIDENCE_VERSION,
               "profile": settings.profile if settings else "unknown", "cases": rows,
               "coverage_complete": complete, "has_allow_and_deny": matrix,
               "qualification": QUALIFICATION}
    return CheckResult(id="gateway_access", title="Gateway access policy", target=target.name,
                       status=status, summary=summary, metrics={"gateway_access": metrics},
                       required=bool(settings and settings.profile == "litellm" and settings.cases)
                                or any(row["request_id"] for row in rows) or run.config.checks == ["gateway_access"],
                       evidence_ids=[row["request_id"] for row in rows if row["request_id"]])
