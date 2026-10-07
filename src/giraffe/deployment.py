"""Bounded, read-only deployment evidence. A chat URL is not a host inventory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

CATEGORIES = {
    "model": {
        "requested",
        "served",
        "revision",
        "digest",
        "quantization",
        "tokenizer",
        "chat_template",
    },
    "runtime": {"engine", "version", "commit", "image", "image_digest"},
    "serving": {
        "launch_arguments",
        "context_limit",
        "max_num_seqs",
        "max_num_batched_tokens",
        "scheduler",
        "gpu_memory_utilization",
        "kv_cache",
        "prefix_caching",
        "resolved_settings",
        "configuration",
        "instrumentation",
    },
    "backend": {"attention", "kernel", "graphs", "compilation"},
    "hardware": {
        "gpu_type",
        "gpu_count",
        "tensor_parallel",
        "pipeline_parallel",
        "layout",
        "interconnect",
    },
    "software": {"driver", "cuda", "rocm", "libraries"},
}
SECRET = re.compile(
    r"(?i)(api[ _-]?key|authorization|password|passwd|credential|secret|access[ _-]?token|bearer|cookie)"
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:api[-_]?key|password|secret|access[-_]?token)\s*[=:]\s*[^\s,;]+|"
    r"\bsk-[a-zA-Z0-9_-]+|\bgh[pousr]_[a-zA-Z0-9]+|\$\{?\w+\}?)"
)
_URL = re.compile(r"https?://[^\s\"'<>]+")
REDACTED = "[redacted]"


def now():
    return datetime.now(timezone.utc).isoformat()


def sanitize(value, *, depth=0, bounded=True):
    """Redact structured credentials and credential-bearing text before any persistence."""
    if depth > (8 if bounded else 24):
        return "[depth limit]"
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:128] if bounded else value.items():
            key = str(key)[:128]
            result[key] = (
                REDACTED
                if SECRET.search(key)
                or key.lower() in {"token", "tokens", "headers", "env", "environment"}
                else sanitize(child, depth=depth + 1, bounded=bounded)
            )
        return result
    if isinstance(value, list):
        return [
            sanitize(v, depth=depth + 1, bounded=bounded)
            for v in (value[:256] if bounded else value)
        ]
    if isinstance(value, str):

        def clean_url(match):
            try:
                url = urlsplit(match.group())
                if url.username or url.password or url.query or url.fragment:
                    host = url.hostname or "redacted"
                    if url.port:
                        host += f":{url.port}"
                    return urlunsplit((url.scheme, host, url.path, "", "")) + "[redacted]"
            except ValueError:
                return REDACTED
            return match.group()

        return _SECRET_TEXT.sub(REDACTED, _URL.sub(clean_url, value))[:8192]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported value]"


# Keep ordered safe argv only: arbitrary positional shell code/environment is never retained.
LAUNCH_FLAGS = {
    "--model",
    "--served-model-name",
    "--revision",
    "--tokenizer",
    "--tokenizer-revision",
    "--quantization",
    "--max-model-len",
    "--max-num-seqs",
    "--max-num-batched-tokens",
    "--gpu-memory-utilization",
    "--num-gpu-blocks-override",
    "--kv-cache-dtype",
    "--tensor-parallel-size",
    "--pipeline-parallel-size",
    "--attention-backend",
    "--scheduling-policy",
    "--enable-prefix-caching",
    "--no-enable-prefix-caching",
    "--enforce-eager",
    "--compilation-config",
    "--host",
    "--port",
    "--config",
}


def safe_launch(command):
    args = shlex.split(command) if isinstance(command, str) else list(command)
    if len(args) > 256:
        raise ValueError("launch command exceeds 256 arguments")
    result, index = [], 0
    while index < len(args):
        arg = str(args[index])
        flag, equals, value = arg.partition("=")
        if flag in LAUNCH_FLAGS:
            result.append(sanitize(arg))
            if not equals and index + 1 < len(args) and not str(args[index + 1]).startswith("--"):
                index += 1
                result.append(sanitize(str(args[index])))
        elif flag.startswith("--"):
            result.append(flag + "=" + REDACTED)
            if not equals and index + 1 < len(args) and not str(args[index + 1]).startswith("--"):
                index += 1
        elif index < 3 and arg in {"vllm", "serve", "ollama", "run"}:
            result.append(arg)
        else:
            result.append(REDACTED)
        index += 1
    return result


def safe_metadata(data):
    if not isinstance(data, dict) or len(json.dumps(data, allow_nan=False)) > 65536:
        raise ValueError("deployment metadata must be an object of at most 64 KiB")
    result = {}
    for category, fields in CATEGORIES.items():
        source = data.get(category, {})
        if isinstance(source, dict):
            result[category] = sanitize({k: v for k, v in source.items() if k in fields})
    aliases = {
        "model": ("model", "requested"),
        "revision": ("model", "revision"),
        "quantization": ("model", "quantization"),
        "served_model_name": ("model", "served"),
        "max_model_len": ("serving", "context_limit"),
        "max_num_seqs": ("serving", "max_num_seqs"),
        "max_num_batched_tokens": ("serving", "max_num_batched_tokens"),
        "gpu_memory_utilization": ("serving", "gpu_memory_utilization"),
        "enable_prefix_caching": ("serving", "prefix_caching"),
        "scheduling_policy": ("serving", "scheduler"),
        "attention_backend": ("backend", "attention"),
        "tensor_parallel_size": ("hardware", "tensor_parallel"),
        "pipeline_parallel_size": ("hardware", "pipeline_parallel"),
    }
    for key, (category, field) in aliases.items():
        if key in data and not isinstance(data[key], dict):
            result.setdefault(category, {})[field] = sanitize(data[key])
    if data.get("launch_command"):
        result.setdefault("serving", {})["launch_arguments"] = safe_launch(data["launch_command"])
    # An explicitly named, bounded extension is the only arbitrary metadata channel.
    result["extension"] = (
        sanitize(data.get("extension", {})) if isinstance(data.get("extension", {}), dict) else {}
    )
    return result


def load_metadata(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("deployment metadata file exceeds 64 KiB")
    try:
        data = json.loads(path.read_text())
        result = safe_metadata(data)
        for key in ("scope", "collected_at"):
            if key in data and data[key] is not None:
                if not isinstance(data[key], str):
                    raise ValueError("metadata scope and collection time must be strings")
                result[key] = sanitize(data[key])[:512]
        return result
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("deployment metadata file is not valid bounded JSON") from exc


def _fact(value, source, scope, stamp, status=None):
    stamp = sanitize(stamp) if isinstance(stamp, str) else None
    try:
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        ).total_seconds()
    except (ValueError, TypeError, AttributeError):
        age = None
    return {
        "value": sanitize(value),
        "provenance": source,
        "scope": sanitize(scope),
        "collected_at": stamp,
        "status": status
        or (
            "stale"
            if age is not None and age > 3600
            else "runtime_reported"
            if source in {"runtime-reported", "configured-collector"}
            else "unverified"
        ),
        "freshness": "unknown"
        if age is None
        else "collection time; underlying update time unknown",
    }


async def _read(client, url, *, body=None):
    async with client.stream("POST" if body is not None else "GET", url, json=body) as response:
        response.raise_for_status()
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > 65536:
                raise ValueError("discovery response exceeds 64 KiB")
            data.extend(chunk)
    return json.loads(data)


async def snapshot(target, config, deadline, stop):
    import time
    from giraffe.client import _headers

    deployment = target.deployment
    result = {
        "schema_version": "1",
        "target": target.name,
        "route": target.route,
        "parent": target.parent,
        "scope": sanitize(deployment.scope),
        "collected_at": now(),
        "fields": {},
        "continuity": "unverified",
        "discovery": [],
        "limitations": [
            "One configured endpoint/collector; other replicas unverified.",
            "Chat APIs do not establish host layout, containers, launch settings or kernels.",
        ],
    }
    intended = safe_metadata(deployment.model_dump())
    imported_fields = set()
    imported_scope, imported_time = None, None
    if deployment.metadata_file:
        try:
            imported = load_metadata(deployment.metadata_file)
            imported_scope = imported.pop("scope", None)
            imported_time = imported.pop("collected_at", None)
            for category, values in imported.items():
                intended.setdefault(category, {}).update(values)
                imported_fields.update(f"{category}.{key}" for key in values)
            result["discovery"].append({"status": "available", "source": "local metadata file"})
        except (ValueError, OSError):
            result["discovery"].append(
                {
                    "status": "metadata_file_unavailable",
                    "reason": "Local metadata missing or invalid; endpoint testing remains available.",
                }
            )
    if deployment.launch_command:
        intended.setdefault("serving", {})["launch_arguments"] = safe_launch(
            deployment.launch_command
        )
    for category, values in intended.items():
        for key, value in values.items():
            path = f"{category}.{key}"
            result["fields"][path] = {
                "configured": _fact(
                    value,
                    "user-supplied metadata file" if path in imported_fields else "user-supplied",
                    imported_scope or deployment.scope
                    if path in imported_fields
                    else deployment.scope,
                    (imported_time or deployment.collected_at or result["collected_at"])
                    if path in imported_fields
                    else deployment.collected_at or result["collected_at"],
                )
            }
    result["fields"]["model.requested"] = {
        "configured": _fact(target.model, "user-supplied", deployment.scope, result["collected_at"])
    }
    result["identity"] = sanitize(target.identity)
    result["intended_change"] = sanitize(deployment.intended_change)
    result["unknown_fields"] = sorted(
        f"{c}.{f}"
        for c, fields in CATEGORIES.items()
        for f in fields
        if f"{c}.{f}" not in result["fields"]
    )
    if deployment.discovery == "none" and not deployment.collector_url:
        result["discovery"].append({"status": "not_enabled"})
        return result
    parsed = urlsplit(target.url)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    remaining = min(config.metrics_timeout_seconds, deadline - time.monotonic())
    if stop.is_set() or remaining <= 0:
        result["discovery"].append({"status": "run_stopped"})
        return result
    import ssl

    verify = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else True
    async with httpx.AsyncClient(
        timeout=remaining, verify=verify, proxy=config.proxy, follow_redirects=False
    ) as client:
        try:
            async with asyncio.timeout(remaining):
                reported = {}
                if deployment.discovery != "none":
                    client.headers.update(_headers(target))
                    if deployment.discovery == "ollama":
                        version = await _read(client, origin + "/api/version")
                        model = await _read(
                            client, origin + "/api/show", body={"model": target.model}
                        )
                        reported = {
                            "runtime": {"engine": "ollama", "version": version.get("version")},
                            "model": {
                                "quantization": model.get("details", {}).get("quantization_level")
                            },
                        }
                        if isinstance(model.get("template"), str):
                            reported["model"]["chat_template"] = (
                                "sha256:" + hashlib.sha256(model["template"].encode()).hexdigest()
                            )
                        result["discovery"].append(
                            {"status": "available", "source": "ollama /api/version and /api/show"}
                        )
                    else:
                        version = await _read(client, origin + "/version")
                        reported = {
                            "runtime": {"engine": "vllm", "version": version.get("version")}
                        }
                        result["discovery"].append(
                            {"status": "available", "source": "vllm /version"}
                        )
                    for category, values in reported.items():
                        for key, value in values.items():
                            if value is not None:
                                result["fields"].setdefault(f"{category}.{key}", {})["reported"] = (
                                    _fact(value, "runtime-reported", deployment.scope, now())
                                )
                if deployment.collector_url:
                    # Separate configured collector never inherits inference credentials.
                    client.headers.clear()
                    export = await _read(client, deployment.collector_url)
                    safe = safe_metadata(export)
                    scope = export.get("scope", "configured collector; replica coverage unverified")
                    for category, values in safe.items():
                        for key, value in values.items():
                            result["fields"].setdefault(f"{category}.{key}", {})["reported"] = (
                                _fact(
                                    value,
                                    "configured-collector",
                                    scope,
                                    export.get("collected_at", now()),
                                )
                            )
                    result["discovery"].append(
                        {"status": "available", "source": "configured-collector"}
                    )
        except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
            result["discovery"].append({"status": "unavailable", "reason": type(exc).__name__})
    for row in result["fields"].values():
        if (
            "configured" in row
            and "reported" in row
            and row["configured"]["value"] != row["reported"]["value"]
        ):
            row["status"] = "conflicting"
    known = set(result["fields"])
    result["unknown_fields"] = sorted(
        f"{c}.{f}" for c, fields in CATEGORIES.items() for f in fields if f"{c}.{f}" not in known
    )
    return result


def observed_signature(snapshot):
    return {
        key: {"value": row["reported"]["value"], "scope": row["reported"]["scope"]}
        for key, row in snapshot.get("fields", {}).items()
        if "reported" in row
    }


def deployment_diff(current, baseline):
    rows = []
    current, baseline = current or {}, baseline or {}
    fields, previous = current.get("fields", {}), baseline.get("fields", {})
    for key in sorted(set(fields) | set(previous)):
        c, b = fields.get(key), previous.get(key)

        def values(row):
            return {
                source: {k: value.get(k) for k in ("value", "provenance", "scope", "status")}
                for source, value in (row or {}).items()
                if isinstance(value, dict)
            }

        change = (
            "added"
            if b is None
            else "removed"
            if c is None
            else "changed"
            if values(c) != values(b)
            else "no_observed_change"
        )
        rows.append({"field": key, "change": change, "baseline": b, "current": c})
    changed = [r["field"] for r in rows if r["change"] != "no_observed_change"]
    intended = current.get("intended_change")
    return {
        "fields": rows,
        "unknown_baseline": not bool(baseline),
        "unknown_fields": sorted(
            set(current.get("unknown_fields", [])) | set(baseline.get("unknown_fields", []))
        ),
        "continuity": {
            "baseline": baseline.get("continuity", "unverified"),
            "current": current.get("continuity", "unverified"),
        },
        "intended_change": intended,
        "confounded": bool(intended and any(f != intended for f in changed)),
        "interpretation": "Changes are investigation context, not evidence of causality. Missing/stale/unobserved fields cannot establish unchanged configuration.",
    }


def fixture_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def safe_config(config):
    """Persist secret reference names, never their values or arbitrary launch inputs."""
    result = dict(config)
    result["request_options"] = sanitize(config.get("request_options", {}))
    result["targets"] = []
    for original in config.get("targets", []):
        target = dict(original)
        target["identity"] = sanitize(target.get("identity", {}))
        if target.get("restart_command"):
            target["restart_command"] = safe_launch(target["restart_command"])
        deployment = dict(target.get("deployment", {}))
        for category in (*CATEGORIES, "extension"):
            deployment[category] = safe_metadata(deployment).get(category, {})
        if deployment.get("launch_command"):
            deployment["launch_command"] = safe_launch(deployment["launch_command"])
        for key in ("scope", "collected_at", "intended_change", "metadata_file", "collector_url"):
            deployment[key] = sanitize(deployment.get(key))
        target["deployment"] = deployment
        result["targets"].append(target)
    return result
