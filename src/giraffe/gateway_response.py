"""Allowlisted LiteLLM response evidence; raw errors never leave this parser."""
import json
import re

from .models import GatewayResponseEvidence


def response_evidence(status, body=b"", headers=None):
    kind = None if 200 <= status < 300 else "unknown"
    if status in {401, 403}:
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
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
    return GatewayResponseEvidence(error_kind=kind)
