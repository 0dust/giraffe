"""Allowlisted LiteLLM response evidence; raw errors never leave this parser."""
from decimal import Decimal
import json
import re

from .models import GatewayResponseEvidence


def _model_group(value):
    if (isinstance(value, str) and 0 < len(value) <= 256 and value.isprintable()
            and "," not in value and value == value.strip()):
        return value
    return None


def _saturation_evidence(suffix, header):
    match = re.fullmatch(r", Model saturation: ([0-9]{1,3}(?:\.[0-9]{1,6})?)%", suffix)
    if not match or (body := Decimal(match[1])) > 100:
        return None, None
    body_places = body.as_tuple().exponent
    body_radius = Decimal(5).scaleb(body_places - 1)
    lower, upper = max(Decimal(0), body - body_radius), min(Decimal(100), body + body_radius)
    if header is None:
        return float(body), (lower, upper)
    match = re.fullmatch(r"([0-9]{1,3}\.[0-9]{2})%", header) if isinstance(header, str) else None
    if not match or (precise := Decimal(match[1])) > 100:
        return None, None
    # LiteLLM rounds the same value separately for the body and header.
    header_radius = Decimal("0.005")
    lower, upper = max(lower, precise - header_radius), min(upper, precise + header_radius)
    if lower >= upper:
        return None, None
    return float(precise if body_places > -2 else body), (lower, upper)


def response_evidence(status, body=b"", headers=None, *, declared_priority=None, expected_key_hash=None):
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
            if isinstance(error_type, str) and not provider_error:
                if status == 401 and error_type in {"token_not_found_in_db", "expired_key", "auth_error"}:
                    kind = "authentication"
                if status == 403 and error_type in {
                    "key_model_access_denied", "team_model_access_denied", "user_model_access_denied",
                    "org_model_access_denied", "project_model_access_denied",
                }:
                    kind = "model_access"
    evidence = GatewayResponseEvidence(error_kind=kind)
    headers = headers or {}
    evidence._response_cache_hit_reported = 200 <= status < 300 and "x-litellm-cache-key" in headers
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
    no_reported_fallback = headers.get("x-litellm-attempted-fallbacks") in (None, "0")
    if status != 429:
        if 200 <= status < 300 and evidence.priority_request_limit is not None and no_reported_fallback:
            evidence._priority_model_group = _model_group(headers.get("x-litellm-model-group"))
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
    key = re.fullmatch(r"Rate limit exceeded for api_key: ([0-9a-f]{64})\. Limit type: requests\. Current limit: ([0-9]{1,12}), Remaining: ([0-9]{1,12})\. Limit resets at: [^\r\n]{1,128}", message)
    if key and key[1] == expected_key_hash:
        evidence.error_kind, evidence.policy_rejection = "rate_limit", "key_rpm"
        evidence.rate_limit_unit = "requests"
        evidence.request_limit, evidence.request_remaining = int(key[2]), int(key[3])
    elif message.startswith(("Priority-based rate limit exceeded. Model: ", "Model capacity reached for ")):
        unit = re.search(r", Rate limit type: (requests|tokens),", message)
        rpm = re.search(r", Model RPM: ([0-9]{1,12}), Remaining: 0(?=,|$)", message)
        if not unit or not rpm:
            return evidence
        evidence.error_kind = "rate_limit"
        evidence.policy_rejection = "priority_pool" if message.startswith("Priority-based") else "model_capacity"
        evidence.rate_limit_unit, evidence.model_rpm = unit[1], int(rpm[1])
        model = re.match(r"Priority-based rate limit exceeded\. Model: ([^,]{1,256}), Priority: ", message)
        if model and no_reported_fallback and headers.get("x-litellm-model-group") in (None, model[1]):
            evidence._priority_model_group = _model_group(model[1])
        if declared_priority:
            tier_match = re.search(r", Priority: ([^,\r\n]{1,64}),", message)
            if tier_match:
                matches = tier_match[1] == declared_priority
                evidence.priority_matches = matches if evidence.priority_matches is None else matches and evidence.priority_matches
        # Only the canonical TPM field between the rate type and RPM fields can prove absence.
        evidence._model_tpm_unconfigured = message[unit.end():rpm.start()] == " Model TPM: not configured"
        evidence.saturation_percent, evidence._saturation_bounds = _saturation_evidence(
            message[rpm.end():], headers.get("x-litellm-saturation"))
    return evidence
