from __future__ import annotations

import os

from giraffe.models import CheckResult, RequestSpec


QUALIFICATION = ("Only the declared identities and endpoint/model were exercised. "
                 "These fixtures do not certify tenant isolation or comprehensive gateway security.")


def configured_cases(config):
    settings = config.gateway_access
    return settings.cases if settings else []


def observation(record):
    if record is None:
        return "unverified", "not_run"
    if record.status == "cancelled":
        return "unverified", "request_cancelled"
    if record.status == "completed" and record.http_status == 200 and record.valid:
        return "allow", "valid_completion"
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


async def execute(run, target, client):
    settings = run.config.gateway_access
    rows = [_row(case) for case in configured_cases(run.config)]
    run.observations[target.name]["gateway_access"] = {"cases": rows}
    if settings is None or settings.profile != "litellm":
        for row in rows:
            row["reason"] = "unsupported_profile"
        return
    for case, row in zip(settings.cases, rows):
        identity = target.test_identities.get(case.identity)
        refs = ([identity.api_key_env] if identity and identity.api_key_env else []) + (
            list(identity.headers_env.values()) if identity else [])
        if identity is None or any(not os.environ.get(ref) for ref in refs):
            row["reason"] = "identity_unavailable"
            continue
        if not run.allowed(target.name):
            row["reason"] = "run_budget_or_stop"
            continue
        spec = RequestSpec(fixture_id="gateway-access-" + case.name,
            scenario="gateway_access", check_ids=["gateway_access"], identity=case.identity,
            messages=[{"role": "user", "content": "Reply with OK."}], stream=False,
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
    metrics = {"profile": settings.profile if settings else "unknown", "cases": rows,
               "coverage_complete": complete, "has_allow_and_deny": matrix,
               "qualification": QUALIFICATION}
    return CheckResult(id="gateway_access", title="Gateway access policy", target=target.name,
                       status=status, summary=summary, metrics={"gateway_access": metrics},
                       required=bool(settings and settings.profile == "litellm" and settings.cases)
                                or any(row["request_id"] for row in rows) or run.config.checks == ["gateway_access"],
                       evidence_ids=[row["request_id"] for row in rows if row["request_id"]])
