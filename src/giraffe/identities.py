"""Request-scoped credentials for explicit gateway probes."""
from contextvars import ContextVar
from contextlib import contextmanager
from http.cookiejar import CookieJar
import logging
import os


identity_request = ContextVar("giraffe_identity_request", default=False)


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


def identity_headers(target, alias):
    identity = target.test_identities.get(alias)
    if identity is None:
        raise ValueError("Test identity credentials are unavailable.")
    headers = {"Accept": "application/json, text/event-stream"}
    refs = dict(identity.headers_env)
    if identity.api_key_env:
        refs["Authorization"] = identity.api_key_env
    for header, ref in refs.items():
        value = os.environ.get(ref)
        if not value or any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("Test identity credentials are unavailable.")
        headers[header] = "Bearer " + value if header == "Authorization" and identity.api_key_env else value
    return headers


def discard_identity_response(record):
    """Gateway probes retain counts/timing, never arbitrary credential echoes."""
    record.output = record.reasoning = ""
    record.observed_model = record.backend_id = record.server_request_id = None
    record.answer_hash = record.assistant_message = record.logprob_evidence = None
    record.tool_calls = []
    if record.finish_reason not in {"stop", "length", "tool_calls", "function_call", "content_filter", "error", None}:
        record.finish_reason = None
