"""Conservative arrival advisories from compact, locally saved run evidence."""

import math

from .deployment import fixture_hash
from .fixtures import FIXTURE_VERSION
from .load_test import point_shapes, settings_sets, test_points
from .models import RunConfig


def workload_keys(config):
    if config.custom_fixtures:
        return {}
    shared = fixture_hash({"version": 1, "fixtures": FIXTURE_VERSION,
        "request_source": config.request_source,
        "request_pack": config.request_pack.model_dump(mode="json") if config.request_source == "request_pack" and config.request_pack else None,
        "request_options": config.request_options, "stream": config.stream,
        "max_output_tokens": config.max_output_tokens, "request_timeout_seconds": float(config.request_timeout_seconds),
        "context_limit": config.context_limit, "concurrency": config.concurrency,
        "max_requests": config.max_requests, "max_duration_seconds": config.max_duration_seconds,
        "stop_after_errors": config.stop_after_errors, "overlap_models": config.overlap_models,
        "max_memory_mb": config.max_memory_mb, "unresponsive_after_failures": config.unresponsive_after_failures,
        "unresponsive_for_seconds": float(config.unresponsive_for_seconds), "checks": config.checks,
        "judging": config.judging.model_dump(mode="json"),
        "test_options": {name: options.model_dump(mode="json") for name, options in config.test_options.items()},
        "targets": [{key: target.model_dump(mode="json").get(key) for key in ("name", "url", "model", "tokenizer", "identity")}
                    for target in config.targets]})
    settings_keys = {}
    for source, settings in settings_sets(config.load_test):
        values = settings.model_dump(mode="json")
        values.pop("pass_criteria", None)
        values["patterns"].pop("rates", None)
        settings_keys[source] = fixture_hash(values)
    return {point["id"]: fixture_hash({"shared": shared, "settings": settings_keys[point["source"]],
            "point": {key: point.get(key) for key in ("source", "type", "workload", "pattern", "bursts")}})
            for point in test_points(config.load_test)}


def measurements(report, target_name):
    try:
        config = RunConfig.model_validate(report.manifest.get("config", {}))
    except ValueError:
        return []
    if "load" not in config.checks or config.restart_target:
        return []
    keys = workload_keys(config)
    points = report.observations.get("targets", {}).get(target_name, {}).get("load_test", {}).get("points", [])
    rows = []
    for point in points:
        if point.get("pattern") not in {"constant", "poisson"}:
            continue
        key = keys.get(point["id"])
        if key is None:
            continue
        for group in point.get("groups", []):
            rows.append({"family": "arrival_history", "workload_key": key, "shape": group.get("shape"),
                "rate": point.get("rate"),
                **{name: group.get(name) for name in ("scheduled", "dispatched", "not_sent", "local_drops",
                    "late_arrivals", "not_offered", "completed", "scored", "correct", "completion_p95_ms")}})
    return rows


def _valid(row):
    if any(not isinstance(row.get(key), str) for key in ("shape", "run_id", "cohort", "finished_at")):
        return False
    count_fields = ("scheduled", "dispatched", "not_sent", "local_drops", "late_arrivals", "not_offered",
                    "completed", "scored", "correct")
    if any(type(row.get(key)) is not int or row[key] < 0 for key in count_fields):
        return False
    latency = row.get("completion_p95_ms")
    rate = row.get("rate")
    return (type(rate) in {int, float} and math.isfinite(rate) and rate > 0
            and row["scheduled"] > 0 and row["dispatched"] + row["not_sent"] == row["scheduled"]
            and row["not_sent"] == row["local_drops"] + row["late_arrivals"] + row["not_offered"]
            and row["correct"] <= row["scored"] <= row["completed"] <= row["dispatched"]
            and (latency is None or type(latency) in {int, float} and math.isfinite(latency) and latency >= 0))


def guidance(config, observations):
    from .latency import _datetime

    if "load" not in config.checks:
        return []
    own = dict(settings_sets(config.load_test))
    keys = workload_keys(config)
    rows = []
    for target in config.targets:
        entries = (observations or {}).get(target.name, [])
        for point in test_points(config.load_test):
            if point["pattern"] not in {"constant", "poisson"}:
                continue
            key = keys.get(point["id"])
            matches = [entry for entry in entries if key is not None
                and entry.get("family") == "arrival_history" and entry.get("workload_key") == key
                and entry.get("deployment_known") is True and entry.get("rate") == point["rate"] and _valid(entry)]
            if not matches:
                rows.append({"target": target.name, "point_id": point["id"], "status": "unknown",
                    "message": "No recent run with matching arrival workload, rate and deployment metadata."})
                continue
            latest = max(matches, key=lambda entry: _datetime(entry["finished_at"]))
            cohort = [entry for entry in matches if entry["cohort"] == latest["cohort"]]
            expected_shapes = set(point_shapes(own[point["source"]], point))
            if len(cohort) != len(expected_shapes) or {entry["shape"] for entry in cohort} != expected_shapes:
                rows.append({"target": target.name, "point_id": point["id"], "status": "unknown",
                             "message": "Previous arrival evidence is incomplete for the selected workload shapes."})
                continue
            scheduled = sum(entry["scheduled"] for entry in cohort)
            sent = sum(entry["dispatched"] for entry in cohort)
            dropped = sum(entry["not_sent"] for entry in cohort)
            scored = sum(entry["scored"] for entry in cohort)
            correct = sum(entry["correct"] for entry in cohort)
            # Percentiles cannot be averaged across workload shapes.
            latencies = {entry["shape"]: entry["completion_p95_ms"] for entry in cohort}
            trial = point["rate"] / 2 if dropped else None
            correctness = f"{correct}/{scored} scored answers correct" if scored else "correctness unknown"
            latency_text = ", ".join(f"{shape}: {value:g} ms" if value is not None else f"{shape}: unknown"
                                     for shape, value in latencies.items())
            message = (f"{target.name}, {point['workload']} at {point['rate']:g} requests/s: run {latest['run_id']} "
                f"({latest['finished_at']}) sent {sent}/{scheduled}; {dropped} not sent "
                f"({sum(e['local_drops'] for e in cohort)} local drops, {sum(e['late_arrivals'] for e in cohort)} late, "
                f"{sum(e['not_offered'] for e in cohort)} not offered). Client completion p95 by shape: {latency_text}; {correctness}. "
                "Configured deployment metadata matches; runtime continuity is unverified.")
            if trial is not None:
                message += f" Consider a lower trial at {trial:g} requests/s. This is not a capacity estimate; settings are unchanged."
            rows.append({"target": target.name, "point_id": point["id"], "status": "warning" if dropped else "observed",
                "run_id": latest["run_id"], "finished_at": latest["finished_at"], "scheduled": scheduled,
                "dispatched": sent, "not_sent": dropped, "completion_p95_ms_by_shape": latencies,
                "correct": correct, "scored": scored, "suggested_trial_rate": trial, "message": message})
    return rows
