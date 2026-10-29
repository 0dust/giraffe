"""Compact, comparable latency observations for the request planner."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import httpx
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .client import completion_url
from .conditions import condition_for
from .stats import percentile

if TYPE_CHECKING:
    from .models import RunConfig, RunReport

_MAX_CONTEXT_PREVIEW_CHARS = 1024**2
_HISTOGRAM_SCALE = 32  # Fixed log2 buckets: upper/lower <= 1.022. Counts merge exactly.


def _histogram(values: list[float]) -> dict:
    counts = Counter(math.floor(math.log2(value) * _HISTOGRAM_SCALE) for value in values if value > 0)
    return {"scale": _HISTOGRAM_SCALE, "zero": sum(value == 0 for value in values),
            "buckets": sorted([index, count] for index, count in counts.items())}


def _valid_histogram(entry: dict) -> bool:
    histogram = entry.get("histogram_ms", {})
    if not isinstance(histogram, dict) or histogram.get("scale") != _HISTOGRAM_SCALE:
        return False
    buckets, zero = histogram.get("buckets"), histogram.get("zero")
    return (type(zero) is int and zero >= 0 and isinstance(buckets, list) and len(buckets) <= 2048
            and all(isinstance(pair, list) and len(pair) == 2 and type(pair[0]) is int
                    and -32768 <= pair[0] < 32736 and type(pair[1]) is int and pair[1] > 0 for pair in buckets)
            and len({pair[0] for pair in buckets}) == len(buckets)
            and sum(pair[1] for pair in buckets) + zero == entry.get("n"))


def _histogram_quantile(histogram: dict, quantile: float, minimum: float, maximum: float) -> float:
    if quantile == 0 or minimum == maximum:
        return minimum
    if quantile == 1:
        return maximum
    count = histogram["zero"] + sum(pair[1] for pair in histogram["buckets"])
    rank = (count - 1) * quantile + .5
    cumulative = histogram["zero"]
    if rank <= cumulative:
        return 0
    for index, frequency in sorted(histogram["buckets"]):
        if rank <= cumulative + frequency:
            lower, upper = 2 ** (index / _HISTOGRAM_SCALE), 2 ** ((index + 1) / _HISTOGRAM_SCALE)
            return max(minimum, min(maximum, lower + (upper - lower) * (rank - cumulative) / frequency))
        cumulative += frequency
    return maximum


def input_bucket(input_chars: int) -> str:
    low, high = 0, 255
    while input_chars > high:
        low, high = high + 1, (high + 1) * 4 - 1
    return f"chars:{low}-{high}"


def _deployment_fingerprint(deployment: dict) -> str | None:
    facts = {key: deployment[key] for key in (
        "model", "runtime", "serving", "backend", "hardware", "software", "extension", "launch_command")
        if deployment.get(key)}
    if deployment.get("metadata_file"):
        from .deployment import load_metadata

        try:
            imported = load_metadata(deployment["metadata_file"])
        except (OSError, ValueError):
            return None
        for category in ("model", "runtime", "serving", "backend", "hardware", "software", "extension"):
            if imported.get(category):
                facts[category] = {**facts.get(category, {}), **imported[category]}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest() if facts else None


def latency_summary(report: RunReport) -> dict:
    """Summarize actual specs and measured conditions without retaining answers."""
    config = report.manifest.get("config", {})
    summary = {"version": 3, "finished_at": report.finished_at, "targets": []}
    if config.get("restart_target"):
        return summary
    targets = config.get("targets", [])
    ceiling = config.get("concurrency")
    for target in targets:
        name = target["name"]
        observation = report.observations.get("targets", {}).get(name, {})
        conditions = {row["condition"]["id"]: row["condition"]
                      for row in observation.get("conditions", []) if row.get("condition")}
        groups = defaultdict(list)
        timings = defaultdict(list)
        for record in report.requests:
            if (record.target != name or record.status != "completed" or not record.valid
                    or record.error or record.intentional or record.client_cancelled_ms is not None
                    or record.scenario in {"initial", "readiness", "recovery_prime"}
                    or "calibration" in record.scenario or record.elapsed_ms < 0
                    or not math.isfinite(record.elapsed_ms)):
                continue
            condition = conditions.get(record.condition_id or record.scenario)
            if condition is None:
                condition = condition_for(record.scenario, record.workload)
            parameters = condition.get("parameters", {})
            concurrency = parameters.get("concurrency", condition.get("level", record.workload.get("level", 1)))
            if isinstance(ceiling, int) and ceiling > 0:
                concurrency = min(concurrency, ceiling)
            family = condition["check"]
            if record.load_point and record.load_point.get("point_id"):
                detail = record.load_point
                concurrency = detail.get("level") or 1
                family = f"load.{detail['type']}.{detail['pattern']}" + (".bursts" if detail.get("bursts") else "")
            key = (family, concurrency, record.stream, record.requested_max_tokens,
                   input_bucket(record.input_chars))
            groups[key].append(record.elapsed_ms)
            timings[key].append(record)
        for values in groups.values():
            values.sort()
        measurements = [{"family": key[0], "concurrency": key[1], "stream": key[2],
                         "output_cap": key[3], "input_bucket": key[4], "n": len(values),
                         "p50_ms": percentile(values, .5), "p90_ms": percentile(values, .9),
                         "p99_ms": percentile(values, .99), "max_ms": max(values),
                         "min_ms": min(values), "histogram_ms": _histogram(values),
                         "quantiles_ms": [round(percentile(values, index / 20), 6) for index in range(20)] + [max(values)],
                         "cohort": report.run_id}
                        for key, values in sorted(groups.items())]
        blocks = defaultdict(list)
        previous = None
        for record, key in sorted(((record, key) for key, records in timings.items() for record in records),
                                  key=lambda item: item[0].dispatch_ms or 0):
            boundary = (key, record.scenario, record.workload.get("phase"), record.workload.get("round"))
            if boundary != previous:
                blocks[key].append([])
            blocks[key][-1].append(record)
            previous = boundary
        for measurement, (key, records) in zip(measurements, sorted(timings.items())):
            # Replay immediate replenishment with the measured durations. Batch
            # maxima can exceed the real pool span and conceal dispatch gaps.
            # Keep local scheduling overhead separate from endpoint latency.
            if (key[0] not in {"serving", "generation", "correctness", "json", "consistency", "prefix", "tools"}
                    or any(record.dispatch_ms is None or record.completed_ms is None
                           or record.completed_ms <= record.dispatch_ms for record in records)):
                continue
            # Other levels/buckets between these batches are traffic, not local
            # bookkeeping overhead. Measure contiguous blocks of this setting.
            span = sum(max(record.completed_ms for record in block) - block[0].dispatch_ms
                       for block in blocks[key])
            modeled = 0
            for block in blocks[key]:
                available = [0.] * min(key[1], len(block))
                for record in block:
                    heapq.heapreplace(available, available[0] + record.elapsed_ms)
                modeled += max(available)
            measurement["batch_timing_coverage"] = round(min(1, modeled / span), 6)
            gaps = sum(max(0, math.ceil(len(block) / key[1]) - 1) for block in blocks[key])
            if gaps:
                measurement["batch_overhead_ms"] = max(0, span - modeled) / gaps
        calibration = observation.get("context_calibration", {})
        if isinstance(calibration, dict) and calibration.get("status") in {"sized_locally", "sized_from_observed_usage", "unavailable"}:
            from .fixtures import FIXTURE_VERSION

            configured = report.manifest.get("effective_checks", {}).get("context", {})
            context_limit = configured.get("context_limit", config.get("test_options", {}).get(
                "context", {}).get("context_limit") or config.get("context_limit", 4096))
            profile = {"fixture_version": FIXTURE_VERSION, "context_limit": context_limit,
                       "fixture_context_limit": calibration.get("fixture_context_limit", context_limit)}
            if calibration.get("status") == "sized_locally":
                # Keep only request-shape metadata, never the prepared prompt.
                shapes = {record.fixture_id: {"fixture_id": record.fixture_id,
                          "input_chars": record.input_chars, "output_cap": record.requested_max_tokens}
                          for key, records in timings.items() if key[0] == "context" for record in records}
                profile.update(method="local_tokenizer", tokenizer=target.get("tokenizer") or target["model"],
                               fixtures=[shapes[key] for key in sorted(shapes)])
            for measurement in measurements:
                if measurement["family"] == "context":
                    measurement["context_profile"] = profile
        observed_deployment = observation.get("deployment") or {}
        fingerprint = (observed_deployment.get("fingerprint")
                       or _deployment_fingerprint(target.get("deployment", {})))
        summary["targets"].append({"name": name, "url": completion_url(target["url"]),
            "model": target["model"], "deployment_fingerprint": fingerprint,
            "measurements": measurements})
    return summary


def _datetime(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def load_observations(runs_dir: Path, config: RunConfig, *, now: datetime | None = None,
                      cache: dict | None = None) -> dict[str, list[dict]]:
    """Read only cached metadata; request planning never scans report evidence."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    cache = cache if cache is not None else {}
    observations = {target.name: [] for target in config.targets}
    identities = {}
    for target in config.targets:
        try:
            identities[target.name] = (completion_url(target.url),
                _deployment_fingerprint(target.deployment.model_dump(mode="json")))
        except httpx.InvalidURL:
            continue
    try:
        paths = list(Path(runs_dir).glob("*/.web.json"))
    except OSError:
        return observations
    seen = set()
    for path in paths:
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            key = (str(path), path.stat().st_mtime_ns)
            seen.add(key)
            if key not in cache:
                cache[key] = json.loads(path.read_text())
            if cache[key].get("state") in {"error", "cancelled"}:
                continue
            summary = cache[key].get("latency_summary", {})
            finished = _datetime(summary["finished_at"])
            if not cutoff <= finished <= now:
                continue
            for target in config.targets:
                if target.name not in identities:
                    continue
                url, fingerprint = identities[target.name]
                if target.deployment.metadata_file and fingerprint is None:
                    continue
                for saved in summary.get("targets", []):
                    if (saved.get("url") != url or saved.get("model") != target.model
                            or saved.get("deployment_fingerprint") != fingerprint):
                        continue
                    observations[target.name].extend({**entry, "finished_at": summary["finished_at"],
                                                     "cohort": str(path.resolve())}
                                                     for entry in saved.get("measurements", []))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    for key in list(cache):
        if key not in seen:
            del cache[key]
    for entries in observations.values():
        entries.sort(key=lambda entry: _datetime(entry["finished_at"]), reverse=True)
    return observations


def comparable_latency(observations: dict | None, target_name: str, *, family: str,
                       concurrency: int, stream: bool, output_cap: int, input_bucket: str,
                       cohort: str | None = None) -> dict | None:
    """Pool the latest consistent comparable block; a changed cap has 25% tolerance.

    Runs have equal total weight, so a large older run cannot outweigh a small
    recent run. Legacy quantile-only summaries remain usable individually, never merged.
    Anchor every shift and pooled-median comparison to the newest run, so a
    gradually moving reference cannot admit an arbitrarily old latency regime.
    """
    matches = []
    for entry in (observations or {}).get(target_name, []):
        if (cohort is not None and entry.get("cohort") != cohort):
            continue
        if (entry.get("family") != family or entry.get("concurrency") != concurrency
                or entry.get("stream") != stream or entry.get("input_bucket") != input_bucket):
            continue
        cap = entry.get("output_cap")
        if (not isinstance(cap, (int, float)) or not .75 * output_cap <= cap <= 1.25 * output_cap
                or not isinstance(entry.get("n"), int) or entry["n"] < 1):
            continue
        p50, p90 = entry.get("p50_ms"), entry.get("p90_ms")
        if (not isinstance(p50, (int, float)) or not isinstance(p90, (int, float))
                or not math.isfinite(p50) or not math.isfinite(p90) or not 0 <= p50 <= p90):
            continue
        result = {"n": entry["n"], "p50_ms": p50, "p90_ms": p90, "cohort": entry.get("cohort")}
        coverage = entry.get("batch_timing_coverage")
        if isinstance(coverage, (int, float)) and math.isfinite(coverage) and 0 <= coverage <= 1:
            result["batch_timing_coverage"] = coverage
        overhead = entry.get("batch_overhead_ms")
        if isinstance(overhead, (int, float)) and math.isfinite(overhead) and overhead >= 0:
            result["batch_overhead_ms"] = overhead
        p99, maximum, quantiles = entry.get("p99_ms"), entry.get("max_ms"), entry.get("quantiles_ms")
        if (isinstance(p99, (int, float)) and isinstance(maximum, (int, float))
                and math.isfinite(p99) and math.isfinite(maximum) and p90 <= p99 <= maximum):
            result.update(p99_ms=p99, max_ms=maximum)
            if (isinstance(quantiles, list) and len(quantiles) in {21, 101}
                    and all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
                            for value in quantiles)
                    and quantiles == sorted(quantiles) and quantiles[-1] == maximum):
                result["quantiles_ms"] = quantiles
        if ("max_ms" in result and _valid_histogram(entry)
                and isinstance(entry.get("min_ms"), (int, float)) and 0 <= entry["min_ms"] <= p50):
            result.update(min_ms=entry["min_ms"], histogram_ms=entry["histogram_ms"])
        matches.append(result)
    if not matches:
        return None
    if "histogram_ms" not in matches[0]:
        return matches[0]
    mergeable = []
    for entry in matches:
        if "histogram_ms" not in entry:
            break
        mergeable.append(entry)
    block = []
    shifted = False
    # Inspect the latest five runs even if an earlier pairwise boundary will
    # truncate the block. Otherwise the boundary can hide the trend's reason.
    recent_cohorts = list(dict.fromkeys(entry.get("cohort") for entry in mergeable))[:5]
    trending = _median_trend([entry for entry in mergeable if entry.get("cohort") in recent_cohorts])
    for entry in mergeable:
        candidate = [*block, entry]
        candidate_trending = _median_trend(candidate)
        trending |= candidate_trending
        merged = _merge_block(candidate, distribution=False)
        # Bound distance from the newest run, allowing its median's sampling
        # uncertainty but never more than the existing small-sample 2.5x guard.
        median_bound = max(math.log(1.2), min(math.log(2.5), 3.291 * math.sqrt(
            _median_error(merged) + _median_error(matches[0]))))
        if block and (candidate_trending or _median_shift(matches[0], entry)
                      or _median_distance(merged, matches[0]) > median_bound):
            shifted = True
            break
        block.append(entry)
    pooled = _merge_block(block)
    pooled["drift_detected"] = shifted
    pooled["latency_trending"] = trending
    return pooled


def _median_distance(current: dict, candidate: dict) -> float:
    return abs(math.log(max(current["p50_ms"], 1e-9) / max(candidate["p50_ms"], 1e-9)))


def _median_trend(block: list[dict]) -> bool:
    """Flag strong run-order trends with a material (25%) end-to-end change.

    Spearman rho uses average ranks for ties. Require five runs because three
    randomly ordered medians are monotonic one third of the time. |rho| >= .99
    plus 25% end-to-end movement is a stability gate, not a significance test.
    For five untied medians this requires perfect order (two-sided chance 1/60).
    """
    cohorts = {}
    for entry in block:
        cohorts.setdefault(entry.get("cohort"), []).append(entry)
    block = [_merge_block(entries, distribution=False) if len(entries) > 1 else entries[0]
             for entries in cohorts.values()]
    medians = [entry["p50_ms"] for entry in block]
    if len(medians) < 5 or _median_distance(block[0], block[-1]) < math.log(1.25):
        return False
    ranks = [sum(other < value for other in medians) + (sum(other == value for other in medians) - 1) / 2
             for value in medians]
    middle = (len(ranks) - 1) / 2
    numerator = sum((index - middle) * (rank - middle) for index, rank in enumerate(ranks))
    denominator = math.sqrt(sum((index - middle) ** 2 for index in range(len(ranks)))
                            * sum((rank - middle) ** 2 for rank in ranks))
    return denominator > 0 and abs(numerator / denominator) >= .99


def _median_error(entry: dict) -> float:
    def quantile(q):
        return _histogram_quantile(entry["histogram_ms"], q, entry["min_ms"], entry["max_ms"])
    spread = math.log(max(quantile(.75), 1e-9) / max(quantile(.25), 1e-9)) / 1.349
    if entry["n"] < 20 and entry["max_ms"] > entry["min_ms"] * 1.01:
        spread = max(.4, spread)
    return (1.253 * spread) ** 2 / entry.get("effective_n", entry["n"])


def _median_shift(current: dict, candidate: dict) -> bool:
    """Do not cross a median shift outside a two-sided 99.9% sampling interval.

    Log-space IQR estimates median uncertainty robustly without allowing a rare
    slow maximum to conceal a deployment/latency shift. The 10% floor exceeds
    histogram quantization and avoids splitting constant low-latency cohorts.
    """
    distance = _median_distance(current, candidate)
    small_variable_sample = min(current["n"], candidate["n"]) < 10 and any(
        entry["max_ms"] > entry["min_ms"] * 1.01 for entry in (current, candidate))
    floor = math.log(2.5 if small_variable_sample else 1.1)
    return distance > max(floor, 3.291 * math.sqrt(_median_error(current) + _median_error(candidate)))


def _merge_block(block: list[dict], *, distribution: bool = True) -> dict:
    sizes = Counter()
    for entry in block:
        sizes[entry.get("cohort")] += entry["n"]
    total = sum(sizes.values())
    counts = Counter()
    zero = 0
    for entry in block:
        weight = total / (len(sizes) * sizes[entry.get("cohort")])
        for index, count in entry["histogram_ms"]["buckets"]:
            counts[index] += count * weight
        zero += entry["histogram_ms"]["zero"] * weight
    histogram = {"scale": _HISTOGRAM_SCALE, "zero": zero,
                 "buckets": sorted([index, count] for index, count in counts.items())}
    minimum, maximum = min(entry["min_ms"] for entry in block), max(entry["max_ms"] for entry in block)
    cohorts = list(dict.fromkeys(entry.get("cohort") for entry in block))
    result = {**block[0], "n": sum(entry["n"] for entry in block), "min_ms": minimum, "max_ms": maximum,
              "effective_n": (total if len(set(sizes.values())) == 1 else
                              len(sizes) ** 2 / sum(1 / count for count in sizes.values())),
              "histogram_ms": histogram, "cohorts": cohorts, "run_count": len(cohorts)}
    if distribution:
        result["quantiles_ms"] = (block[0]["quantiles_ms"] if len(block) == 1 and "quantiles_ms" in block[0] else
                                  [_histogram_quantile(histogram, index / 100, minimum, maximum) for index in range(101)])
    else:
        result.pop("quantiles_ms", None)
    for name, quantile in (("p50_ms", .5), ("p90_ms", .9), ("p99_ms", .99)):
        result[name] = (_histogram_quantile(histogram, quantile, minimum, maximum)
                        if len(block) > 1 else block[0][name])
    # Retain the strictest coverage: an older fast cohort cannot hide newer
    # unmodelled scheduler/runner overhead.
    result["batch_timing_coverage"] = min(entry.get("batch_timing_coverage", 0) for entry in block)
    if (len(cohorts) >= 3 and result["batch_timing_coverage"] >= .95
            and all(isinstance(entry.get("batch_overhead_ms"), (int, float))
                    and math.isfinite(entry["batch_overhead_ms"]) and entry["batch_overhead_ms"] >= 0 for entry in block)):
        overheads = Counter()
        for entry in block:
            overheads[entry.get("cohort")] += entry["batch_overhead_ms"] * entry["n"] / sizes[entry.get("cohort")]
        mean = sum(overheads.values()) / len(cohorts)
        result["batch_overhead_ms"] = mean
        result["batch_overhead_variance_ms2"] = sum((value - mean) ** 2 for value in overheads.values()) / (len(cohorts) - 1)
        result["modeled_timing_coverage"] = 1
    else:
        # A single run's overhead is retained as evidence, never as an attested
        # repeated-run model; nor can a large/unknown overhead be explained away.
        result.pop("batch_overhead_ms", None)
    return result


def calibrated_context_fixtures(observations: dict | None, target_name: str, config: RunConfig, *,
                                cohort: str | None = None) -> list | None:
    """Recover matching request shapes for planning; these specs are never sent."""
    from .fixtures import FIXTURE_VERSION, builtin_fixtures

    options = config.effective_check("context")
    context_limit = options["context_limit"]
    for entry in (observations or {}).get(target_name, []):
        if entry.get("family") != "context" or (cohort is not None and entry.get("cohort") != cohort):
            continue
        profile = entry.get("context_profile", {})
        if not isinstance(profile, dict):
            continue
        fixture_limit = profile.get("fixture_context_limit")
        if (profile.get("fixture_version") != FIXTURE_VERSION or profile.get("context_limit") != context_limit
                or type(fixture_limit) is not int
                or not 128 <= fixture_limit <= min(context_limit * 16, _MAX_CONTEXT_PREVIEW_CHARS)):
            continue
        effective = config.model_copy(update={**{key: value for key, value in options.items() if key != "limits"},
                                              "context_limit": fixture_limit})
        specs = builtin_fixtures(effective, check_id="context")["context"]
        if profile.get("method") == "local_tokenizer":
            target = next((target for target in config.targets if target.name == target_name), None)
            if target is None or profile.get("tokenizer") != (target.tokenizer or target.model):
                continue
            shapes = profile.get("fixtures")
            if (not isinstance(shapes, list) or len(shapes) != len(specs)
                    or any(not isinstance(shape, dict) or not isinstance(shape.get("fixture_id"), str)
                           for shape in shapes)):
                continue
            by_id = {shape.get("fixture_id"): shape for shape in shapes}
            if set(by_id) != {spec.fixture_id for spec in specs}:
                continue
            if any(type(by_id[spec.fixture_id].get("input_chars")) is not int
                   or not 1 <= by_id[spec.fixture_id]["input_chars"] <= _MAX_CONTEXT_PREVIEW_CHARS
                   or by_id[spec.fixture_id].get("output_cap") != spec.max_tokens for spec in specs):
                continue
            for spec in specs:
                spec.input_chars = by_id[spec.fixture_id]["input_chars"]
        return specs
    return None
