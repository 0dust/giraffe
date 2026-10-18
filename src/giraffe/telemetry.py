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
}
# Exact metric contracts from the source used for the live Apple Silicon run.
SGLANG_REFERENCE = "SGLang 44bc47886 srt/observability/metrics_collector.py"
GPU = {
    "DCGM_FI_DEV_FB_USED": ("memory", "gauge", "MiB"),
    "DCGM_FI_DEV_GPU_UTIL": ("utilization", "gauge", "percent"),
    "DCGM_FI_DEV_GPU_TEMP": ("temperature", "gauge", "celsius"),
    "DCGM_FI_DEV_CLOCKS_EVENT_REASONS": ("throttling", "gauge", "bitmask"),
    "DCGM_FI_DEV_CLOCK_THROTTLE_REASONS": ("throttling", "gauge", "bitmask"),
    "DCGM_FI_DEV_XID_ERRORS": ("device_errors", "gauge", "code"),
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


def assess_gpu(snapshots, max_age_seconds):
    """Assess published GPU devices; an advancing scrape alone proves no freshness.

    Value changes establish freshness for that device, not for other GPUs. Unseen
    devices cannot be enumerated from this endpoint and remain outside coverage.
    """
    devices, series, alerts = {}, defaultdict(list), []
    warnings, informational = [], []
    completed = 0
    for index, snap in enumerate(snapshots):
        samples = [s for s in snap.get("samples", []) if s["family"] in GPU_FAMILIES]
        completed += bool(samples)
        for sample in samples:
            labels, family, value = sample.get("labels", {}), sample["family"], sample["value"]
            # Hostname disambiguates identical GPU indices on multiple exporter hosts.
            key = (labels.get("Hostname", ""), labels.get("gpu"))
            device = devices.setdefault(key, {"families": set(), "timestamped": set(),
                                               "changing": False, "unknown_identity": key[1] is None})
            timestamp = sample.get("sample_timestamp_ms")
            age = sample.get("age_seconds")
            if timestamp is not None and age is None:
                collected = snap.get("collected_at")
                observed = datetime.fromisoformat(collected).timestamp() if collected else time.time()
                age = observed - timestamp / 1000
            stale = timestamp is not None and (age > max_age_seconds or age < -5)
            # DCGM's int64 blank/error sentinels are not measurements.
            if stale or not math.isfinite(value) or value < 0 or value >= 2**63 - 1024:
                continue
            device["families"].add(family)
            if timestamp is not None:
                device["timestamped"].add(family)
            identity = (key, sample["name"], tuple(sorted(labels.items())))
            series[identity].append((index, value))
            if family == "throttling" and int(value) & ~sum(GPU_THROTTLE_REASONS):
                informational.append({"gpu": key[1], "value": value, "snapshot": index})
    for (key, name, _), values in series.items():
        if any(a[0] != b[0] and a[1] != b[1] for a, b in zip(values, values[1:])):
            devices[key]["changing"] = True
        family = GPU[name][0]
        if family == "device_errors":
            if values[0][1] > 0:
                warnings.append({"gpu": key[1], "hostname": key[0], "historical_xid": values[0][1],
                                 "reason": "Last XID predates observation; not an error count."})
            for (_, previous), (index, value) in zip(values, values[1:]):
                if value > 0 and value != previous:
                    alerts.append({"gpu": key[1], "hostname": key[0], "family": family,
                                   "value": value, "reasons": ["XID changed during observation"], "snapshot": index})
        elif family == "throttling":
            for bit, reason in GPU_THROTTLE_REASONS.items():
                observed = [(i, v) for i, v in values if int(v) & bit]
                if len({i for i, _ in observed}) >= 2:
                    alerts.append({"gpu": key[1], "hostname": key[0], "family": family,
                                   "value": observed[-1][1], "reasons": [reason],
                                   "snapshots": sorted({i for i, _ in observed})})
    rows = []
    for (hostname, gpu), device in sorted(devices.items(), key=lambda item: str(item[0])):
        fresh = device["families"] if device["changing"] else device["timestamped"]
        missing = sorted(GPU_FAMILIES - device["families"])
        unknown = sorted(device["families"] - fresh)
        rows.append({"gpu": gpu, "hostname": hostname, "missing_families": missing,
                     "unknown_freshness_families": unknown, "identity_verified": not device["unknown_identity"],
                     "freshness": "values changed across scrapes" if device["changing"] else
                     "source timestamps" if not unknown else "unknown"})
    unavailable = sorted({f for row in rows for f in row["missing_families"]}) if rows else sorted(GPU_FAMILIES)
    metrics = {"attempted": len(snapshots), "completed": completed, "snapshots": snapshots,
               "unavailable_families": unavailable, "devices": rows, "alerts": alerts,
               "warnings": warnings, "informational_clock_events": informational,
               "coverage": "Published GPU labels only; devices absent from all scrapes cannot be discovered.",
               "metrics_max_age_seconds": max_age_seconds}
    qualifier = "this is not device health certification."
    if alerts:
        return metrics, "fail", "GPU telemetry reports a new XID or repeated adverse slowdown; " + qualifier
    if not rows or any(row["missing_families"] or row["unknown_freshness_families"] or
                       not row["identity_verified"] for row in rows):
        return metrics, "inconclusive", "GPU observations lack per-device coverage or freshness evidence; " + qualifier
    return metrics, "pass", "Fresh GPU telemetry covers every observed device without new XID or repeated adverse slowdown events; " + qualifier


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


def parse(body, target, config):
    profile = (
        V1
        if target.metrics_profile == "vllm-v1"
        else LEGACY
        if target.metrics_profile == "vllm-legacy"
        else SGLANG
        if target.metrics_profile == "sglang"
        else {}
    )
    mapping = profile | GPU
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
            if len(labels) >= 16:
                truncated = True
                break
            labels[key] = value[:256]
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
                "provenance": "runtime-reported metrics",
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
    required = (
        (SERVING_FAMILIES - {"preemptions"}) | {"retractions"}
        if target.metrics_profile == "sglang" else SERVING_FAMILIES
    )
    return {
        "status": state,
        "samples": samples,
        "missing_families": sorted(required - families),
        "series_limit_reached": truncated,
        "mapping": target.metrics_profile,
        "mapping_reference": "vLLM 0.30.0 v1/metrics/loggers.py"
        if target.metrics_profile == "vllm-v1"
        else SGLANG_REFERENCE
        if target.metrics_profile == "sglang"
        else "explicit legacy metric-name contract; runtime/version qualification required"
        if target.metrics_profile == "vllm-legacy"
        else "No serving metric-name contract selected.",
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


async def scrape(run, target, phase, *, force=False):
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
                        if len(body) + len(chunk) > 1024 * 1024:
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
            and len(run.observations[target.name]["deployment_history"]) < 32
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
    return data
