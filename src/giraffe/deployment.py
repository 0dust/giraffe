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

from .caps import (
    MAX_DEPLOYMENT_BODY_BYTES, MAX_DEPLOYMENT_SCOPE_CHARS, MAX_LAUNCH_ARGUMENTS,
    MAX_REPORT_SANITIZE_DEPTH, MAX_SANITIZE_DEPTH, MAX_SANITIZE_ITEMS,
    MAX_SANITIZE_KEYS, MAX_SANITIZE_KEY_CHARS, MAX_SANITIZE_STRING_CHARS,
)

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
    if depth > (MAX_SANITIZE_DEPTH if bounded else MAX_REPORT_SANITIZE_DEPTH):
        return "[depth limit]"
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:MAX_SANITIZE_KEYS] if bounded else value.items():
            key = str(key)[:MAX_SANITIZE_KEY_CHARS]
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
            for v in (value[:MAX_SANITIZE_ITEMS] if bounded else value)
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

        return _SECRET_TEXT.sub(REDACTED, _URL.sub(clean_url, value))[:MAX_SANITIZE_STRING_CHARS]
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
    if len(args) > MAX_LAUNCH_ARGUMENTS:
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
    if path.stat().st_size > MAX_DEPLOYMENT_BODY_BYTES:
        raise ValueError("deployment metadata file exceeds 64 KiB")
    try:
        data = json.loads(path.read_text())
        result = safe_metadata(data)
        for key in ("scope", "collected_at"):
            if key in data and data[key] is not None:
                if not isinstance(data[key], str):
                    raise ValueError("metadata scope and collection time must be strings")
                result[key] = sanitize(data[key])[:MAX_DEPLOYMENT_SCOPE_CHARS]
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
            if len(data) + len(chunk) > MAX_DEPLOYMENT_BODY_BYTES:
                raise ValueError("discovery response exceeds 64 KiB")
            data.extend(chunk)
    return json.loads(data)


def _sglang_metadata(info):
    """Select known fields; /get_server_info also contains credentials and paths."""
    if not isinstance(info, dict):
        raise ValueError("SGLang server info must be an object")
    args = info.get("server_args", info)
    if not isinstance(args, dict):
        raise ValueError("SGLang server args must be an object")
    reported = {
        "runtime": {"engine": "sglang", "version": info.get("version")},
        "model": {
            key: args[source] for source, key in (
                ("revision", "revision"), ("quantization", "quantization"),
                ("tokenizer_path", "tokenizer"),
            ) if isinstance(args.get(source), str)
        },
        "serving": {
            "resolved_settings": {
                key: args[key] for key in (
                    "context_length", "max_running_requests", "max_total_tokens",
                    "chunked_prefill_size", "mem_fraction_static", "schedule_policy",
                    "disable_radix_cache", "disable_overlap_schedule", "mlx_enable_sampling",
                    "grammar_backend", "tool_call_parser",
                ) if key in args and isinstance(args[key], (str, int, float, bool))
            },
        },
        "backend": {},
        "hardware": {},
    }
    for source, category, key in (
        ("context_length", "serving", "context_limit"),
        ("schedule_policy", "serving", "scheduler"),
        ("attention_backend", "backend", "attention"),
        ("tp_size", "hardware", "tensor_parallel"),
        ("pp_size", "hardware", "pipeline_parallel"),
    ):
        if isinstance(args.get(source), (str, int, float)) and not isinstance(args[source], bool):
            reported[category][key] = args[source]
    if isinstance(args.get("disable_radix_cache"), bool):
        reported["serving"]["prefix_caching"] = not args["disable_radix_cache"]
    if isinstance(args.get("enable_metrics"), bool):
        reported["serving"]["instrumentation"] = "enabled" if args["enable_metrics"] else "disabled"
    return reported


ENGINE_DISCOVERY_ROUTES = {
    "llamacpp": "/props", "tensorrt-llm": "/version",
    "lmdeploy": "/v1/models", "tgi": "/info",
}


def _engine_metadata(engine, info, model):
    """Select documented fields; model paths and arbitrary server settings stay private."""
    if not isinstance(info, dict):
        raise ValueError("runtime discovery must be an object")
    reported = {"runtime": {"engine": engine}, "model": {}, "serving": {}}
    if engine == "llamacpp":
        settings = info.get("default_generation_settings")
        if not isinstance(settings, dict) or type(info.get("total_slots")) is not int:
            raise ValueError("invalid llama.cpp properties")
        if isinstance(info.get("build_info"), str):
            reported["runtime"]["version"] = info["build_info"]
        for key, value in (("context_limit", settings.get("n_ctx")),
                           ("max_num_seqs", info["total_slots"])):
            if type(value) is int and value > 0:
                reported["serving"][key] = value
        if isinstance(info.get("chat_template"), str):
            reported["model"]["chat_template"] = (
                "sha256:" + hashlib.sha256(info["chat_template"].encode()).hexdigest())
    elif engine == "tensorrt-llm":
        if not isinstance(info.get("version"), str) or not info["version"]:
            raise ValueError("invalid TensorRT-LLM version")
        reported["runtime"]["version"] = info["version"]
    elif engine == "lmdeploy":
        models = info.get("data")
        if not isinstance(models, list) or not any(
            isinstance(item, dict) and item.get("id") == model
            and item.get("owned_by") == "lmdeploy" for item in models
        ):
            raise ValueError("requested model not reported by LMDeploy")
        reported["model"]["served"] = model
        # /v1/models does not publish a runtime version or resolved settings.
    elif engine == "tgi":
        if info.get("router") != "text-generation-router" or not isinstance(info.get("model_id"), str):
            raise ValueError("invalid TGI server info")
        for source, category, key in (
            ("version", "runtime", "version"), ("sha", "runtime", "commit"),
            ("model_id", "model", "served"), ("model_sha", "model", "revision"),
        ):
            if isinstance(info.get(source), str):
                reported[category][key] = info[source]
        if type(info.get("max_total_tokens")) is int and info["max_total_tokens"] > 0:
            reported["serving"]["context_limit"] = info["max_total_tokens"]
        reported["serving"]["resolved_settings"] = {
            key: info[key] for key in ("max_input_tokens", "max_total_tokens", "max_concurrent_requests")
            if type(info.get(key)) is int and info[key] > 0
        }
    return reported


def unknown_residency(target, reason):
    return {"state": "unknown", "source": "ollama /api/ps" if target.deployment.discovery == "ollama" else None,
            "target": target.name, "model": sanitize(target.model),
            "scope": "configured endpoint; other replicas unverified",
            "collected_at": None, "reason": reason}


async def model_residency(target, config, *, deadline=None, stop=None):
    """Observe residency without issuing an inference request."""
    import time
    from .client import _headers

    result = unknown_residency(target, "Residency discovery is not enabled for this runtime.")
    if target.deployment.discovery != "ollama":
        return result
    remaining = config.metrics_timeout_seconds
    if deadline is not None:
        remaining = min(remaining, deadline - time.monotonic())
    if remaining <= 0 or stop is not None and stop.is_set():
        result["reason"] = "Run stopped before residency could be observed."
        return result
    parsed = urlsplit(target.url)
    url = urlunsplit((parsed.scheme, parsed.netloc, "/api/ps", "", ""))
    try:
        async with asyncio.timeout(remaining):
            client = await asyncio.to_thread(_discovery_client, config, remaining)
            async with client:
                client.headers.update(_headers(target))
                payload = await _read(client, url)
        entries = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not isinstance(entry.get("model", entry.get("name")), str)
            for entry in entries
        ):
            raise ValueError("Invalid residency inventory")
        def canonical(name):
            return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"
        resident = any(canonical(entry.get("model", entry.get("name"))) == canonical(target.model)
                       for entry in entries)
        result.update(state="resident" if resident else "not_resident", collected_at=now(),
                      reason="Requested model listed by runtime." if resident else "Requested model absent from runtime inventory.")
    except (httpx.HTTPError, ValueError, OSError, TimeoutError):
        result["reason"] = "Runtime residency inventory unavailable or invalid."
    return result


async def preparation_residency(config, *, stop=None):
    observations = await asyncio.gather(*(model_residency(target, config, stop=stop) for target in config.targets))
    return dict(zip((target.name for target in config.targets), observations, strict=True))


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
    client = await asyncio.to_thread(_discovery_client, config, remaining)
    async with client:
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
                        for route in ("/api/tags", "/api/ps"):
                            try:
                                inventory = await _read(client, origin + route)
                                for entry in inventory.get("models", []):
                                    if entry.get("model", entry.get("name")) != target.model:
                                        continue
                                    if isinstance(entry.get("digest"), str):
                                        reported["model"]["digest"] = entry["digest"]
                                    context = entry.get("context_length")
                                    if (
                                        route == "/api/ps"
                                        and isinstance(context, int)
                                        and not isinstance(context, bool)
                                        and context > 0
                                    ):
                                        reported.setdefault("serving", {})["context_limit"] = (
                                            context
                                        )
                                result["discovery"].append(
                                    {"status": "available", "source": "ollama " + route}
                                )
                            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                                result["discovery"].append(
                                    {"status": "unavailable", "source": "ollama " + route}
                                )
                        result["discovery"].append(
                            {"status": "available", "source": "ollama /api/version and /api/show"}
                        )
                    elif deployment.discovery == "sglang":
                        info = await _read(client, origin + "/get_server_info")
                        reported = _sglang_metadata(info)
                        result["discovery"].append(
                            {"status": "available", "source": "sglang /get_server_info"}
                        )
                    elif deployment.discovery in ENGINE_DISCOVERY_ROUTES:
                        route = ENGINE_DISCOVERY_ROUTES[deployment.discovery]
                        info = await _read(client, origin + route)
                        reported = _engine_metadata(deployment.discovery, info, target.model)
                        result["discovery"].append(
                            {"status": "available", "source": deployment.discovery + " " + route}
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


def _discovery_client(config, timeout):
    import ssl

    # Discovery can run beside inference on other targets. TLS setup is blocking.
    verify = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else True
    return httpx.AsyncClient(
        timeout=timeout, verify=verify, proxy=config.proxy, follow_redirects=False,
    )


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
    options = config.get("request_options", {})
    if config.get("request_source") == "request_pack":
        from .models import validate_pack_options
        validate_pack_options(options)
        result["request_options"] = options
    else:
        result["request_options"] = sanitize(options)
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
