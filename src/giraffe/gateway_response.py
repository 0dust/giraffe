"""Allowlisted LiteLLM response evidence; raw errors never leave this parser."""
import json
import re

from .models import GatewayResponseEvidence


def response_evidence(status, body=b"", headers=None, *, declared_priority=None):
    kind = None if 200 <= status < 300 else "unknown"
    if status in {401, 403}:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            data = None
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            error_type, message = error.get("type"), error.get("message", "")
            provider_error = not isinstance(message, str) or re.search(r"\b\w+Exception\b", message)
            code = error.get("code", status)
            matching_status = type(code) in (int, str) and code in (status, str(status))
            if isinstance(error_type, str) and matching_status and not provider_error:
                if status == 401 and error_type in {"token_not_found_in_db", "expired_key"}:
                    kind = "authentication"
                if status == 403 and error_type in {
                    "key_model_access_denied", "team_model_access_denied", "user_model_access_denied",
                    "org_model_access_denied", "project_model_access_denied",
                }:
                    kind = "model_access"
    evidence = GatewayResponseEvidence(error_kind=kind)
    headers = headers or {}
    for name, field in (("x-ratelimit-limit-requests", "request_limit"),
                        ("x-ratelimit-remaining-requests", "request_remaining"),
                        ("x-ratelimit-limit-tokens", "token_limit"),
                        ("x-ratelimit-remaining-tokens", "token_remaining")):
        value = headers.get(name)
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
            setattr(evidence, field, int(value))
    if headers.get("x-litellm-rate-limiter-version") == "v3":
        evidence.limiter_version = "v3"
        limit = headers.get("x-ratelimit-priority_model-limit-requests")
        if isinstance(limit, str) and re.fullmatch(r"[0-9]{1,12}", limit):
            evidence.priority_request_limit = int(limit)
    retry = headers.get("retry-after")
    if isinstance(retry, str) and re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,6})?", retry):
        evidence.retry_after_seconds = float(retry)
    tier = headers.get("x-litellm-priority")
    if declared_priority and isinstance(tier, str) and len(tier) <= 64:
        evidence.priority_matches = tier == declared_priority
    if status != 429:
        return evidence
    evidence.policy_rejection = "unknown"
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return evidence
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict) or error.get("type") != "throttling_error" or error.get("code") not in (429, "429"):
        return evidence
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 8192 or re.search(r"\b\w+Exception\b", message):
        return evidence
    key = re.fullmatch(r"Rate limit exceeded for api_key: [^\r\n]{1,256}\. Limit type: requests\. Current limit: ([0-9]{1,12}), Remaining: ([0-9]{1,12})\. Limit resets at: [^\r\n]{1,128}", message)
    if key:
        evidence.error_kind, evidence.policy_rejection = "rate_limit", "key_rpm"
        evidence.rate_limit_unit = "requests"
        evidence.request_limit, evidence.request_remaining = map(int, key.groups())
    elif message.startswith(("Priority-based rate limit exceeded. Model: ", "Model capacity reached for ")):
        unit = re.search(r", Rate limit type: (requests|tokens),", message)
        rpm = re.search(r", Model RPM: ([0-9]{1,12}), Remaining: 0(?:,|$)", message)
        if not unit or not rpm:
            return evidence
        evidence.error_kind = "rate_limit"
        evidence.policy_rejection = "priority_pool" if message.startswith("Priority-based") else "model_capacity"
        evidence.rate_limit_unit, evidence.model_rpm = unit[1], int(rpm[1])
        if declared_priority:
            tier_match = re.search(r", Priority: ([^,\r\n]{1,64}),", message)
            if tier_match:
                matches = tier_match[1] == declared_priority
                evidence.priority_matches = matches if evidence.priority_matches is None else matches and evidence.priority_matches
        saturation = re.search(r", Model saturation: ([0-9]{1,3}(?:\.[0-9]{1,6})?)%$", message)
        if saturation:
            value = float(saturation[1])
            if value <= 100:
                evidence.saturation_percent = value
    return evidence
