"""Request-scoped credentials for explicit gateway probes."""
from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
from http.cookiejar import CookieJar
import logging
import os


identity_request = ContextVar("giraffe_identity_request", default=False)
LITELLM_NATIVE_KEY_HEADERS = (
    "x-litellm-api-key", "authorization", "api-key", "x-api-key",
    "x-goog-api-key", "ocp-apim-subscription-key",
)


class _IdentityLogFilter(logging.Filter):
    def filter(self, record):
        return not identity_request.get()


_log_filter = _IdentityLogFilter()


@contextmanager
def identity_scope(selected):
    # HTTPX 0.28 logs server reason phrases; HTTPcore logs headers and errors.
    # Filter only the active probe task, including inherited cleanup tasks.
    if selected:
        for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                     "httpcore.proxy", "httpcore.socks"):
            logging.getLogger(name).addFilter(_log_filter)
    token = identity_request.set(selected)
    try:
        yield
    finally:
        identity_request.reset(token)


class IdentityCookieJar(CookieJar):
    def extract_cookies(self, response, request):
        # HTTPX extracts cookies in the send task. Context-local suppression
        # keeps concurrent selected identities out of the shared-key cookie jar.
        if not identity_request.get():
            super().extract_cookies(response, request)


def gateway_identities_only(checks):
    """GPU telemetry can accompany identity probes without shared inference."""
    return (any(check.startswith("gateway_") for check in checks)
            and all(check == "gpu" or check.startswith("gateway_") for check in checks))


def identity_headers(target, alias):
    identity = target.test_identities.get(alias)
    if identity is None:
        raise ValueError("Test identity credentials are unavailable.")
    return credential_headers(identity)


def credential_headers(identity):
    headers = {"Accept": "application/json, text/event-stream"}
    refs = dict(identity.headers_env)
    if identity.api_key_env:
        refs["Authorization"] = identity.api_key_env
    for header, ref in refs.items():
        value = os.environ.get(ref)
        if not value or value != value.strip() or any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("Test identity credentials are unavailable.")
        headers[header] = "Bearer " + value if header == "Authorization" and identity.api_key_env else value
    return headers


def litellm_key_fingerprint(headers, *, auth_header: str | None = None):
    """Fingerprint the virtual key selected by LiteLLM's header precedence."""
    headers = {name.lower(): value for name, value in headers.items()}
    custom = auth_header.lower() if auth_header else None
    header = custom if custom in headers else next(
        (name for name in LITELLM_NATIVE_KEY_HEADERS if headers.get(name)), None)
    if header is None:
        return None
    value = headers[header]
    if header in {custom, "authorization", "x-litellm-api-key"}:
        scheme, separator, token = value.partition(" ")
        if separator and scheme in {"Bearer", "bearer", "Basic"}:
            value = token
        elif header == custom or header == "authorization":
            return None
    if not value or any(char.isspace() for char in value):
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def discard_identity_response(record):
    """Gateway probes retain counts/timing, never arbitrary credential echoes."""
    record.output = record.reasoning = ""
    record.observed_model = record.backend_id = record.server_request_id = None
    record.answer_hash = record.assistant_message = record.logprob_evidence = None
    record.tool_calls = []
    if record.finish_reason not in {"stop", "length", "tool_calls", "function_call", "content_filter", "error", None}:
        record.finish_reason = None
