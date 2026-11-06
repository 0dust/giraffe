from __future__ import annotations

import json
import re

from giraffe.client import REASONING_BUDGET_ERROR, _ProtocolError, _text
from giraffe.identities import LITELLM_NATIVE_KEY_HEADERS, identity_headers, litellm_key_fingerprint
from giraffe.models import CheckResult, RequestSpec


EVIDENCE_VERSION = 2
_TOOL_OPTIONS = ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call")
QUALIFICATION = ("Only the declared identities and endpoint/model were exercised. "
                 "These fixtures do not certify tenant isolation or comprehensive gateway security.")


def configured_cases(config):
    settings = config.gateway_access
    return settings.cases if settings else []


def assistant_reply(message, finish_reason):
    """Validate the fields used to qualify a plain assistant completion."""
    if message.get("role") != "assistant" or finish_reason not in ("stop", "length"):
        raise _ProtocolError("gateway access probe requires a completed assistant answer")
    if message.get("tool_calls") not in (None, []) or message.get("function_call") is not None:
        raise _ProtocolError("gateway access probe received an unexpected tool call")
    answer = _text(message.get("content"))
    reasoning = _text(message.get("reasoning_content"))
    alternate_reasoning = _text(message.get("reasoning"))
    refusal = _text(message.get("refusal"))
    return answer, reasoning or alternate_reasoning, finish_reason == "stop" and bool(refusal.strip())


def denial_kind(status, body):
    """Qualify only explicit LiteLLM authentication or model-access denials."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return "unknown"
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "unknown"
    error_type, message = error.get("type"), error.get("message", "")
    code = error.get("code", status)
    if (not isinstance(error_type, str) or not isinstance(message, str)
            or re.search(r"\b\w+Exception\b", message)
            or type(code) not in (int, str) or code not in (status, str(status))):
        return "unknown"
    if status == 401 and error_type in {"token_not_found_in_db", "expired_key"}:
        return "authentication"
    if status == 403 and error_type in {
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
    if record.status == "completed" and record.http_status == 200:
        if record.valid:
            return "allow", "valid_completion"
        if record._gateway_access_refusal:
            return "allow", "assistant_refusal"
        if (record.error == REASONING_BUDGET_ERROR
                and record.reasoning_tokens == record.completion_tokens == record.requested_max_tokens > 0):
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
        by_credentials.setdefault(credential, []).append(row)

    for cases in by_credentials.values():
        if {row["expected"] for row in cases} == {"allow", "deny"}:
            for row in cases:
                row["reason"] = "identity_conflict"


async def execute(run, target, client):
    settings = run.config.gateway_access
    rows = [_row(case) for case in configured_cases(run.config)]
    run.observations[target.name]["gateway_access"] = {"cases": rows}
    if settings is None or settings.profile != "litellm":
        for row in rows:
            row["reason"] = "unsupported_profile"
        return
    _qualify_identities(target, rows, settings.auth_header)
    for case, row in zip(settings.cases, rows):
        if row["reason"] != "not_run":
            continue
        if not run.allowed(target.name):
            row["reason"] = "run_budget_or_stop"
            continue
        spec = RequestSpec(fixture_id="gateway-access-" + case.name,
            scenario="gateway_access", check_ids=["gateway_access"], identity=case.identity,
            messages=[{"role": "user", "content": "Reply with OK."}], stream=False,
            options=dict.fromkeys(_TOOL_OPTIONS),
            max_tokens=settings.max_output_tokens,
            request_timeout_seconds=settings.request_timeout_seconds,
            workload={"case": case.name, "expected": case.expected})
        await run.request(target, client, spec, finalize_record=lambda record: _record_case(row, record))


def result(run, target):
    settings = run.config.gateway_access
    data = run.observations[target.name].get("gateway_access", {})
    rows = data["cases"] if "cases" in data else [_row(case) for case in configured_cases(run.config)]
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
