"""Optional bounded Prometheus scrapes; shared counters never imply causality."""

from __future__ import annotations

import asyncio
import math
import re
import ssl
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from .caps import (
    MAX_DEPLOYMENT_HISTORY, MAX_METRICS_BODY_BYTES, MAX_METRICS_LABELS,
    MAX_METRICS_LABEL_CHARS,
)
from giraffe.deployment import sanitize

# Exact names and units, not substring guesses. Profiles are explicit runtime contracts.
# vLLM V1: vllm/v1/metrics/loggers.py; legacy: engine/metrics.py.
V1 = {
    "vllm:kv_cache_usage_perc": ("cache_occupancy", "gauge", "fraction"),
    "vllm:num_requests_running": ("running", "gauge", "requests"),
    "vllm:num_requests_waiting": ("waiting", "gauge", "requests"),
    "vllm:num_preemptions_total": ("preemptions", "counter", "events"),
    "vllm:prefix_cache_hits_total": ("prefix_hits", "counter", "tokens"),
    "vllm:prefix_cache_queries_total": ("prefix_queries", "counter", "tokens"),
    "vllm:time_to_first_token_seconds_sum": ("server_ttft_sum", "counter", "seconds"),
    "vllm:time_to_first_token_seconds_count": ("server_ttft_count", "counter", "requests"),
    "vllm:time_to_first_token_seconds_bucket": ("server_ttft_bucket", "counter", "requests"),
    "vllm:prompt_tokens_total": ("prompt_tokens", "counter", "tokens"),
    "vllm:generation_tokens_total": ("generated_tokens", "counter", "tokens"),
}
LEGACY = V1 | {
    "vllm:gpu_cache_usage_perc": ("cache_occupancy", "gauge", "fraction"),
    "vllm:cpu_cache_usage_perc": ("cpu_cache_occupancy", "gauge", "fraction"),
}
SGLANG = {
    "sglang:token_usage": ("cache_occupancy", "gauge", "fraction"),
    "sglang:num_running_reqs": ("running", "gauge", "requests"),
    "sglang:num_queue_reqs": ("waiting", "gauge", "requests"),
    "sglang:num_retracted_requests_total": ("retractions", "counter", "requests"),
    "sglang:num_retracted_reqs": ("retractions_current", "gauge", "requests"),
    "sglang:cache_hit_rate": ("prefix_hit_rate", "gauge", "fraction"),
    "sglang:cached_tokens_total": ("cached_tokens", "counter", "tokens"),
    "sglang:prompt_tokens_total": ("prompt_tokens", "counter", "tokens"),
    "sglang:time_to_first_token_seconds_sum": ("server_ttft_sum", "counter", "seconds"),
    "sglang:time_to_first_token_seconds_count": ("server_ttft_count", "counter", "requests"),
    "sglang:time_to_first_token_seconds_bucket": ("server_ttft_bucket", "counter", "requests"),
}
# Exact metric contracts from the source used for the live Apple Silicon run.
SGLANG_REFERENCE = "SGLang 44bc47886 srt/observability/metrics_collector.py"
LLAMACPP = {
    "llamacpp:requests_processing": ("running", "gauge", "requests"),
    "llamacpp:requests_deferred": ("waiting", "gauge", "requests"),
    "llamacpp:prompt_tokens_total": ("prompt_tokens", "counter", "tokens"),
    "llamacpp:tokens_predicted_total": ("generated_tokens", "counter", "tokens"),
}
LMDEPLOY = {
    "lmdeploy:num_requests_running": ("running", "gauge", "requests"),
    "lmdeploy:num_requests_waiting": ("waiting", "gauge", "requests"),
    "lmdeploy:gpu_cache_usage_perc": ("cache_occupancy", "gauge", "fraction"),
    "lmdeploy:prefix_cache_hit_rate": ("prefix_hit_rate", "gauge", "fraction"),
    "lmdeploy:cached_tokens_total": ("cached_tokens", "counter", "tokens"),
    "lmdeploy:prompt_tokens_total": ("prompt_tokens", "counter", "tokens"),
    "lmdeploy:generation_tokens_total": ("generated_tokens", "counter", "tokens"),
}
TGI = {
    "tgi_request_queue_duration_sum": ("server_queue_sum", "counter", "seconds"),
    "tgi_request_queue_duration_count": ("server_queue_count", "counter", "requests"),
    "tgi_request_inference_duration_sum": ("server_inference_sum", "counter", "seconds"),
    "tgi_request_inference_duration_count": ("server_inference_count", "counter", "requests"),
    "tgi_request_queue_duration_bucket": ("server_queue_bucket", "counter", "requests"),
    "tgi_request_inference_duration_bucket": ("server_inference_bucket", "counter", "requests"),
}
ENGINE_METRICS = {"llamacpp": LLAMACPP, "lmdeploy": LMDEPLOY, "tgi": TGI}
ENGINE_METRIC_REFERENCES = {
    "llamacpp": "llama.cpp tools/server/README.md /metrics contract (b11696)",
    "lmdeploy": "LMDeploy lmdeploy/metrics/loggers.py; runtime/version qualification required",
    "tgi": "TGI router/src/server.rs duration histograms in seconds; runtime/version qualification required",
    "tensorrt-llm": "Native /metrics iteration JSON is not a Prometheus contract; use a separate Prometheus exporter for GPU observations.",
}
GPU = {
    "DCGM_FI_DEV_FB_USED": ("memory", "gauge", "MiB"),
    "DCGM_FI_DEV_GPU_UTIL": ("utilization", "gauge", "percent"),
    "DCGM_FI_DEV_GPU_TEMP": ("temperature", "gauge", "celsius"),
    "DCGM_FI_DEV_CLOCKS_EVENT_REASONS": ("throttling", "gauge", "bitmask"),
    "DCGM_FI_DEV_CLOCK_THROTTLE_REASONS": ("throttling", "gauge", "bitmask"),
    "DCGM_FI_DEV_XID_ERRORS": ("device_errors", "gauge", "code"),
}
HOST = {
    "node_cpu_seconds_total": ("host_cpu_seconds", "counter", "seconds"),
    "node_memory_MemTotal_bytes": ("host_ram_total", "gauge", "bytes"),
    "node_memory_MemAvailable_bytes": ("host_ram_available", "gauge", "bytes"),
    "node_uname_info": ("host_identity", "gauge", "info"),
}
LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*?\})?\s+([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)(?:\s+(\d+(?:\.\d+)?))?\s*$"
)
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
SERVING_FAMILIES = {"cache_occupancy", "running", "waiting", "preemptions"}
GPU_FAMILIES = {entry[0] for entry in GPU.values()}
# NVIDIA DCGM adverse bits: HW slowdown, SW/HW thermal, power brake.
# Idle/application-clock/sync-boost/display-clock reasons alone are not failures.
GPU_THROTTLE_REASONS = {8: "hardware slowdown",
                        32: "software thermal", 64: "hardware thermal", 128: "hardware power brake"}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def _gpu_device(labels):
    # UUID is globally unique; otherwise host/instance disambiguate identical GPU indices.
    uuid = labels.get("UUID") or labels.get("uuid")
    return ("uuid", uuid) if uuid else ("index", labels.get("Hostname", ""), labels.get("instance", ""), labels.get("gpu"))


def assess_gpu(snapshots, max_age_seconds, *, gpu_count=None, gpu_max_temperature_c=None):
    """Assess published GPU devices; an advancing scrape alone proves no freshness.

    Value changes establish freshness for that device, not for other GPUs. Unseen
    devices cannot be enumerated from this endpoint; declared counts bound coverage.
    Every device must be fresh in the first and last GPU scrape; final or
    consecutive absences from complete GPU scrapes establish disappearance.
    """
    devices, series, alerts = {}, defaultdict(list), []
    temperatures = defaultdict(dict)
    # XID history per (device, metric): exporters may attach err_code/err_msg labels.
    xids = defaultdict(lambda: defaultdict(set))
    presence = defaultdict(dict)
    successful, truncated = [], set()
    warnings, informational = [], []
    completed = 0
    for index, snap in enumerate(snapshots):
        if snap.get("status") not in {"available", "stale_samples", "absent_families"}:
            continue
        samples = [s for s in snap.get("samples", []) if s["family"] in GPU_FAMILIES]
        if snap.get("series_limit_reached"):
            truncated.add(index)
        if not samples:
            continue
        completed += 1
        successful.append(index)
        for sample in samples:
            labels, family, value = sample.get("labels", {}), sample["family"], sample["value"]
            key = _gpu_device(labels)
            device = devices.setdefault(key, {"families": set(), "timestamped": set(), "changing": False,
                                               "gpu": labels.get("gpu"), "hostname": labels.get("Hostname", ""),
                                               "unknown_identity": key[0] != "uuid" and key[-1] is None})
            presence[key].setdefault(index, False)
            timestamp = sample.get("sample_timestamp_ms")
            age = sample.get("age_seconds")
            if timestamp is not None and age is None:
                collected = snap.get("collected_at")
                observed = datetime.fromisoformat(collected).timestamp() if collected else time.time()
                age = observed - timestamp / 1000
            stale = (age > max_age_seconds or age < -5) if timestamp is not None else sample.get("stale", False)
            if family == "device_errors":
                fresh = xids[(key, sample["name"])][index]
            # DCGM's int64 blank/error sentinels are not measurements.
            if not math.isfinite(value) or value < 0 or value >= 2**63 - 1024:
                continue
            if family == "device_errors":
                if not stale:
                    fresh.add(value)
            if stale:
                continue
            presence[key][index] = True
            device["families"].add(family)
            if family == "temperature":
                temperatures[key][index] = max(temperatures[key].get(index, value), value)
            if timestamp is not None:
                device["timestamped"].add(family)
            identity = (key, sample["name"], tuple(sorted(labels.items())))
            series[identity].append((index, value))
            if family == "throttling" and int(value) & ~sum(GPU_THROTTLE_REASONS):
                informational.append({"gpu": device["gpu"], "value": value, "snapshot": index})
    for (key, name, _), values in series.items():
        device = devices[key]
        if any(a[0] != b[0] and a[1] != b[1] for a, b in zip(values, values[1:])):
            device["changing"] = True
        if GPU[name][0] == "throttling":
            for bit, reason in GPU_THROTTLE_REASONS.items():
                observed = [(i, v) for i, v in values if int(v) & bit]
                if len({i for i, _ in observed}) >= 2:
                    alerts.append({"gpu": device["gpu"], "hostname": device["hostname"], "family": "throttling",
                                   "value": observed[-1][1], "reasons": [reason],
                                   "snapshots": sorted({i for i, _ in observed})})
    for (key, name), history in xids.items():
        device, previous = devices[key], None
        first = min(presence[key])
        uncertain = first in truncated or first in history and not history[first]
        for index in sorted(history):
            fresh = history[index]
            if previous is None and (index == first or uncertain):
                warnings.extend({"gpu": device["gpu"], "hostname": device["hostname"], "historical_xid": value,
                                 "history_unknown": uncertain,
                                 "reason": "Initial XID history unknown (stale, blank or truncated)." if uncertain else
                                           "Last XID predates observation; not an error count."}
                                for value in sorted(fresh) if value > 0)
            else:
                # A series first seen after the device's first scrape is new, whatever its labels.
                alerts.extend({"gpu": device["gpu"], "hostname": device["hostname"], "family": "device_errors",
                               "value": value, "snapshot": index, "reasons": [
                                   "XID changed during observation" if previous is not None else "XID appeared during observation"]}
                              for value in sorted(fresh) if value > 0 and value not in (previous or set()))
            if fresh:
                previous = fresh
    if gpu_max_temperature_c is not None:
        for key, values in temperatures.items():
            above = sorted(index for index, value in values.items() if value > gpu_max_temperature_c)
            if len(above) >= 2:
                alerts.append({"gpu": devices[key]["gpu"], "hostname": devices[key]["hostname"], "family": "temperature",
                               "value": max(values.values()), "snapshots": above,
                               "reasons": ["temperature exceeded configured limit in repeated snapshots"]})
    rows, disappeared = [], False
    bounds = {successful[0], successful[-1]} if successful else set()
    for key, device in sorted(devices.items(), key=lambda item: str(item[0])):
        seen = presence[key]
        # A truncated scrape cannot prove a device was absent.
        absent = [i for i in successful if i > min(seen) and i not in seen and i not in truncated]
        gone = next((i for pos, i in enumerate(successful) if i in absent and
                     (pos == len(successful) - 1 or pos and successful[pos - 1] in absent)), None)
        if absent and gone is None:
            warnings.append({"gpu": device["gpu"], "hostname": device["hostname"], "snapshots": absent,
                             "reason": "GPU device transiently absent from a GPU scrape; returned on the next scrape."})
        if gone is not None:
            disappeared = True
            alerts.append({"gpu": device["gpu"], "hostname": device["hostname"], "family": "presence", "value": None,
                           "reasons": ["GPU device disappeared during the run"], "snapshot": gone})
        fresh = device["families"] if device["changing"] else device["timestamped"]
        missing = sorted(GPU_FAMILIES - device["families"])
        unknown = sorted(device["families"] - fresh)
        rows.append({"gpu": device["gpu"], "hostname": device["hostname"],
                     **({"uuid": key[1]} if key[0] == "uuid" else {"instance": key[2]} if key[2] else {}),
                     "missing_families": missing, "unknown_freshness_families": unknown,
                     "identity_verified": not device["unknown_identity"],
                     "fresh_in_first_and_last_snapshot": all(seen.get(i) for i in bounds),
                     "disappeared_at_snapshot": gone,
                     "freshness": "values changed across scrapes" if device["changing"] else
                     "source timestamps" if not unknown else "unknown"})
    unavailable = sorted({f for row in rows for f in row["missing_families"]}) if rows else sorted(GPU_FAMILIES)
    metrics = {"attempted": len(snapshots), "completed": completed, "snapshots": snapshots,
               "unavailable_families": unavailable, "devices": rows, "alerts": alerts,
               "warnings": warnings, "informational_clock_events": informational,
               "coverage": "Published GPU labels only; devices absent from all scrapes cannot be discovered.",
               "metrics_max_age_seconds": max_age_seconds}
    observed_count = sum(row["identity_verified"] and row["fresh_in_first_and_last_snapshot"]
                         and row["disappeared_at_snapshot"] is None for row in rows)
    metrics.update({"declared_gpu_count": gpu_count, "observed_gpu_count": observed_count,
                    "max_temperature_c": max((v for values in temperatures.values() for v in values.values()), default=None),
                    "gpu_max_temperature_c": gpu_max_temperature_c})
    qualifier = "this is not device health certification."
    if disappeared:
        return metrics, "fail", "GPU device disappeared during the run (final or consecutive successful GPU scrapes); " + qualifier
    if alerts:
        return metrics, "fail", "GPU telemetry reports a new XID, repeated adverse slowdown or configured temperature violation; " + qualifier
    if any(warning.get("history_unknown") for warning in warnings):
        return metrics, "inconclusive", "Initial XID history is unknown; cannot distinguish a historical XID from a new error; " + qualifier
    if isinstance(gpu_count, int) and not isinstance(gpu_count, bool) and observed_count < gpu_count:
        return metrics, "inconclusive", f"GPU telemetry covers {observed_count} devices, fewer than the declared {gpu_count}; " + qualifier
    if any(not row["fresh_in_first_and_last_snapshot"] for row in rows):
        return metrics, "inconclusive", "GPU device samples are stale or missing in the first or last successful scrape; " + qualifier
    if not rows or any(row["missing_families"] or row["unknown_freshness_families"] or
                       not row["identity_verified"] for row in rows):
        return metrics, "inconclusive", "GPU observations lack per-device coverage or freshness evidence; " + qualifier
    return metrics, "pass", "Fresh GPU telemetry covers every observed device without new XID, repeated adverse slowdown or configured temperature violations; " + qualifier


def _metrics_headers(target):
    # Metrics has independent secret references, even on the inference origin.
    import os

    headers = {}
    if target.metrics_api_key_env:
        value = os.environ.get(target.metrics_api_key_env)
        if not value:
            raise ValueError("missing metrics credential reference")
        headers["Authorization"] = "Bearer " + value
    for key, ref in target.metrics_headers_env.items():
        value = os.environ.get(ref)
        if not value:
            raise ValueError("missing metrics header reference")
        headers[key] = value
    return headers


def parse(body, target, config, *, host=False):
    profile = (
        V1
        if target.metrics_profile == "vllm-v1"
        else LEGACY
        if target.metrics_profile == "vllm-legacy"
        else SGLANG
        if target.metrics_profile == "sglang"
        else ENGINE_METRICS.get(target.metrics_profile, {})
    )
    mapping = HOST if host else profile | GPU
    samples, recognized, truncated = [], 0, False
    for line in body.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        match = LINE.fullmatch(line)
        if not match:
            continue
        recognized += 1
        name, raw_labels, raw_value, raw_time = match.groups()
        if name not in mapping or not math.isfinite(float(raw_value)):
            continue
        if len(samples) >= config.metrics_max_series:
            truncated = True
            continue
        labels = {}
        for key, value in LABEL.findall(raw_labels or ""):
            if len(labels) >= MAX_METRICS_LABELS:
                truncated = True
                break
            labels[key] = value[:MAX_METRICS_LABEL_CHARS]
        family, kind, unit = mapping[name]
        timestamp = float(raw_time) if raw_time else None
        age = time.time() - timestamp / 1000 if timestamp is not None else None
        max_age = (config.effective_check("gpu")["metrics_max_age_seconds"]
                   if family in {entry[0] for entry in GPU.values()} and "gpu" in config.checks
                   else config.metrics_max_age_seconds)
        stale = age is not None and (age > max_age or age < -5)
        samples.append(
            {
                "name": name,
                "family": family,
                "kind": kind,
                "unit": unit,
                "value": float(raw_value),
                "labels": sanitize(labels),
                "sample_timestamp_ms": timestamp,
                "age_seconds": age,
                "stale": stale,
                "freshness": "source timestamp"
                if timestamp
                else "scrape only; source freshness unknown",
                "provenance": "host node_exporter metrics" if host else "runtime-reported metrics",
            }
        )
    families = {s["family"] for s in samples if not s["stale"]}
    state = (
        "stale_samples"
        if samples and all(s["stale"] for s in samples)
        else "available"
        if samples
        else "absent_families"
        if recognized
        else "unsupported_format"
    )
    if not body.strip() or body.strip() in {"[]", "{}"}:
        state = "empty_response"
    required = {"host_cpu_seconds", "host_ram_total", "host_ram_available"} if host else (
        (SERVING_FAMILIES - {"preemptions"}) | {"retractions"}
        if target.metrics_profile == "sglang" else SERVING_FAMILIES
    )
    return {
        "status": state,
        "samples": samples,
        "missing_families": sorted(required - families),
        "series_limit_reached": truncated,
        "mapping": "node_exporter" if host else target.metrics_profile,
        "mapping_reference": "https://prometheus.io/docs/guides/node-exporter/" if host else "vLLM 0.30.0 v1/metrics/loggers.py"
        if target.metrics_profile == "vllm-v1"
        else SGLANG_REFERENCE
        if target.metrics_profile == "sglang"
        else "explicit legacy metric-name contract; runtime/version qualification required"
        if target.metrics_profile == "vllm-legacy"
        else ENGINE_METRIC_REFERENCES.get(target.metrics_profile, "No serving metric-name contract selected."),
        "runtime_version": target.metrics_runtime_version,
        "version_verification": "user-supplied profile/version; verify with recorded deployment discovery",
        "instrumentation": target.instrumentation,
        "instrumentation_provenance": "user-supplied; unverified",
        "reason": {
            "empty_response": "Successful response contains no usable statistics; system health and instrumentation state unknown.",
            "absent_families": "Relevant metric families absent; missing is unknown, not zero.",
            "unsupported_format": "Unrecognized metrics format or runtime profile.",
            "stale_samples": "Only stale source samples were returned.",
        }.get(state, "Observed metrics; shared traffic and between-scrape peaks remain unknown."),
    }


def _metrics_client(config, target, timeout):
    # Loading CA certificates can block even for an HTTP statistics endpoint.
    verify = ssl.create_default_context(cafile=config.ca_bundle) if config.ca_bundle else True
    return httpx.AsyncClient(
        verify=verify, proxy=config.proxy, headers=_metrics_headers(target),
        timeout=timeout, follow_redirects=False,
    )


async def _scrape_runtime(run, target, phase, *, force=False):
    result = {
        "phase": phase,
        "collected_at": stamp(),
        "elapsed_seconds": time.monotonic() - run.start,
        "runtime_version": target.metrics_runtime_version,
        "profile": target.metrics_profile,
        "samples": [],
        "scope": "published label series; may include unrelated shared traffic",
    }
    if not run.config.metrics and not force:
        return result | {"status": "not_enabled", "reason": "Collection not enabled."}
    if not target.metrics_url:
        return result | {"status": "no_endpoint", "reason": "No statistics endpoint configured."}
    remaining = min(run.config.metrics_timeout_seconds, run.deadline - time.monotonic())
    if run.stop.is_set() or remaining <= 0:
        return result | {"status": "run_stopped", "reason": "Run stopped or deadline reached."}
    try:
        async with asyncio.timeout(remaining):
            client = await asyncio.to_thread(_metrics_client, run.config, target, remaining)
            async with client:
                async with client.stream("GET", target.metrics_url) as response:
                    result["http_status"] = response.status_code
                    if response.status_code in {401, 403}:
                        return result | {
                            "status": "authentication_denied",
                            "reason": "Statistics authentication denied.",
                        }
                    if response.status_code == 404:
                        return result | {
                            "status": "endpoint_absent",
                            "reason": "Configured statistics endpoint returned 404.",
                        }
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_METRICS_BODY_BYTES:
                            return result | {
                                "status": "response_limit",
                                "reason": "Statistics response exceeds 1 MiB.",
                            }
                        body.extend(chunk)
        result.update(parse(body.decode("utf-8"), target, run.config))
    except (httpx.HTTPError, OSError, ValueError, UnicodeError, TimeoutError) as exc:
        result.update(
            status="connection_failure",
            reason=f"Statistics unavailable ({type(exc).__name__}); instrumentation state unknown.",
        )
    instrument = (
        (run.observations[target.name].get("deployment") or {})
        .get("fields", {})
        .get("serving.instrumentation", {})
        .get("reported")
    )
    if instrument and instrument.get("value") in {"enabled", "disabled", "unsupported"}:
        result["instrumentation_evidence"] = instrument
        result["instrumentation"] = instrument["value"]
        result["instrumentation_provenance"] = instrument["provenance"]
    result["scrape_duration_seconds"] = time.monotonic() - run.start - result["elapsed_seconds"]
    return result


async def sample_during(run, target):
    data = run.observations[target.name]["serving_telemetry"]
    last = None
    while not run.stop.is_set() and time.monotonic() < run.deadline:
        if len(data["snapshots"]) >= run.config.metrics_max_samples - 1:
            data["sample_limit_reached"] = True
            return
        phase = (
            ",".join(
                sorted(
                    s for (name, s), count in run.active.items() if name == target.name and count
                )
            )
            or "between phases"
        )
        snap = await scrape(run, target, phase)
        if last is not None:
            data["collection_gaps_seconds"].append(snap["elapsed_seconds"] - last)
        last = snap["elapsed_seconds"]
        data["snapshots"].append(snap)
        if (
            target.deployment.collector_url
            and len(run.observations[target.name]["deployment_history"]) < MAX_DEPLOYMENT_HISTORY
        ):
            from giraffe.deployment import snapshot

            run.observations[target.name]["deployment_history"].append(
                await snapshot(target, run.config, run.deadline, run.stop)
            )
        try:
            await asyncio.wait_for(run.stop.wait(), run.config.metrics_interval_seconds)
        except TimeoutError:
            pass


def summarize(data, threshold):
    groups = defaultdict(list)
    phases = defaultdict(set)
    for snap in data["snapshots"]:
        for sample in snap.get("samples", []):
            if not sample["stale"]:
                key = (sample["name"], str(sorted(sample["labels"].items())))
                groups[key].append((snap, sample))
                phases[snap["phase"]].add(key)
    rows = []
    pressure = False
    for key, pairs in groups.items():
        values = [s["value"] for _, s in pairs]
        first = pairs[0][1]
        row = {
            "name": key[0],
            "labels": first["labels"],
            "family": first["family"],
            "unit": first["unit"],
            "kind": first["kind"],
            "samples": len(values),
            "first_collected_at": pairs[0][0]["collected_at"],
            "last_collected_at": pairs[-1][0]["collected_at"],
        }
        if first["kind"] == "gauge":
            row.update(min=min(values), max=max(values), peak=max(values))
            pressure |= first["family"] == "cache_occupancy" and max(values) >= threshold
        else:
            resets = sum(b < a for a, b in zip(values, values[1:]))
            row.update(
                resets=resets,
                delta=None if resets or len(values) < 2 else values[-1] - values[0],
                interpretation="reset/restart; delta unknown"
                if resets
                else "shared endpoint counter window; attribution unknown",
            )
        rows.append(row)
    identities = {
        str(sorted(sample["labels"].items()))
        for snap in data["snapshots"]
        for sample in snap.get("samples", [])
    }
    data.update(
        series=rows,
        phases={phase: len(keys) for phase, keys in phases.items()},
        cache_pressure={
            "threshold": threshold,
            "status": "observed"
            if pressure
            else "not_exercised"
            if any(r["family"] == "cache_occupancy" for r in rows)
            else "unverified",
        },
        label_scopes=sorted(identities),
        failures=sum(s["status"] != "available" for s in data["snapshots"]),
        interpretation="Concurrent observations provide context, not causality. Sampling can miss peaks. Missing counters are unknown. Label changes create separate series; counters are never joined across replicas.",
    )
    data["derived"] = derive_server_metrics(data, host_configured=data.get("host_configured", False))
    return data


async def _scrape_host(run, target, phase):
    """Independent read-only exporter; never forward inference/runtime credentials."""
    from urllib.parse import urlsplit

    result = {"phase": phase, "collected_at": stamp(),
              "elapsed_seconds": time.monotonic() - run.start, "samples": [],
              "host_identity": urlsplit(target.host_metrics_url).hostname,
              "collection_period_seconds": run.config.metrics_interval_seconds,
              "max_age_seconds": run.config.metrics_max_age_seconds,
              "scope": "server-wide; configured exporter host, not runner resources"}
    remaining = min(run.config.metrics_timeout_seconds, run.deadline - time.monotonic())
    if run.stop.is_set() or remaining <= 0:
        return result | {"status": "run_stopped", "reason": "Run stopped or deadline reached."}
    try:
        def make_client():
            verify = ssl.create_default_context(cafile=run.config.ca_bundle) if run.config.ca_bundle else True
            return httpx.AsyncClient(verify=verify, proxy=run.config.proxy, timeout=remaining,
                                     follow_redirects=False)
        async with asyncio.timeout(remaining):
            client = await asyncio.to_thread(make_client)
            async with client:
                async with client.stream("GET", target.host_metrics_url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_METRICS_BODY_BYTES:
                            return result | {"status": "response_limit", "reason": "Host statistics response exceeds 1 MiB."}
                        body.extend(chunk)
        result.update(parse(body.decode("utf-8"), target, run.config, host=True))
        hosts = {s["labels"].get("nodename") for s in result["samples"]
                 if s["family"] == "host_identity" and not s["stale"]}
        result["reported_host_identities"] = sorted(h for h in hosts if h)
    except (httpx.HTTPError, OSError, ValueError, UnicodeError, TimeoutError) as exc:
        result.update(status="connection_failure", reason=f"Host statistics unavailable ({type(exc).__name__}).")
    result["scrape_duration_seconds"] = time.monotonic() - run.start - result["elapsed_seconds"]
    return result


async def scrape(run, target, phase, *, force=False):
    runtime = _scrape_runtime(run, target, phase, force=force)
    if not target.host_metrics_url:
        return await runtime
    result, host = await asyncio.gather(runtime, _scrape_host(run, target, phase))
    result["host"] = host
    result["host_configured"] = True
    return result


def _derived(value, unit, source, *, reason=None, state=None, samples=0, eligible=0, **details):
    return {"value": value, "unit": unit, "source": source,
            "state": state or ("measured" if value is not None else "insufficient_evidence"),
            "reason": reason or ("No fresh compatible observations or nonzero measurement population." if value is None else None),
            "action": None if value is not None else "Collect at least two fresh compatible exporter samples during the workload.",
            "sample_count": samples, "eligible_count": eligible, "scope": "server-wide",
            "details": details}


def _fresh_families(snapshot, families):
    """A failed, truncated, duplicate or stale scope cannot form a counter interval."""
    if snapshot.get("status") not in {"available", "absent_families"} or snapshot.get("series_limit_reached"):
        return None
    rows = {family: {} for family in families}
    for sample in snapshot.get("samples", []):
        family = sample.get("family")
        if family not in rows:
            continue
        labels = tuple(sorted(sample.get("labels", {}).items()))
        value = sample.get("value")
        if sample.get("stale") or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or labels in rows[family]:
            return None
        rows[family][labels] = value
    return rows if all(rows.values()) else None


def _counter_intervals(snapshots, families, *, matching=True):
    intervals, excluded = [], 0
    for left, right in zip(snapshots, snapshots[1:]):
        elapsed = right.get("elapsed_seconds", 0) - left.get("elapsed_seconds", 0)
        a, b = _fresh_families(left, families), _fresh_families(right, families)
        identity_changed = (left.get("reported_host_identities") != right.get("reported_host_identities")
                            or left.get("host_identity") != right.get("host_identity"))
        if identity_changed or elapsed <= 0 or not a or not b or any(a[f].keys() != b[f].keys() for f in families):
            excluded += 1
            continue
        if matching and any(a[f].keys() != a[families[0]].keys() for f in families):
            excluded += 1
            continue
        deltas = {f: {labels: b[f][labels] - value for labels, value in a[f].items()} for f in families}
        if any(v < 0 for rows in deltas.values() for v in rows.values()):
            excluded += 1
            continue
        if len(families) == 2 and families[0].endswith("_sum") and any(
                deltas[families[1]][labels] == 0 and value != 0 for labels, value in deltas[families[0]].items()):
            excluded += 1
            continue
        intervals.append((elapsed, deltas))
    return intervals, excluded


def _mean_duration(snapshots, family):
    sum_family, count_family = family + "_sum", family + "_count"
    intervals, excluded = _counter_intervals(snapshots, [sum_family, count_family])
    count = sum(sum(d[count_family].values()) for _, d in intervals)
    total = sum(sum(d[sum_family].values()) for _, d in intervals)
    return _derived(1000 * total / count if count else None, "ms", "1000 × matching Δsum / Δcount; server-wide",
                    samples=count, eligible=len(snapshots), reason=None if count else
                    "No fresh matching duration sum/count increments; reset, missing scope or zero count.",
                    duration_seconds=sum(t for t, _ in intervals), excluded_intervals=excluded,
                    attribution="Includes other server traffic; not request-linked.")


def _histogram_percentiles(snapshots, family):
    bucket_family = family + "_bucket"
    intervals, excluded = _counter_intervals(snapshots, [bucket_family])
    buckets, resolutions, population = defaultdict(float), set(), 0
    valid_intervals = 0
    for _, deltas in intervals:
        scopes = defaultdict(dict)
        try:
            for labels, value in deltas[bucket_family].items():
                labels = dict(labels)
                bound = float(labels.pop("le"))
                scopes[tuple(sorted(labels.items()))][bound] = value
        except (KeyError, ValueError):
            excluded += 1
            continue
        layouts = {tuple(sorted(rows)) for rows in scopes.values()}
        if len(layouts) != 1 or any(math.inf not in rows or any(b < a for a, b in zip(
                [rows[k] for k in sorted(rows)], [rows[k] for k in sorted(rows)][1:])) for rows in scopes.values()):
            excluded += 1
            continue
        layout = next(iter(layouts))
        if any(bound < 0 for bound in layout) or resolutions and layout not in resolutions:
            excluded += 1
            continue
        resolutions.add(layout)
        valid_intervals += 1
        for rows in scopes.values():
            population += rows[math.inf]
            for bound, count in rows.items():
                buckets[bound] += count
    output = {}
    for quantile in (.95, .99):
        value, bounds = None, None
        if population:
            rank, previous_bound, previous_count = quantile * population, 0.0, 0.0
            for bound in sorted(buckets):
                count = buckets[bound]
                if count >= rank:
                    bounds = [previous_bound, bound if math.isfinite(bound) else None]
                    if math.isfinite(bound) and count > previous_count:
                        value = 1000 * (previous_bound + (bound - previous_bound) * (rank - previous_count) / (count - previous_count))
                    break
                previous_bound, previous_count = bound, count
        output[f"p{int(quantile * 100)}"] = _derived(
            value, "ms", "Linear interpolation within classic histogram Δbucket counts; server-wide",
            state="estimated" if value is not None else "insufficient_evidence", samples=population,
            eligible=len(snapshots), reason=None if value is not None else
            "No complete compatible histogram increments, zero observations, or percentile falls in unbounded +Inf bucket.",
            bucket_bounds_seconds=bounds,
            bucket_resolution_seconds=[b for b in sorted(buckets) if math.isfinite(b)],
            excluded_intervals=excluded, observed_intervals=valid_intervals,
            approximation="Uniform observations within each finite bucket; no exact percentile or maximum.")
    return output


def derive_server_metrics(data, *, host_configured=False):
    """Derive server-wide observations from adjacent compatible scrape intervals.

    Scopes must match at both ends and across paired families. Counter resets,
    stale/missing data, failed scrapes and label/replica changes exclude intervals;
    coverage records exclusions. Replica sums/counts and bucket counts are pooled,
    never replica percentiles. Rates use actual elapsed seconds, not the configured
    polling interval. Source freshness without exporter timestamps stays unknown.
    """
    snapshots = data.get("snapshots", [])
    metrics = {}
    for family in ("server_ttft", "server_queue", "server_inference"):
        metrics[family + "_mean_ms"] = _mean_duration(snapshots, family)
        for percentile, row in _histogram_percentiles(snapshots, family).items():
            metrics[family + "_" + percentile + "_ms"] = row
    for family in ("prompt_tokens", "generated_tokens", "preemptions", "retractions"):
        intervals, excluded = _counter_intervals(snapshots, [family])
        duration = sum(t for t, _ in intervals)
        delta = sum(sum(d[family].values()) for _, d in intervals)
        key = "server_" + family + "_per_second"
        metrics[key] = _derived(delta / duration if duration else None,
                               "tokens/s" if family.endswith("tokens") else "events/s",
                               "Compatible server-wide counter delta / actual sample duration",
                               samples=len(intervals), eligible=max(0, len(snapshots)-1),
                               duration_seconds=duration, counter_delta=delta, excluded_intervals=excluded,
                               reason=None if duration else "No fresh stable counter interval; resets, missing samples or changing labels.")
    intervals, excluded = _counter_intervals(snapshots, ["prefix_hits", "prefix_queries"])
    hits = sum(sum(d["prefix_hits"].values()) for _, d in intervals)
    queries = sum(sum(d["prefix_queries"].values()) for _, d in intervals)
    metrics["prefix_cache_hit_ratio"] = _derived(hits / queries if queries and hits <= queries else None,
        "fraction", "Matching prefix-cache hit/query token-counter deltas; server-wide",
        samples=queries, eligible=len(snapshots), excluded_intervals=excluded,
        reason=None if queries and hits <= queries else "No compatible nonzero hit/query deltas; missing is not zero.")
    # Runtime direct gauges retain their units; average samples is not a time-weighted average.
    for family in ("running", "waiting", "cache_occupancy", "prefix_hit_rate", "memory", "utilization",
                   "temperature", "throttling", "device_errors"):
        samples = [s for snap in snapshots for s in snap.get("samples", [])
                   if s.get("family") == family and not s.get("stale")]
        unit = samples[0]["unit"] if samples else {
            "running": "requests", "waiting": "requests", "cache_occupancy": "fraction",
            "prefix_hit_rate": "fraction", "memory": "MiB", "utilization": "percent",
            "temperature": "celsius", "throttling": "bitmask", "device_errors": "code"}[family]
        metrics[family] = _derived(max((s["value"] for s in samples), default=None), unit,
            "Maximum fresh runtime-reported gauge sample; server-wide", samples=len(samples),
            eligible=len(snapshots), reason=None if samples else "No fresh gauge observations.",
            aggregation="Maximum across observed samples/replicas, not sum or time-weighted mean.")
    host_snapshots = [snap.get("host", {"status": "missing", "elapsed_seconds": snap.get("elapsed_seconds", 0)})
                      for snap in snapshots]
    configured = host_configured or data.get("host_configured") or any("host" in snap for snap in snapshots)
    metrics.update(_derive_host(host_snapshots, configured))
    profiles = {snap.get("profile", snap.get("mapping")) for snap in snapshots
                if snap.get("profile", snap.get("mapping"))}
    mappings = {"vllm-v1": V1, "vllm-legacy": LEGACY, "sglang": SGLANG, **ENGINE_METRICS}
    support = {family for profile in profiles for family, _, _ in mappings.get(profile, {}).values()}
    for key, row in metrics.items():
        row["details"].setdefault("freshness", "Exporter timestamps when supplied; scrape-only samples have unknown source freshness.")
        row["details"].setdefault("partial_coverage", bool(row["details"].get("excluded_intervals")))
        if row["value"] is None and not key.startswith("host_"):
            if not snapshots or all(snap.get("status") == "not_enabled" for snap in snapshots):
                row.update(state="not_collected", reason="Runtime telemetry was not collected.",
                           action="Configure and enable a supported runtime metrics endpoint.")
            elif profiles and key.startswith(("server_ttft_", "server_queue_", "server_inference_")):
                family = key.rsplit("_", 2)[0]
                required = family + ("_bucket" if "_p95_" in key or "_p99_" in key else "_sum")
                if required not in support:
                    row.update(state="not_supported", reason="Selected runtime profile has no explicit contract for this measurement.",
                               action="Use an adapter exposing documented matching duration counters or histogram buckets.")
    return metrics


def _derive_host(snapshots, configured):
    intervals, excluded = _counter_intervals(snapshots, ["host_cpu_seconds"])
    idle, total = 0, 0
    valid_cpu_intervals = 0
    for _, deltas in intervals:
        modes = defaultdict(set)
        for labels in deltas["host_cpu_seconds"]:
            labels = dict(labels)
            mode = labels.pop("mode", None)
            modes[tuple(sorted(labels.items()))].add(mode)
        if any("idle" not in values or not (values - {None, "idle", "guest", "guest_nice"}) for values in modes.values()):
            excluded += 1
            continue
        valid_cpu_intervals += 1
        for labels, delta in deltas["host_cpu_seconds"].items():
            mode = dict(labels).get("mode")
            # guest time is already included in user/nice in node_exporter Linux counters.
            if mode in {"guest", "guest_nice"}:
                continue
            total += delta
            if mode == "idle":
                idle += delta
    cpu = 100 * (1 - idle / total) if total and any(
        dict(labels).get("mode") == "idle" for _, d in intervals for labels in d["host_cpu_seconds"]) else None
    rows = {"host_cpu_percent": _derived(cpu, "percent", "100 × (1 − Δidle / Δtotal CPU seconds); node_exporter; server-wide",
        samples=valid_cpu_intervals, eligible=max(0, len(snapshots)-1), excluded_intervals=excluded)}
    # Match RAM labels in the latest scrape; stale/missing final RAM stays unavailable.
    ram = None
    for snap in snapshots[-1:]:
        fresh = _fresh_families(snap, ["host_ram_total", "host_ram_available"])
        if fresh and fresh["host_ram_total"].keys() == fresh["host_ram_available"].keys():
            if all(0 <= fresh["host_ram_available"][k] <= v for k, v in fresh["host_ram_total"].items()):
                ram = fresh
                break
    total_ram = sum(ram["host_ram_total"].values()) if ram else None
    used = total_ram - sum(ram["host_ram_available"].values()) if ram else None
    for key, value in (("host_ram_total_bytes", total_ram), ("host_ram_used_bytes", used)):
        rows[key] = _derived(value, "bytes", "Latest matching MemTotal − MemAvailable (used), or MemTotal; node_exporter; server-wide",
                             samples=len(ram["host_ram_total"]) if ram else 0, eligible=len(snapshots))
        if value is None:
            rows[key]["action"] = "Collect fresh matching node_memory_MemTotal_bytes and node_memory_MemAvailable_bytes from the configured exporter."
    identities = sorted({s.get("host_identity") for s in snapshots if s.get("host_identity")})
    for row in rows.values():
        row["details"].update(host_identities=identities, collection_period_seconds=sorted({
            s["collection_period_seconds"] for s in snapshots if "collection_period_seconds" in s}),
            freshness="Exporter timestamp when supplied; otherwise scrape time only, source freshness unknown.")
        if not configured:
            row.update(state="not_collected", reason="Server host exporter not configured.",
                       action="Set targets[].host_metrics_url to an existing node_exporter Prometheus-text endpoint.")
    return rows
