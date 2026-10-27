"""Shared bounded endpoint/model checks without run artifacts."""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator

from .caps import (
    MAX_PREFLIGHT_BODY_BYTES, MAX_PREFLIGHT_HEADER_CHARS,
)
from giraffe.client import LLMClient, _headers, completion_url
from giraffe.models import Model, RequestSpec, RunConfig, Target


class PreflightError(Exception):
    def __init__(self, message: str, *, blocking: bool = True):
        super().__init__(message)
        self.blocking = blocking


class _PreflightClient(LLMClient):
    def _request_headers(self) -> dict[str, str]:
        headers = httpx.Headers(super()._request_headers())
        headers["X-Giraffe-Preflight"] = "1"
        return dict(headers)


class APIKeyInput(Model):
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("api_key", mode="before")
    @classmethod
    def validate_api_key(cls, value):
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            raise ValueError("API key must be text")
        raw = raw.strip()
        if not raw:
            return None
        if len(raw) > MAX_PREFLIGHT_HEADER_CHARS or any(ord(c) < 33 or ord(c) > 126 for c in raw):
            raise ValueError("API key must contain printable ASCII characters without whitespace")
        return raw


def attach_api_key(target: Target, key: SecretStr | None) -> None:
    if key is None:
        return
    if any(header.lower() == "authorization" for header in target.headers_env):
        raise ValueError("Use either a pasted API key or a custom Authorization header.")
    target._api_key = key
    # Export a usable reference, never silently turn an authenticated setup into an open one.
    target.api_key_env = target.api_key_env or "GIRAFFE_API_KEY"


class ModelDiscovery(APIKeyInput):
    url: str
    api_key_env: str | None = None
    headers_env: dict[str, str] = Field(default_factory=dict)
    ca_bundle: str | None = None
    proxy: str | None = None

    def target(self, model: str = "discovery") -> Target:
        target = Target(name="connection", model=model, url=self.url.strip(),
                        api_key_env=self.api_key_env, headers_env=self.headers_env)
        attach_api_key(target, self.api_key)
        return target

    def configuration(self, model: str, *, request_options: dict | None = None) -> RunConfig:
        return RunConfig(targets=[self.target(model)], checks=["access"], concurrency=1,
                         max_requests=1, max_duration_seconds=15, request_timeout_seconds=15,
                         max_output_tokens=32, samples=1, stream=False,
                         ca_bundle=self.ca_bundle, proxy=self.proxy,
                         request_options=request_options or {})

    @model_validator(mode="after")
    def validate_endpoint(self) -> ModelDiscovery:
        self.configuration("discovery")
        return self


class TokenizerSelection(Model):
    model: str = Field(default="", max_length=512)
    token_id: str = Field(default="auto", max_length=512)


class ConnectionTest(ModelDiscovery):
    model: str | None = None
    request_options: dict = Field(default_factory=dict)

    @field_validator("model", "api_key_env", mode="before")
    @classmethod
    def trim_optional(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_request_options(self) -> ConnectionTest:
        self.configuration(self.model or "connection", request_options=self.request_options)
        return self


async def discover_models(body: ModelDiscovery, *, allow_manual: bool = False,
                          include_tokenizers: bool = False) -> dict:
    """Read model IDs on explicit request; never send a completion or follow redirects."""
    target = body.target()
    try:
        headers = _headers(target)
    except ValueError as error:
        raise PreflightError(str(error)) from None
    url = completion_url(target.url).removesuffix("/chat/completions") + "/models"
    try:
        verify = ssl.create_default_context(cafile=body.ca_bundle) if body.ca_bundle else True
        client = httpx.AsyncClient(
            timeout=6, follow_redirects=False, verify=verify, proxy=body.proxy, trust_env=True,
        )
    except OSError:
        raise PreflightError("Could not load the TLS trust file. Check ca_bundle "
                            "on the machine running Giraffe.") from None
    except (ValueError, ImportError, httpx.InvalidURL):
        raise PreflightError("Could not use the connection settings. "
                            "Check the endpoint and proxy configuration.") from None
    try:
        async with asyncio.timeout(8), client:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in {401, 403}:
                    raise PreflightError("Server denied access. Check your authentication: "
                                        "the API key or environment variable in the API key section.")
                if response.status_code != 200:
                    if allow_manual:
                        return {"models": [], "message": "The server does not provide a model list. "
                                "Enter its model name to test the connection."}
                    raise PreflightError("This server did not return a model list. "
                                        "Check the endpoint or enter the model name manually.")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_PREFLIGHT_BODY_BYTES:
                        if allow_manual:
                            return {"models": [], "message": "The model list is too large. "
                                    "Enter the model name to test the connection."}
                        raise PreflightError("The model list is too large. Enter the model name manually.")
        payload = json.loads(content)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("Unexpected model list")
        models = list(dict.fromkeys(item["id"] for item in data if isinstance(item, dict)
                                   and isinstance(item.get("id"), str) and item["id"].strip()))
    except (httpx.HTTPError, httpx.InvalidURL, TimeoutError):
        raise PreflightError("Could not reach the model list. Check that the server is running "
                            "and check the address, TLS trust and proxy settings.") from None
    except (ValueError, UnicodeError):
        if allow_manual:
            return {"models": [], "message": "The server's model list could not be read. "
                    "Enter the model name to test the connection."}
        raise PreflightError("The server returned an unreadable model list. "
                            "You can still enter the model name manually.") from None
    extra = {}
    if include_tokenizers:
        import re
        roots = {}
        for item in data:
            if not isinstance(item, dict) or item.get("id") not in models:
                continue
            root = item.get("root")
            # A remote filesystem path must never become a local file lookup.
            if (isinstance(root, str) and re.fullmatch(r"[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}", root)
                    and ".." not in root and not Path(root).exists()):
                roots.setdefault(item["id"], set()).add(root)
        extra["tokenizers"] = {model: next(iter(values)) for model, values in roots.items() if len(values) == 1}
    return {**extra, "models": models, "message": "Model list received. No inference requests were sent."
            if models else "The server returned no models. Enter an installed model name manually."}


async def check_connection(body: ConnectionTest) -> dict:
    """Resolve a model, then send at most one bounded inference without saving a run."""
    model = body.model
    try:
        async with asyncio.timeout(15):
            if not model:
                discovered = await discover_models(body, allow_manual=True)
                models = discovered["models"]
                if len(models) != 1:
                    return {"status": "needs_model", "models": models,
                            "message": "Choose a model, then test its connection."
                            if models else discovered["message"]}
                model = models[0]
            config = body.configuration(model, request_options=body.request_options)
            return await check_target(config.targets[0], config)
    except TimeoutError:
        raise PreflightError("Connection test timed out after 15 seconds. "
                            "Check that the model is loaded, then try again.") from None
    except OSError:
        raise PreflightError("Could not load the TLS trust file. "
                            "Check ca_bundle on the machine running Giraffe.") from None
    except (ValueError, ImportError, httpx.HTTPError, httpx.InvalidURL):
        raise PreflightError("Could not connect. Check the endpoint, authentication, "
                            "TLS trust and proxy settings.") from None


async def check_target(target: Target, config: RunConfig) -> dict:
    """Send one bounded inference using the run's target and transport settings."""
    config = config.model_copy(update={"request_timeout_seconds": 15, "max_output_tokens": 32,
                                       "stream": False})
    try:
        async with asyncio.timeout(15):
            try:
                _headers(target)
            except ValueError as error:
                raise PreflightError(str(error)) from None
            async with _PreflightClient(target, config) as client:
                record = await client.execute(RequestSpec(
                    fixture_id="connection", scenario="connection", check_ids=["access"],
                    messages=[{"role": "user", "content": "Reply with OK."}],
                    max_tokens=32, stream=False,
                ))
    except (TimeoutError, httpx.TimeoutException):
        raise PreflightError("Connection test timed out after 15 seconds. "
                            "Check that the model is loaded, then try again.", blocking=False) from None
    except OSError:
        raise PreflightError("Could not load the TLS trust file. "
                            "Check ca_bundle on the machine running Giraffe.") from None
    except (ValueError, ImportError, httpx.InvalidURL):
        raise PreflightError("Could not connect. Check the endpoint, authentication, "
                            "TLS trust and proxy settings.") from None
    except httpx.HTTPError as error:
        raise PreflightError("Could not connect. Check the endpoint, authentication, "
                            "TLS trust and proxy settings.",
                            blocking=isinstance(error, (httpx.ConnectError, httpx.UnsupportedProtocol))) from None
    if record.status in {"timeout", "cancelled"}:
        raise PreflightError("Connection test timed out after 15 seconds. "
                            "Check that the model is loaded, then try again.", blocking=False)
    if record.http_status in {401, 403}:
        raise PreflightError("Server denied access. Check your authentication: the API key, "
                            "environment variable and header settings.")
    if not record.valid:
        if record.http_status in {400, 404, 422} and record.workload.get("model_not_served"):
            discovery = ModelDiscovery(url=target.url, api_key_env=target.api_key_env,
                                       headers_env=target.headers_env, ca_bundle=config.ca_bundle,
                                       proxy=config.proxy, api_key=target._api_key)
            try:
                available = (await discover_models(discovery, allow_manual=True))["models"]
            except PreflightError:
                available = []
            message = f'Model "{target.model}" not served at {target.url}.'
            if target.model not in available:
                if available:
                    message += " Available: " + ", ".join(available) + "."
                raise PreflightError(message)
        blocking = record.http_status == 404 and not record.workload.get("model_not_served")
        if record.http_status is None:
            error = record.error or ""
            timeout = "Timeout" in error
            blocking = not timeout and any(name in error for name in (
                "ConnectError", "SSLError", "ConnectionError", "ConnectionRefusedError",
                "ProxyError", "UnsupportedProtocol", "InvalidURL"))
            message = ("Connection test timed out after 15 seconds. Check that the model is loaded, then try again."
                       if timeout else "Could not reach the model. Check the endpoint, TLS trust and proxy settings.")
        elif not 200 <= record.http_status < 300:
            message = (f"The server returned HTTP {record.http_status}. "
                       "Check the model name and OpenAI-compatible endpoint.")
        else:
            message = ("The server responded but did not return a usable answer within the "
                       "32-token test. Check the model's settings or continue with its name.")
        raise PreflightError(message, blocking=blocking)
    return {"status": "connected", "model": target.model, "latency_ms": round(record.elapsed_ms, 1),
            "message": "The model returned an answer. Your endpoint is ready for checks."}
