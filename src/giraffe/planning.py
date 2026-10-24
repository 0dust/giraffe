"""Describe selected target work without traffic, credentials or local input imports."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from decimal import Decimal
from functools import lru_cache
from statistics import NormalDist
import math
from typing import Any

from giraffe.fixtures import builtin_fixtures
from giraffe.concurrent_load import prompt_overhead
from giraffe.logprob_workload import corpus_width, sample_count
from giraffe.load_test import context_window, calibration_keys, load_time_plan, minimum_reservation, test_points, point_shapes, settings_sets, saved_shape_warnings
from giraffe.models import CATALOGUE_NAMES, CHECK_NAMES, CHECK_OPTION_FIELDS, CORE_CHECKS, MAX_PLANNED_ARRIVALS, ConfigDraft, RunConfig, required_concurrency
from giraffe.request_pack import active_pack, pack_summary, pack_settings
from giraffe.workloads import fixed_session_turns, label_prompt, prefix_admitted
from giraffe.tool_checks import resolved_tool_fixtures, resolved_tool_modes


def _warning_title(row):
    identifier = row["id"]
    check = {"initial": "access", "warm": "access", "context_first_batch": "context",
             "pack_control": "correctness", "pack_loaded": "correctness",
             "limits.stop_control": "cancellation", "backend_cancellation": "cancellation",
             "cancellation_intentional": "cancellation", "cancellation_wait": "cancellation",
             "arrivals_pending": "arrivals", "restart_readiness": "access",
             "recovery_prime": "recovery", "recovery_baseline": "recovery",
             "sustained": "recovery", "recovery_wait": "recovery", "sessions_wait": "sessions"}.get(identifier, identifier)
    if identifier.startswith("load_"):
        check = "load"
    return CATALOGUE_NAMES.get(check, row["title"])


def _steady_count(rate: float, duration: float) -> int:
    # Match arrival_schedule's n / rate < duration test, including float edges,
    # without allocating every arrival or assigning its workload class.
    count = math.ceil(rate * duration)
    while count > 0 and (count - 1) / rate >= duration:
        count -= 1
    while count / rate < duration:
        count += 1
    return min(count, MAX_PLANNED_ARRIVALS)


def _time_plan(config: RunConfig | ConfigDraft, rows: list[dict[str, Any]], observations=None) -> dict[str, Any]:
    """Describe known traffic time separately from requests at unknown speed."""
    from giraffe.latency import calibrated_context_fixtures, comparable_latency, input_bucket

    components, unknowns, timeline = [], [], []
    uncertain_observations = []
    observation_notes = []
    duration_estimates, duration_runs = {}, {}
    selected = set(config.checks)
    requirements = required_concurrency(config)
    pool = min(requirements["peak"], config.concurrency) if config.concurrency else requirements["peak"]
    shares = {target.name: min(requirements["per_target_peak"], pool) for target in config.targets}

    effective_configs, fixture_metadata = {}, {}

    def effective(check):
        if check not in effective_configs:
            options = config.effective_response() if check == "serving" else config.effective_check(check)
            effective_configs[check] = config.model_copy(update={key: value for key, value in options.items() if key != "limits"})
        return effective_configs[check]

    def fixtures_for(check):
        if check not in fixture_metadata:
            fixture_metadata[check] = builtin_fixtures(effective(check), check_id=check)
        return fixture_metadata[check]

    def pooled_matches(target, queries, entries):
        # Only complete cohorts may participate. Do not invent a complete
        # serving workload from disjoint historical fixtures or load levels.
        signatures = [tuple(sorted(query.items())) for query in queries]
        unique = list(dict.fromkeys(signatures))
        queries = [dict(signature) for signature in unique]
        cohorts = list(dict.fromkeys(entry.get("cohort") for entry in entries))
        complete = []
        for cohort in cohorts:
            source = {target: [entry for entry in entries if entry.get("cohort") == cohort]}
            matches = [comparable_latency(source, target, **query) for query in queries]
            if matches and all(item is not None for item in matches):
                complete.append(cohort)
        source = {target: [entry for entry in entries if entry.get("cohort") in complete]}
        matches = [comparable_latency(source, target, **query) for query in queries]
        if not matches or any(item is None for item in matches):
            return None
        # A shift in any fixture cuts the block for the whole component.
        common = set.intersection(*(set(item.get("cohorts", [item.get("cohort")])) for item in matches))
        drift = any(item.get("drift_detected") for item in matches)
        trending = any(item.get("latency_trending") for item in matches)
        if not common:
            return None
        source = {target: [entry for entry in entries if entry.get("cohort") in common]}
        matches = [comparable_latency(source, target, **query) for query in queries]
        resolved = dict(zip(unique, matches))
        return {"matches": [resolved[signature] for signature in signatures],
                "run_count": len(common), "drift_detected": drift, "latency_trending": trending}

    def measured(target, check, concurrency, specs, source=None):
        entries = (source if source is not None else observations or {}).get(target, [])
        queries = [dict(family=check, concurrency=concurrency, stream=config.stream and spec.stream,
                        output_cap=spec.max_tokens, input_bucket=input_bucket(spec.input_chars or 0)) for spec in specs]
        matched = pooled_matches(target, queries, entries)
        if matched:
            converted = [{key: value / 1000 if key.endswith("_ms") and isinstance(value, (int, float))
                          else [item / 1000 for item in value] if key == "quantiles_ms" else value
                          for key, value in item.items()} for item in matched["matches"]]
            return {**matched, **{key: max(item[key] for item in converted) for key in ("p50_ms", "p90_ms")},
                    "min_ms": min(item.get("min_ms", 0) for item in converted), "matches": converted}
        return None

    def observation_note(target, row, observed):
        runs = observed["run_count"]
        if observed.get("latency_trending"):
            uncertain_observations.append(f"{target} · {_warning_title(row)}: latency is trending across recent runs; "
                                          "older measurements cannot support medium confidence.")
        observation_notes.append(f"{target} · {_warning_title(row)}: based on {runs} earlier {'run' if runs == 1 else 'runs'} of this endpoint."
                                 + (" Latency drift detected; only the most recent consistent block is used." if observed["drift_detected"] else ""))

    def time_field(identifier):
        if identifier.startswith("load_"):
            return "load_test.schedule.duration_seconds"
        if identifier.startswith("pack_"):
            return "request_pack.cases"
        check = {"warm": "serving", "context_first_batch": "context", "recovery_prime": "recovery",
                 "recovery_baseline": "recovery", "limits.stop_control": "cancellation",
                 "backend_cancellation": "cancellation", "cancellation_intentional": "cancellation"}.get(identifier, identifier)
        return {"capacity": "traffic.duration_seconds", "arrivals": "arrivals.requests",
                "sessions": "sessions", "consistency": "consistency", "tools": "tool_checks",
                "prefix": "prefix", "sessions_wait": "sessions.delay_seconds"}.get(check, "test_options." + check)

    def unknown(target, row, count, timeout, bound):
        item = {"id": row["id"], "title": row["title"], "target": target,
                "requests": count, "request_timeout_seconds": timeout, "timeout_bound_seconds": bound}
        unknowns.append(item)
        timeline.append({**item, "kind": "unknown", "field": time_field(row["id"]),
                         "low_seconds": 0, "high_seconds": bound})

    @lru_cache(maxsize=256)
    def batch_moments(distributions):
        # For a gather, P(max <= x) = product(F_i(x)). Integrate its
        # survival function for E[max] and E[max**2]. The quantile curve is
        # piecewise linear; midpoint quadrature avoids atoms at repeated values.
        points = sorted({0, *(x for distribution in distributions for x, _ in distribution)})
        grouped = Counter(distributions)

        def cdf(distribution, x):
            index = bisect_right(distribution, (x, math.inf))
            if not index:
                return 0
            if index == len(distribution):
                return 1
            left, right = distribution[index - 1], distribution[index]
            return left[1] + (right[1] - left[1]) * (x - left[0]) / (right[0] - left[0])

        mean = second = third = 0
        for left, right in zip(points, points[1:]):
            step = (right - left) / 16
            for index in range(16):
                x = left + (index + .5) * step
                survival = 1 - math.prod(cdf(distribution, x) ** count
                                        for distribution, count in grouped.items())
                mean += survival * step
                second += 2 * x * survival * step
                third += 3 * x * x * survival * step
        return mean, max(0, second - mean * mean), max(0, third - 3 * mean * second + 2 * mean ** 3)

    def prediction(mean, variance, third):
        z = NormalDist().inv_cdf(.995)
        uncertainty = z * math.sqrt(variance)
        # Finite small batches have a positively skewed completion time. The
        # first Cornish-Fisher correction retains the normal lower bound and
        # extends the upper tail using measured third moments, not fixed padding.
        tail = (z * z - 1) * third / (6 * variance) if variance else 0
        return max(0, mean - uncertainty), mean + uncertainty + tail

    def distribution(item):
        values = item.get("quantiles_ms")
        if not values:
            return ((item["p50_ms"], .5), (item["p90_ms"], 1))
        points = {index / (len(values) - 1): value for index, value in enumerate(values)}
        # Retain the tail detail between p95 and max in a compact 21-point curve.
        points[.99] = item.get("p99_ms", values[-1])
        return tuple((value, quantile) for quantile, value in sorted(points.items()))

    def finite(target, row, check, count, concurrency=1, specs=None, batches=None, batch_sizes=None, batch_groups=None, source=None):
        options = effective(check)
        configured_concurrency = concurrency
        concurrency = min(concurrency, shares[target])
        explicit_batches = batches is not None
        full, remainder = divmod(count, configured_concurrency)
        batches = (full * math.ceil(configured_concurrency / concurrency)
                   + math.ceil(remainder / concurrency)) if batches is None else batches
        specs = (specs or [])[:count]
        observed = measured(target, check, concurrency, specs, source)
        if observed:
            matches = observed["matches"]
            curves = [distribution(item) for item in matches]
            groups = [range(start, min(count, start + configured_concurrency))
                      for start in range(0, count, configured_concurrency)]
            waves = [list(group)[start:start + concurrency] for group in groups
                     for start in range(0, len(group), concurrency)]
            mean = variance = third = 0
            for batch in range(batches):
                if batch_groups is not None:
                    indices = batch_groups[batch]
                elif explicit_batches:
                    size = batch_sizes[batch] if batch_sizes is not None else min(concurrency, count)
                    indices = range(batch * concurrency, batch * concurrency + size)
                else:
                    indices = waves[batch]
                active = [curves[index % len(curves)] for index in indices]
                if active:
                    batch_mean, batch_variance, batch_third = batch_moments(tuple(active))
                    mean += batch_mean
                    variance += batch_variance
                    third += batch_third
            # Predict the sum of independent batch maxima, including uncertainty
            # in the mean learned from a finite training cohort. This is a 99%
            # prediction interval with a skewed upper tail, not a sum of batch tail bounds.
            minimum_n = min(item.get("effective_n", item["n"]) for item in matches)
            mean += batches * max(item.get("batch_overhead_ms", 0) for item in matches)
            variance += batches ** 2 * max(item.get("batch_overhead_variance_ms2", 0) for item in matches) / 1e6
            variance *= 1 + count / minimum_n
            third *= (1 + count / minimum_n) ** 1.5
            low, high = prediction(mean, variance, third)
            uncertainty = NormalDist().inv_cdf(.995) * math.sqrt(variance)
            item = {"id": row["id"], "title": row["title"], "target": target,
                    "kind": "finite", "requests": count, "concurrency": concurrency,
                    "configured_concurrency": configured_concurrency, "low_seconds": low,
                    "high_seconds": high, "fixed_seconds": 0,
                    "expected_seconds": mean, "prediction_variance": variance, "prediction_third": third,
                    "pooled": observed["run_count"] >= 3}
            components.append(item)
            timeline.append({**item, "field": time_field(row["id"])})
            # With n=150, the chance of missing an independent 2% slow tail is
            # .98**150 < 5%. Smaller pooled cohorts additionally require repeated-run stability.
            runs = observed["run_count"]
            observation_note(target, row, observed)
            pooled = runs >= 3 and minimum_n >= 15
            if observed["drift_detected"] and runs < 5 and minimum_n < 150:
                uncertain_observations.append(f"{target} · {_warning_title(row)}: a recent latency shift requires "
                                              "5 consistent runs or 150 comparable samples before medium confidence can resume.")
            if any("quantiles_ms" not in item for item in matches) or not (minimum_n >= 150 or pooled):
                uncertain_observations.append(f"{target} · {_warning_title(row)}: 150 comparable samples from one run, or at least 15 samples per setting across 3 consistent runs, are required; this component has {minimum_n:g} effective samples across {runs} runs.")
            elif any(item.get("modeled_timing_coverage", item.get("batch_timing_coverage", 0)) < .99 for item in matches):
                uncertain_observations.append(f"{target} · {row['title']}: recorded batch timings must account for at least 99% of traffic time; local scheduling overhead is unmodelled or timing coverage is unavailable.")
            elif not pooled and uncertainty > .4 * mean:
                uncertain_observations.append(f"{target} · {row['title']}: the prediction interval is too wide for medium confidence.")
        else:
            unknown(target, row, count, options.request_timeout_seconds, batches * options.request_timeout_seconds)

    def fixed(target, row, seconds, field, *, count=1, timeout=0, observed=None, last_gaps=None, minimum_drain=False):
        if not seconds and not timeout:
            return
        drain_high = count * timeout
        gaps = last_gaps or [0] * count
        if observed:
            observation_note(target, row, observed)
        latency = observed["min_ms"] if observed and minimum_drain else observed["p50_ms"] if observed else 0
        drain_low = sum(min(timeout, max(0, latency - gap)) for gap in gaps) if observed else 0
        item = {"id": row["id"], "title": row["title"], "target": target,
                "kind": "fixed", "fixed_seconds": seconds,
                "low_seconds": seconds + drain_low, "high_seconds": seconds + drain_high,
                "drain_uncertainty_seconds": max(0, drain_high - drain_low)}
        components.append(item)
        for index in range(count):
            timeline.append({**item, "title": row["title"] + (f" {index + 1}" if count > 1 else ""),
                             "field": field, "low_seconds": seconds / count + drain_low / count,
                             "high_seconds": seconds / count + timeout,
                             "coverage_high_seconds": seconds / count + (timeout if observed else 0),
                             "stage": index + 1 if count > 1 else None,
                             "wait": row["id"] in {"recovery_wait", "cancellation_wait", "sessions_wait"}})

    # Keep request-breakdown titles stable; duration prose uses readable names.
    time_titles = {"limits.stop_control": "Stop-sequence control", "recovery_baseline": "Recovery baseline",
                   "recovery_prime": "Recovery baseline warm-up"}
    ordered = [{**row, "title": time_titles.get(row["id"], CATALOGUE_NAMES.get(row["id"], row["title"]))} for row in rows]
    # Recovery's sustained window precedes its follow-up probe batch.
    if any(row["id"] == "sustained" for row in ordered):
        sustained = next(row for row in ordered if row["id"] == "sustained")
        ordered.remove(sustained)
        ordered.insert(next(index for index, row in enumerate(ordered) if row["id"] == "recovery"), sustained)
    calibration = next((row for row in ordered if row["id"] == "load_calibration"), None)
    if calibration:
        ordered.remove(calibration)
        ordered.insert(next(index for index, row in enumerate(ordered) if row["id"].startswith("load_")), calibration)
    if "capacity" not in selected and any(row["id"] == "context" for row in ordered):
        pack_rows = [row for row in ordered if row["id"] in {"pack_control", "pack_loaded"}]
        ordered = [row for row in ordered if row["id"] not in {"pack_control", "pack_loaded"}]
        after_context = next(index for index, row in enumerate(ordered) if row["id"] == "context") + 1
        ordered[after_context:after_context] = pack_rows
    for target in config.targets:
        has_observations = bool((observations or {}).get(target.name))
        for row in ordered:
            name, count = row["id"], row["requests_per_target"]
            if name == "recovery_prime" and "cancellation" in selected:
                fixed(target.name, {"id": "recovery_wait", "title": "Recovery cleanup wait"},
                      min(5, config.effective_check("cancellation")["cleanup_seconds"]), "test_options.cancellation.cleanup_seconds")
            if count == 0 and name != "capacity":
                continue
            check = {"initial": "serving", "warm": "serving", "pack_control": "correctness",
                     "pack_loaded": "correctness", "context_first_batch": "context",
                     "recovery_prime": "recovery", "recovery_baseline": "recovery",
                     "limits.stop_control": "cancellation", "backend_cancellation": "cancellation",
                     "sustained": "recovery"}.get(name, name)
            if name in {"initial", "restart_readiness"}:
                # Setup stays outside the headline. Initial inference still
                # consumes the shared deadline, so budget remedies reserve its
                # full effective timeout before the selected traffic starts.
                if name == "initial":
                    timeline.append({"id": name, "title": "Initial connection allowance", "target": target.name,
                                     "kind": "setup", "field": "test_options.access.request_timeout_seconds"
                                     if "access" in selected else "test_options.serving.request_timeout_seconds",
                                     "low_seconds": 0, "high_seconds": config.effective_initial()["request_timeout_seconds"],
                                     "wait": True})
                continue
            if name == "load_calibration":
                timeout = max(own.request_timeout_seconds for _, own in settings_sets(config.load_test))
                unknown(target.name, row, count, timeout, count * timeout)
                continue
            if name.startswith("load_"):
                points = [p for p in test_points(config.load_test) if p["type"] == name.removeprefix("load_")]
                own = dict(settings_sets(config.load_test))
                queries, completion_points = [], []
                for point in points:
                    if point["pattern"] != "concurrent":
                        continue
                    settings = own[point["source"]]
                    shapes = point_shapes(settings, point)
                    completion_points.append((point,len(shapes)))
                    for shape in shapes:
                        input_size, output_size = shape.split("_")
                        queries.append(dict(family=f"load.{point['type']}.{point['pattern']}",
                            concurrency=min(point["level"],shares[target.name]),stream=config.stream,
                            output_cap=getattr(settings.profile,output_size).output_tokens,
                            input_bucket=input_bucket(getattr(settings.profile,input_size).input_tokens*4)))
                observed = pooled_matches(target.name,queries,(observations or {}).get(target.name,[])) if queries else None
                if observed and all(item["p50_ms"] > 0 for item in observed["matches"]):
                    estimates, offset = [], 0
                    for point,size in completion_points:
                        p50 = max(item["p50_ms"] for item in observed["matches"][offset:offset+size])/1000
                        estimates.append(point["configured_window_seconds"]*min(point["level"],shares[target.name])/p50)
                        offset += size
                    detail = load_time_plan(config.load_test)["workloads"][name.removeprefix("load_")]
                    # Add known arrivals once; interference remains unknown.
                    duration_estimates.setdefault(name,{})[target.name] = round(sum(estimates)+detail["expected_requests"])
                    duration_runs.setdefault(name,{})[target.name] = observed["run_count"]
                    observation_note(target.name,row,observed)
                for point in points:
                    settings = own[point["source"]]
                    before = len(timeline)
                    fixed(target.name, row, point["windows"] * settings.schedule.duration_seconds,
                          "load_test.schedule.duration_seconds", count=point["windows"], timeout=settings.request_timeout_seconds)
                    for item in timeline[before:]:
                        # Outstanding requests can use their remaining request
                        # timeout after sending stops, even without timing samples.
                        item["coverage_high_seconds"] = item["high_seconds"]
                continue
            if name == "capacity":
                settings = config.effective_traffic()
                options = effective("capacity")
                from giraffe.fixtures import traffic_fixtures
                fixtures = traffic_fixtures(options, check_id="capacity") if has_observations else {}
                specs = [spec for group in fixtures.values() for spec in group]
                observed = measured(target.name, "capacity", min(settings.max_in_flight, shares[target.name]), specs)
                fixed(target.name, row, len(settings.rates) * settings.duration_seconds, "traffic.duration_seconds",
                      count=len(settings.rates), timeout=min(options.request_timeout_seconds, settings.drain_timeout_seconds or options.request_timeout_seconds), observed=observed,
                      last_gaps=[1 / rate for rate in settings.rates] if settings.arrival == "steady" else [math.inf] * len(settings.rates))
                continue
            if name == "sustained":
                options = config.effective_check("recovery")
                from giraffe.fixtures import traffic_fixtures
                heavy = traffic_fixtures(effective("recovery"), check_id="recovery") if has_observations else {}
                observed = measured(target.name, "recovery", min(options["concurrency"], shares[target.name]),
                                    heavy.get("long_input", []) + heavy.get("long_output", []))
                fixed(target.name, row, options["sustained_seconds"], "test_options.recovery.sustained_seconds",
                      timeout=options["request_timeout_seconds"], observed=observed)
                continue
            if name == "arrivals":
                settings = config.arrivals
                span = ((settings.requests - 1) / settings.requests_per_second if settings.mode == "steady"
                        else ((settings.requests - 1) // settings.burst_size) * settings.interval_seconds)
                concurrency = min(config.effective_arrivals().max_in_flight, shares[target.name])
                options = effective("arrivals")
                fixtures = fixtures_for("arrivals")["short"][:1] if has_observations else []
                observed = measured(target.name, "arrivals", concurrency, fixtures)
                fixed(target.name, row, span, "arrivals.requests_per_second" if settings.mode == "steady" else "arrivals.interval_seconds",
                      timeout=options.request_timeout_seconds, observed=observed,
                      last_gaps=[0], minimum_drain=True)
                # Pending arrivals survive the last scheduled offer. Unlike load
                # refills, this bounded queue can take several waves to drain.
                pending = min(settings.max_pending, max(0, settings.requests - concurrency))
                if pending:
                    queued = {"id": "arrivals_pending", "title": "Queued arrival drain"}
                    finite(target.name, queued, "arrivals", pending, concurrency, fixtures)
                    if not observed:
                        unknowns[-1]["upper_bound"] = True
                    elif settings.mode != "burst" or settings.requests > settings.burst_size:
                        components[-1]["low_seconds"] = 0
                        components[-1]["upper_bound"] = True
                continue
            if check not in CHECK_OPTION_FIELDS:
                continue
            options = effective(check)
            fixtures = fixtures_for(check) if has_observations else {}
            key = {"initial": "short", "warm": "short", "recovery_baseline": "sustained",
                   "recovery_prime": "sustained", "recovery": "sustained", "context_first_batch": "context",
                   "limits.stop_control": "limits"}.get(name, name)
            specs = fixtures.get(key, [])
            concurrency = config.effective_check(check).get("concurrency", 1) if check in {"correctness", "json"} else 1
            measurement_source = None
            if name == "context_first_batch":
                # Calibration and measured latency must share a reconstruction profile.
                specs = []
                entries = (observations or {}).get(target.name, [])
                for cohort in dict.fromkeys(entry.get("cohort") for entry in entries):
                    source = {target.name: [entry for entry in entries if entry.get("cohort") == cohort]}
                    candidates = calibrated_context_fixtures(source, target.name, config) or []
                    profile = next((entry.get("context_profile") for entry in source[target.name] if entry.get("family") == "context"), None)
                    compatible = {target.name: [entry for entry in entries if entry.get("context_profile") == profile]}
                    if measured(target.name, check, concurrency, candidates, compatible):
                        specs, measurement_source = candidates, compatible
                        break
            if name == "limits.stop_control":
                specs = [spec for spec in fixtures.get("limits", []) if spec.fixture_id.startswith("limits.stop_control")]
            if name == "cancellation":
                normal = [spec for spec in fixtures.get("limits", []) if spec.cancel_after_ms is None
                          and not spec.fixture_id.startswith("limits.stop_control")]
                normal_count = max(options.samples, len(normal) if has_observations else 2)
                finite(target.name, row, check, normal_count, specs=normal)
                short = fixtures.get("short", [])
                cancelled = sum(spec.cancel_after_ms is not None for spec in fixtures_for("cancellation")["limits"]) if has_observations else 1
                finite(target.name, {**row, "title": "Cancellation health probes"}, check,
                       options.samples + bool(cancelled), specs=short)
                # Intentional cancellation and the deadline self-test are
                # excluded from latency observations. Never infer them from a
                # completed output-control request just because its cap matches.
                unknown(target.name, {"id": "cancellation_intentional", "title": "Cancellation and deadline self-tests"},
                        cancelled + bool(cancelled), options.request_timeout_seconds,
                        (cancelled + bool(cancelled)) * options.request_timeout_seconds)
                continue
            if name == "pack_control" or name == "pack_loaded":
                from types import SimpleNamespace
                consumer = "capacity" if "capacity" in selected else "correctness"
                cap = config.effective_check(consumer)["max_output_tokens"]
                specs = [SimpleNamespace(stream=config.stream, max_tokens=case.max_tokens or cap,
                                         input_chars=sum(len(message.content) for message in case.messages))
                         for case in config.request_pack.cases] if has_observations and active_pack(config) else []
                concurrency = config.effective_check("correctness")["concurrency"] if name == "pack_loaded" else 1
            if name == "consistency":
                from giraffe.workloads import COPY_PASSAGES, consistency_output_cap, label_context, spec
                settings = config.consistency
                for shape in settings.shapes:
                    for level in dict.fromkeys([1, *settings.levels]):
                        concurrency = min(level, shares[target.name])
                        requests = (corpus_width(settings) * (settings.repetitions + settings.logprobs.priming_rounds)
                                    if settings.logprobs else settings.prompts_per_shape * sample_count(settings.repetitions, level))
                        specs = []
                        if settings.logprobs:
                            if has_observations:
                                from giraffe.logprob_workload import logprob_fixtures
                                request = logprob_fixtures(options, fixtures)[shape]
                                request.stream = False
                                specs = [request]
                            rounds = settings.repetitions + settings.logprobs.priming_rounds
                            finite(target.name, row, check, requests, concurrency, specs,
                                   batches=rounds * math.ceil(corpus_width(settings) / concurrency),
                                   batch_sizes=[min(concurrency, corpus_width(settings) - start)
                                                for _ in range(rounds)
                                                for start in range(0, corpus_width(settings), concurrency)])
                        else:
                            for index in range(settings.prompts_per_shape):
                                specs = []
                                if has_observations:
                                    answer = COPY_PASSAGES[index % len(COPY_PASSAGES)]
                                    if index >= len(COPY_PASSAGES):
                                        answer += f" The parcel tag reads R{index:04d}."
                                    context = (label_context(max(64, (options.context_limit - 256) * 2))
                                               + "\nThe background above is context only.\n" if shape == "long" else "")
                                    prompt = context + f"Copy the following text exactly, with no extra text:\n{answer}"
                                    specs = [spec(prompt, "consistency", "consistency", max_tokens=consistency_output_cap(options))]
                                finite(target.name, row, check, sample_count(settings.repetitions, level), concurrency, specs)
                continue
            if name == "sessions":
                from giraffe.workloads import session_prompt, spec
                settings = config.sessions
                turns = (max(len(workload.turns) for workload in settings.workloads) if settings.profile == "performance"
                         else len(settings.turns))
                modes = 1 if settings.profile == "performance" else len(settings.modes)
                concurrency = min(settings.sessions, settings.concurrency, shares[target.name])
                waves = math.ceil(settings.sessions / concurrency)
                timeline_start = len(timeline)
                count = count if count is not None else settings.sessions * modes * turns
                truncated = settings.profile != "performance" and settings.modes == ["fixed"] and count < settings.sessions * turns
                pauses = {"id": "sessions_wait", "title": "Conversation pauses"}
                pause_seconds = modes * waves * (turns - 1) * settings.delay_seconds
                if truncated and pause_seconds:
                    unknown(target.name, pauses, None, 0, pause_seconds)
                    unknowns[-1]["reason"] = f"Conversation history admission determines the pauses (up to {pause_seconds:g} s)."
                    timeline[-1]["wait"] = True
                else:
                    fixed(target.name, pauses, pause_seconds, "sessions.delay_seconds")
                specs = []
                if has_observations and settings.profile != "performance" and settings.modes == ["fixed"] and not truncated:
                    for index in range(settings.sessions):
                        history, value = [], f"Session_{index:04d}_fixed"
                        for turn, prompt in enumerate(settings.turns):
                            prompt = session_prompt(prompt, turn, value)
                            messages = [*history, {"role": "user", "content": prompt}]
                            chars = sum(len(message["content"]) for message in messages)
                            if chars + settings.max_tokens + 32 > options.context_limit:
                                break
                            request = spec(prompt, "sessions", "sessions", max_tokens=settings.max_tokens)
                            request.input_chars = chars
                            specs.append(request)
                            history = [*messages, {"role": "assistant", "content": value}]
                batch_count = modes * waves * turns
                batch_groups = [[session * turns + turn for session in range(start, min(settings.sessions, start + concurrency))]
                                for _ in range(modes) for start in range(0, settings.sessions, concurrency)
                                for turn in range(turns)]
                finite(target.name, row, check, count, concurrency, specs, batches=batch_count, batch_groups=batch_groups)
                # Session pauses occur between turns, never before the first
                # request. Retain aggregate components for the headline while
                # placing their batches/waits in runtime order for coverage.
                session_timeline = timeline[timeline_start:]
                del timeline[timeline_start:]
                requests = next(item for item in session_timeline if item["id"] == "sessions")
                pause = next((item for item in session_timeline if item["id"] == "sessions_wait"), None)
                for batch in range(batch_count):
                    timeline.append({**requests, "low_seconds": requests["low_seconds"] / batch_count,
                                     "high_seconds": (requests["high_seconds"] / batch_count
                                                      if requests["high_seconds"] is not None else None)})
                    if pause and (batch + 1) % turns:
                        timeline.append({**pause, "low_seconds": 0 if truncated else settings.delay_seconds,
                                         "high_seconds": settings.delay_seconds})
                continue
            if name == "prefix" and has_observations:
                from giraffe.workloads import spec
                settings = config.prefix
                prefix_chars = len(label_prompt(settings.prefix_chars + 33 + 80)) + len("\nTrial 0000.")
                specs = [spec("x" * prefix_chars, "prefix", "prefix", max_tokens=min(24, options.max_output_tokens))]
                history_chars = 0
                for _ in range(settings.history_turns):
                    history_chars += len(label_prompt(96))
                    if not prefix_admitted(history_chars, min(24, options.max_output_tokens), options.context_limit):
                        break
                    specs.append(spec("x" * history_chars, "prefix", "prefix", max_tokens=min(24, options.max_output_tokens)))
                    history_chars += 4
            if name == "tools" and has_observations and not (config.tool_checks and config.tool_checks.conversations):
                from giraffe.models import RequestSpec
                specs = [RequestSpec(fixture_id=fixture.fixture_id, scenario="tools", check_ids=["tools"],
                            messages=[message.model_dump() for message in fixture.messages],
                            input_chars=sum(len(message.content) for message in fixture.messages),
                            max_tokens=options.max_output_tokens, stream=mode == "stream")
                         for fixture in resolved_tool_fixtures(config) for mode in resolved_tool_modes(config)]
            if count is None:
                if name == "context":
                    specs = []
                    count = 1 + 2 * next(item["requests_per_target"] for item in rows if item["id"] == "context_first_batch")
                elif name == "backend_cancellation":
                    count = 3 * config.effective_check("cancellation")["samples"]
                else:
                    unknown(target.name, row, None, options.request_timeout_seconds, None)
                    continue
            finite(target.name, row, check, count, concurrency, specs, source=measurement_source)
            if name == "backend_cancellation":
                options = config.effective_check("cancellation")
                fixed(target.name, {"id": "cancellation_wait", "title": "Cancellation evidence waits"},
                      options["samples"] * options["trace_wait_seconds"], "test_options.cancellation.trace_wait_seconds")

    for row in rows:
        estimates = duration_estimates.get(row["id"], {})
        if len(estimates) == len(config.targets) and len(set(estimates.values())) == 1:
            row["estimated_requests_per_target"] = next(iter(estimates.values()))
            row["estimate_source"] = "from your recent runs" if max(duration_runs[row["id"]].values()) > 1 else "from your last run"

    def combine(key):
        totals = [sum(item.get(key, 0) for item in components if item["target"] == target.name) for target in config.targets]
        if not config.overlap_models:
            return sum(totals)
        combined = max(totals, default=0)
        if pool < requirements["peak"]:
            finite = sum(item.get(key, 0) for item in components if item["kind"] == "finite")
            if key == "low_seconds" and pool < len(config.targets):
                combined = max(combined, finite / pool)
            elif key == "high_seconds":
                other = max((sum(item.get(key, 0) for item in components
                             if item["target"] == target.name and item["kind"] != "finite")
                             for target in config.targets), default=0)
                combined = max(combined, other + finite)
        return combined

    low, high, fixed_seconds = combine("low_seconds"), combine("high_seconds"), combine("fixed_seconds")
    timeline_high = high
    # Independent sequential groups add variances, not interval half-widths.
    # Keep individual component highs in the budget timeline for a conservative
    # completion remedy. Shared-pool overlap retains its contention upper bound.
    if not config.overlap_models or pool >= requirements["peak"]:
        ranges = []
        for target in config.targets:
            items = [item for item in components if item["target"] == target.name]
            finite_items = [item for item in items if item["kind"] == "finite" and not item.get("upper_bound")]
            mean = sum(item["expected_seconds"] for item in finite_items)
            finite_low, finite_high = prediction(mean, sum(item["prediction_variance"] for item in finite_items),
                                                sum(item["prediction_third"] for item in finite_items))
            other = [item for item in items if item["kind"] != "finite" or item.get("upper_bound")]
            ranges.append((sum(item["low_seconds"] for item in other) + finite_low,
                           sum(item["high_seconds"] for item in other) + finite_high))
        low = max((item[0] for item in ranges), default=0) if config.overlap_models else sum(item[0] for item in ranges)
        high = max((item[1] for item in ranges), default=0) if config.overlap_models else sum(item[1] for item in ranges)
    midpoint = (low + high) / 2
    drain_fraction = combine("drain_uncertainty_seconds") / midpoint if midpoint else 0
    fixed_fraction = fixed_seconds / midpoint if midpoint else 0
    confidence = ("low" if unknowns or uncertain_observations or not components else
                  "high" if fixed_fraction >= .8 and drain_fraction <= .2 else "medium")
    if confidence == "medium" and high - low > midpoint:
        confidence = "low"
        uncertain_observations.append("The combined duration range is too wide for medium confidence.")
    setup_totals = [sum(item["high_seconds"] for item in timeline
                        if item["target"] == target.name and item["kind"] == "setup") for target in config.targets]
    setup_allowance = max(setup_totals, default=0) if config.overlap_models else sum(setup_totals)
    budget_high = timeline_high + setup_allowance
    errors, warnings = [], []
    budget = config.max_duration_seconds
    if budget is not None:
        cut, first_start = [], None
        risk_over_budget = False
        cursor_low = cursor_high = cursor_risk = 0
        for target in config.targets:
            if config.overlap_models:
                cursor_low = cursor_high = cursor_risk = 0
            for item in (item for item in timeline if item["target"] == target.name):
                if not item.get("wait"):
                    first_start = min(first_start, cursor_low) if first_start is not None else cursor_low
                estimated = item["low_seconds"] if item["kind"] in {"unknown", "setup"} else item.get("coverage_high_seconds", item["high_seconds"])
                end_high = cursor_high + estimated
                cursor_risk = (cursor_risk + item["high_seconds"]
                               if cursor_risk is not None and item["high_seconds"] is not None else None)
                risk_over_budget |= cursor_risk is None or cursor_risk > budget
                if not item.get("wait") and end_high > budget:
                    cut.append(item)
                cursor_low += item["low_seconds"]
                cursor_high = end_high
        if cut:
            fields = list(dict.fromkeys(["max_duration_seconds", *(item["field"] for item in cut)]))
            fully_known = not unknowns
            suggestion = max(1, math.ceil(budget_high))
            label = f"Use {suggestion:,} s" + ("" if fully_known else " (covers fixed windows only)")
            affected = list(dict.fromkeys(f"{item['target']} · {_warning_title(item)}" + (f" {item['stage']}" if item.get("stage") else "") for item in cut))
            edits = list({item["field"]: {"label": f"Edit {_warning_title(item)}", "field": item["field"]}
                          for item in cut}.values())
            notice = {"kind": "incomplete_coverage", "fields": fields,
                      "values": {"minimum_seconds": fixed_seconds, "max_duration_seconds": budget,
                                 "suggested_seconds": suggestion, "fully_known": fully_known,
                                 "setup_allowance_seconds": setup_allowance},
                      "cut_checks": affected,
                      "actions": ([{"label": label, "field": "max_duration_seconds", "value": suggestion}]
                                  if suggestion > budget else []) + edits,
                      "msg": f"Incomplete coverage: the traffic time limit (Maximum duration) may cut {', '.join(affected)}. " +
                             (f"The full traffic timeline including the initial connection allowance needs up to {budget_high:,g} seconds."
                              if fully_known else "Some request durations are unknown; the suggested limit covers fixed windows only.")}
            if first_start is not None and budget <= first_start:
                errors.append(notice | {"field": fields[0], "loc": ["body", fields[0]]})
            else:
                warnings.append(notice)
        if budget_high > budget or risk_over_budget:
            reasons = []
            if high > budget:
                reasons.append("The estimated traffic duration may exceed the time limit, leaving checks unfinished.")
            risk_components = [item for item in timeline if item["kind"] in {"setup", "unknown"}
                               and (item["high_seconds"] is None or item["high_seconds"] > 0)]
            for item in risk_components:
                if item["kind"] == "setup":
                    reasons.append(f"The first connection check for {item['target']} may wait up to {item['high_seconds']:,g} s "
                                   "if the server is slow to respond; this allowance shares the time limit and is excluded from the estimated duration.")
                elif item["high_seconds"] is not None:
                    reasons.append(f"{item['target']} · {_warning_title(item)} has no comparable timing measurements; "
                                   f"its requests may take up to {item['high_seconds']:,g} s in total "
                                   f"if each reaches its {item['request_timeout_seconds']:,g} s timeout.")
                else:
                    reasons.append(f"{item['target']} · {_warning_title(item)} has an unknown workload size and request speed; "
                                   "its completion time cannot be bounded.")
            if not reasons:
                reasons.append("The conservative completion allowance across all traffic components may exceed the time limit, "
                               "even though the combined estimated duration fits.")
            warnings.append({"kind": "estimate_risk",
                             "fields": list(dict.fromkeys(["max_duration_seconds", *(item["field"] for item in risk_components)])),
                             "components": risk_components, "msg": " ".join(dict.fromkeys(reasons))})
    reasons = [f"{item['target']} · {item['title']}: " + (item["reason"] if "reason" in item else
               (("up to " if item.get("upper_bound") else "") + f"{item['requests']:,} requests at unknown speed (up to {item['timeout_bound_seconds']:g} s if every request hits its {item['request_timeout_seconds']:g} s timeout)"
                if item["requests"] is not None else "workload size and request speed are unknown")) for item in unknowns]
    known = bool(components) or not unknowns
    return {"estimated_seconds": round(midpoint, 3) if known else None,
            "estimated_low_seconds": math.floor(math.nextafter(low * 1000, math.inf)) / 1000 if known else None,
            "estimated_high_seconds": math.ceil(math.nextafter(high * 1000, -math.inf)) / 1000 if known else None,
            "worst_case_seconds": round(high, 3) if known else None,
            "minimum_seconds": fixed_seconds, "time_components": components,
            "unknown_time_components": unknowns, "confidence": confidence,
            "confidence_details": {"fixed_fraction": fixed_fraction, "drain_uncertainty_fraction": drain_fraction,
                                   "insufficient_observations": list(dict.fromkeys(uncertain_observations))},
            "time_timeline": timeline, "budget_high_seconds": budget_high,
            "estimate_exclusions": ["setup/connection", "AI evaluation", "report generation"],
            "time_bound_reasons": reasons, "time_estimate_assumptions": ([
                "Finite batches use the expected maximum from comparable recorded distributions. A nominal 99% prediction interval combines batch variance and sample uncertainty across sequential groups, with a measured-skew correction on the upper tail. Medium confidence requires 150 samples from one run or at least 15 samples per setting across 3 consistent runs, at least 99% accounted timing coverage (including small measured inter-batch overhead only after 3 runs), no unknown components and a combined relative half-width at most 50% (single-run components also require a normal half-width at most 40%). Each run has equal total weight, so recent runs weigh no less than older runs; pooling stops at a detected latency shift."
            ] if any(item["kind"] == "finite" for item in components) else []) + list(dict.fromkeys(observation_notes)) +
                (["Confidence remains low because these components have unknown duration: " +
                  ", ".join(dict.fromkeys(item["title"] for item in unknowns)) + "."] if unknowns else []) +
                ([f"Request deadlines after sending stops contribute {combine('drain_uncertainty_seconds'):g} seconds of uncertainty; more latency samples cannot remove that configured bound."]
                 if confidence == "low" and drain_fraction > .2 else []),
            "errors": errors, "time_warnings": warnings}


def request_plan(config: RunConfig | ConfigDraft, *,
                 pack_details: dict[str, Any] | None = None, observations=None) -> dict[str, Any]:
    """Count complete fixed work before hard caps; leave adaptive totals unknown.

    Rows account for shared controls only once. ``known`` includes only exact
    fixed portions, and evaluator calls share the dataset preview's count rules.
    The optional preview avoids rebuilding request-pack schedules in the API.
    """
    selected = set(config.checks)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    target_count = len(config.targets)
    if config.max_requests is None:
        quotas = {target.name: None for target in config.targets}
    else:
        base, extra = divmod(config.max_requests, target_count)
        quotas = {target.name: base + (index < extra)
                  for index, target in enumerate(config.targets)}
    concurrency_warnings = config.concurrency_notices()
    capacity_warnings = []
    if "capacity" in selected:
        traffic = config.effective_traffic()
        if traffic.expected_response_seconds is not None:
            rate = max(traffic.rates)
            unfinished = rate * traffic.expected_response_seconds
            if unfinished > traffic.max_in_flight:
                unfinished_label = (f"{unfinished:,g}" if math.isfinite(unfinished)
                    else f"{Decimal(str(rate)) * Decimal(str(traffic.expected_response_seconds)):,g}")
                message = (f"Capacity risk estimate: ≈ {rate:,g} requests/second × {traffic.expected_response_seconds:,g} seconds "
                    f"= {unfinished_label} unfinished requests needed; in-flight limit {traffic.max_in_flight:,} "
                    "→ the rest are dropped locally. This uses your expected response time, not measured capacity.")
                capacity_warnings.append({"kind": "capacity_risk_estimate", "estimate": True,
                    "fields": ["traffic.rates", "traffic.expected_response_seconds", "traffic.max_in_flight"],
                    "values": {"traffic.rates": traffic.rates,
                        "traffic.expected_response_seconds": traffic.expected_response_seconds,
                        "traffic.max_in_flight": traffic.max_in_flight,
                        "unfinished_requests": unfinished if math.isfinite(unfinished) else unfinished_label},
                    "msg": message,
                    "actions": [{"label": "Edit sending rate", "field": "traffic.rates"},
                                {"label": "Edit expected response time", "field": "traffic.expected_response_seconds"},
                                {"label": "Edit in-flight limit", "field": "traffic.max_in_flight"}]})
    warnings.extend(notice["msg"] for notice in capacity_warnings)
    if "arrivals" in selected and config.arrivals.mode == "burst":
        arrivals = config.effective_arrivals()
        in_flight = arrivals.max_in_flight
        contention = None
        if config.overlap_models and config.concurrency is not None and target_count > 1:
            required = required_concurrency(config)
            pool = min(required["peak"], config.concurrency)
            occupied = min(pool, required["per_target_peak"] * (target_count - 1))
            in_flight = min(in_flight, max(0, pool - occupied))
            if in_flight < arrivals.max_in_flight:
                contention = (pool, occupied)
        capacity = in_flight + arrivals.max_pending
        if arrivals.burst_size > capacity:
            dropped = arrivals.burst_size - capacity
            warning = {
                "fields": ["arrivals.burst_size", "arrivals.max_in_flight", "arrivals.max_pending"],
                "values": {"arrivals.burst_size": arrivals.burst_size,
                           "arrivals.max_in_flight": arrivals.max_in_flight,
                           "arrivals.max_pending": arrivals.max_pending, "dropped": dropped},
                "msg": f"Burst size {arrivals.burst_size:,} exceeds {in_flight} in-flight + {arrivals.max_pending:,} pending = {capacity}; {dropped:,} excess requests will be dropped locally."}
            if contention:
                pool, occupied = contention
                warning["fields"].append("concurrency")
                warning["values"].update({"concurrency": config.concurrency, "shared_pool": pool,
                    "other_endpoint_slots": occupied, "available_in_flight": in_flight})
                warning["msg"] = (f"Arrivals share {pool} slots across {target_count} endpoints. Other endpoints can occupy {occupied:,}; "
                    f"in that case {in_flight} in-flight + {arrivals.max_pending:,} pending = {capacity}, "
                    f"so up to {dropped:,} of the {arrivals.burst_size:,} burst requests may be dropped locally.")
            concurrency_warnings.append(warning)
    warnings.extend(notice["msg"] for notice in concurrency_warnings)

    def add(identifier: str, count: int | None, summary: str, *,
            source: str = "generated examples", kind: str = "exact", title: str | None = None):
        rows.append({"id": identifier, "title": title or CHECK_NAMES.get(identifier, identifier),
                     "requests_per_target": count, "kind": kind, "source": source,
                     "summary": summary})

    # Only fixture cardinalities are needed. Keep context filler small even if
    # the operator has configured a very large context limit.
    fixtures = builtin_fixtures(config.model_copy(update={"context_limit": 128}))
    initial = bool(selected & {"access", "serving", "first_output", "generation"})
    if initial:
        add("initial", 1, "One generated request checks the connection. Time to first text uses this same request.",
            title="Connection check")
    if config.restart_target:
        add("restart_readiness", None,
            f"GET readiness polls for {config.restart_target}, then one inference probe outside the request budget.",
            kind="bounded", title="Restart readiness")
    warm_count = config.effective_response()["samples"] if selected & {"serving", "first_output"} else 0
    if warm_count:
        add("warm", warm_count, "Repeated connection requests reused to compare response timing and mixed-length requests.",
            title="Repeated connection checks")
    for check in ("access", "serving", "first_output"):
        if check in selected:
            add(check, 0, "Reuses connection checks and the other selected tests; adds no requests.")

    if "generation" in selected:
        count = config.effective_check("generation")["samples"]
        add("generation", count, f"{count} copies of the generated passage.")

    pack_selected = config.request_source == "request_pack" and bool(selected & {"load", "capacity", "correctness"})
    pack = active_pack(config)
    details = pack_details if pack_details is not None else pack_summary(config) if pack else None
    if pack_selected and "correctness" in selected:
        count = len(config.request_pack.cases) if pack else None
        add("pack_control", count, "Sends each of your questions once. The answer and arrival checks reuse these results."
            if pack else "Add your questions to calculate their request count.",
            source="your dataset", kind="exact" if pack else "bounded", title="Your questions (one each)")
        if not pack:
            warnings.append("The selected dataset is empty; request counts and run readiness are incomplete.")

    if pack_selected and "correctness" in selected and not selected & {"load", "capacity"} and pack:
        count = max(len(config.request_pack.cases), config.effective_check("correctness")["concurrency"])
        add("pack_loaded", count, "Concurrent answer pass over your questions.", source="your dataset")

    if "capacity" in selected:
        traffic = config.effective_traffic()
        limits = config.effective_check("capacity")["limits"]
        if limits["first_output_ms"] is None and limits["latency_ms"] is None:
            warnings.append("Set a maximum answer time or time to first text for a pass/fail result under load. Without a time target, Giraffe reports measurements only.")
        if pack_selected and not pack:
            add("capacity", None, "Add your questions to calculate the arrival request count.",
                source="your dataset", kind="bounded")
        else:
            if pack:
                count = sum(stage["planned"] for stage in details["stages"])
                kind = "exact"
            elif traffic.arrival == "steady":
                count = sum(_steady_count(rate, traffic.duration_seconds) for rate in traffic.rates)
                kind = "exact"
            else:
                count = sum(min(round(rate * traffic.duration_seconds), MAX_PLANNED_ARRIVALS)
                            for rate in traffic.rates)
                kind = "exact"
            add("capacity", count,
                f"Starts {count} requests across {len(traffic.rates)} rates. "
                + "Scheduled requests use the run limit even when Giraffe cannot send them because all request slots are busy.",
                source="your dataset" if pack else "generated examples", kind=kind)

    if "context" in selected:
        context_options = config.effective_check("context")
        context_shapes = builtin_fixtures(config.model_copy(update={**{key: value for key, value in context_options.items() if key != "limits"}, "context_limit": min(512, context_options["context_limit"])}))["context"]
        count = max(context_options["samples"], len(context_shapes))
        add("context_first_batch", count, "The first generated context batch.", title="Initial context cases")
        add("context", 0,
            "Prompts are sized locally with the selected tokenizer; Automatic resolves the model name with a built-in fallback. Server usage determines actual input-size coverage.",
            kind="exact")

    if "correctness" in selected:
        if pack_selected:
            add("correctness", 0, "Uses each question's first result; adds no requests when arrival traffic is also selected.",
                source="your dataset")
        elif config.custom_fixtures:
            add("correctness", None,
                "Built-in questions plus a local custom-fixture file; its case count is unknown without importing the file.",
                source="generated examples and your local fixture file", kind="bounded")
        else:
            count = max(config.effective_check("correctness")["samples"], len(fixtures["correctness"]))
            add("correctness", count, f"{count} requests using {len(fixtures['correctness'])} generated questions with known answers.")
    if "json" in selected:
        count = max(config.effective_check("json")["samples"], len(fixtures["json"]))
        add("json", count, f"{count} generated structured-answer requests.")

    if "cancellation" in selected:
        options = config.effective_check("cancellation")
        normal = sum(spec.cancel_after_ms is None and not spec.fixture_id.startswith("limits.stop_control")
                     for spec in fixtures["limits"])
        cancelled = sum(spec.cancel_after_ms is not None for spec in fixtures["limits"])
        add("limits.stop_control", 1, "One full-output control for stop-sequence enforcement.")
        count = max(options["samples"], normal) + cancelled + 2 * bool(cancelled) + options["samples"]
        add("cancellation", count, "Output/stop controls, a pre-cancellation baseline, a client cancellation, a client deadline self-test and follow-up health requests.")
        if options["backend_verification"] == "required":
            add("backend_cancellation", None,
                f"Runtime and local evidence availability determine whether up to {3 * options['samples']} additional verification requests run.",
                kind="bounded", title="Backend cancellation verification")
    if "recovery" in selected:
        options = config.effective_check("recovery")
        later = [name for name in config.checks if name not in CORE_CHECKS or name == "load"]
        later_known = request_plan(config.model_copy(update={"checks": later, "restart_target": None}))["minimum_reserved_per_target"]
        prior = sum(row["requests_per_target"] or 0 for row in rows)
        needed = later_known + 2 * options["samples"] + max(options["limits"]["min_samples"], options["concurrency"] * 2)
        eligible = [quota is None or quota - prior > needed for quota in quotas.values()]
        uncertain = config.max_requests is not None and (any(row["kind"] != "exact" for row in rows) or len(set(eligible)) > 1)
        add("recovery_prime", int(any(eligible)), "One baseline warm-up when remaining target/run budget exceeds later fixed work, both probe batches and the minimum load batch.",
            kind="bounded" if any(eligible) and uncertain else "exact", title="Recovery baseline warm-up")
        add("recovery_baseline", options["samples"], "Matched quiet controls before sustained load.")
        add("recovery", options["samples"], "Fixed follow-up health requests after sustained load.")
        if options["sustained_seconds"] > 0:
            add("sustained", None,
                f"Response pace determines the number of load batches during {options['sustained_seconds']:g} seconds.",
                kind="bounded", title="Sustained load")
    if "gpu" in selected:
        add("gpu", 0, "Observes metrics without adding inference requests.", source="configured metrics endpoint")

    if "arrivals" in selected:
        add("arrivals", config.arrivals.requests,
            f"{config.arrivals.requests} scheduled offers; the pending queue and run limits can leave some undispatched.")
    if "prefix" in selected:
        settings = config.prefix
        context_limit = config.effective_check("prefix")["context_limit"]
        output_cap = min(24, config.max_output_tokens)
        # Include the 32-character nonce, newline, filler/instruction and trial suffix.
        prefix_chars = len(label_prompt(settings.prefix_chars + 33 + 80)) + len("\nTrial 0000.")
        if not prefix_admitted(prefix_chars, output_cap, context_limit):
            add("prefix", 0, "Estimated prefix input plus output/template reserves exceeds context_limit; no prefix requests are admitted.")
            warnings.append("Prefix coverage will be unfinished: estimated token admission rejects the configured prefix.")
        else:
            prompt_chars = len(label_prompt(96))
            history = sum(prefix_admitted(prompt_chars * (turn + 1) + 4 * turn, output_cap, context_limit)
                          for turn in range(settings.history_turns))
            add("prefix", 2 * settings.repeats + history,
                f"{2 * settings.repeats} shared/independent prefix controls and {history} fixed-history requests admitted by the token estimate.")

    bucket_cases, bucket_levels = [], []
    load_plan = load_time_plan(config.load_test) if "load" in selected else {}
    load_warnings = []
    if load_plan:
        load_plan["saved_shape_warnings"] = saved_shape_warnings(config)
        load_warnings.extend({"fields": [], "msg": warning}
                             for warning in load_plan["saved_shape_warnings"])
        for kind, detail in load_plan["workloads"].items():
            count = None if detail["unknown_requests"] else detail["expected_requests"]
            add(f"load_{kind}", count, f"{detail['test_points']} tests, {detail['windows']} sending windows; "
                f"{detail['configured_load_seconds']:g} s sending plus up to {detail['finish_allowance_seconds']:g} s finishing. "
                + ("Completion-driven request count is unknown." if detail["unknown_requests"] else
                   "Poisson requests are approximate." if detail["approximate"] else "Scheduled arrivals are exact."),
                title=detail["title"], kind="duration" if detail["unknown_requests"] else "estimate" if detail["approximate"] else "exact",
                source="your dataset" if config.request_source == "request_pack" else "saved questions" if config.custom_fixtures else "generated token-target prompts")
            rows[-1]["expected_scheduled_requests"] = detail["expected_requests"]
            rows[-1]["known_scheduled_requests"] = detail["known_scheduled_requests"]
            rows[-1]["approximate"] = detail["approximate"]
        if config.request_source != "request_pack" and not config.custom_fixtures:
            add("load_calibration", 0, "Prompts use the selected local tokenizer; Automatic resolves the model name with a built-in fallback. Preparation sends no inference requests.", title="Local prompt preparation", kind="exact")
            for source, own in settings_sets(config.load_test):
                floors = {}
                for size, variant in calibration_keys(own):
                    floors[size] = max(floors.get(size, 0), prompt_overhead(variant))
                prefix = ("load_test" if source == "primary" else
                          f"load_test.migrated_tests.{source.split('_')[1]}.settings")
                for size, floor in floors.items():
                    target = getattr(own.profile, size).input_tokens
                    if target < floor:
                        load_warnings.append({"fields": [f"{prefix}.profile.{size}.input_tokens"],
                            "msg": f"{size.capitalize()} input target {target} is below the minimum prompt length "
                                   f"(≈{floor} tokens); local tokenization reports the actual input length."})
        criteria = config.load_test.pass_criteria
        if criteria.max_first_answer_ms is None and criteria.max_completion_ms is None:
            load_warnings.append({"fields":["load_test.pass_criteria.max_first_answer_ms","load_test.pass_criteria.max_completion_ms"],
                "msg":"Measure only: a first-answer or completion limit is required to establish highest tested accepted capacity."})
        if any(context_window(t)[0] is None for t in config.targets):
            load_warnings.append({"fields":["load_test.profile.long.input_tokens"],
                "msg":"Endpoint context window unknown. Explicit token targets are not sized by the client Context length; server context errors may require smaller targets."})
        if config.max_requests is not None and load_plan["expected_scheduled_requests"]*target_count > config.max_requests:
            load_warnings.append({"fields":["max_requests","load_test.schedule.duration_seconds","load_test.patterns.rates"],
                "msg":"Expected scheduled Load test requests exceed the total request budget; some tests will be truncated.",
                "values":{"expected_scheduled_requests":load_plan["expected_scheduled_requests"]*target_count,"max_requests":config.max_requests}})
        warnings.extend(w["msg"] for w in load_warnings)
    if "sessions" in selected:
        settings = config.sessions
        if settings.profile == "performance":
            maximum = settings.sessions * max(len(workload.turns) for workload in settings.workloads)
            add("sessions", None,
                f"Up to {maximum} requests; prior responses and history admission determine whether later turns run.",
                source="configured performance conversations and live history", kind="bounded")
        else:
            fixed = sum(fixed_session_turns(settings, index, config.effective_check("sessions")["context_limit"])
                        for index in range(settings.sessions)) if "fixed" in settings.modes else 0
            if "live" in settings.modes:
                add("sessions", None,
                    f"{fixed} fixed-history requests and up to {settings.sessions * len(settings.turns)} live requests; actual final-answer lengths determine live history admission.",
                    source="configured session prompts and saved/live history", kind="bounded")
            else:
                add("sessions", fixed,
                    "Fixed seeded histories with exact character admission; failed responses or shared budgets can stop sessions early.",
                    source="configured session prompts and seeded fixed history")
    if "consistency" in selected:
        settings = config.consistency
        levels = len(set([1, *settings.levels]))
        count = len(settings.shapes) * settings.prompts_per_shape * sum(
            sample_count(settings.repetitions, level) for level in set([1, *settings.levels]))
        if settings.logprobs:
            count = len(settings.shapes) * levels * corpus_width(settings) * (settings.repetitions + settings.logprobs.priming_rounds)
            add("consistency", count, f"{settings.repetitions} measured rounds plus {settings.logprobs.priming_rounds} priming rounds per shape and level; {corpus_width(settings)} fixed corpus requests per round. Complete responses with logprobs enabled; client overlap does not prove server batching.")
        else:
            add("consistency", count, f"At least {settings.repetitions} repeats of {settings.prompts_per_shape} distinct prompts per shape and level, rounded up to full load groups, including sequential controls once.")
    if "tools" in selected:
        fixtures = resolved_tool_fixtures(config)
        count = (len(fixtures) + sum(fixture.choice is None for fixture in fixtures)
                 * config.forced_tool_diagnostic) * len(resolved_tool_modes(config))
        conversations = config.tool_checks.conversations if config.tool_checks else []
        count += sum(len(fixture.steps) + 1 for fixture in conversations) * len(resolved_tool_modes(config))
        add("tools", count, "Tool fixtures in each selected mode. Conversation counts are upper bounds: expected calls plus one final answer; invalid or repeated calls stop a conversation early. No tools execute.",
            source="configured tool fixtures" if config.tool_checks and (config.tool_checks.fixtures or conversations) else "generated examples",
            kind="bounded" if conversations else "exact")

    sample_counts = {}
    if "arrivals" in selected:
        sample_counts["arrivals.requests"] = config.arrivals.requests
    if "prefix" in selected:
        sample_counts["prefix.repeats (excluding first use)"] = config.prefix.repeats - 1
    if "consistency" in selected:
        if config.consistency.logprobs:
            sample_counts["consistency.logprob measured samples"] = config.consistency.repetitions * corpus_width(config.consistency)
        else:
            sample_counts["consistency.repetitions"] = config.consistency.repetitions
    if "sessions" in selected:
        sample_counts["sessions.sessions"] = config.sessions.sessions
    for name, count in sample_counts.items():
        check = name.split(".")[0]
        minimum = config.effective_check(check)["limits"]["min_samples"]
        if count < minimum:
            warnings.append(f"{CATALOGUE_NAMES[check]}: {name}={count:,} is below min_samples={minimum:,}; this check cannot qualify.")

    judging = details["judging"] if details else None
    evaluator = {"active": bool(judging and judging["active"]),
                 "call_upper_bound": judging["planned_call_upper_bound"] if judging else 0,
                 "judge_count": judging["judge_count"] if judging else 0,
                 "ready": judging["ready"] if judging else True,
                 "readiness_errors": judging["readiness_errors"] if judging else []}
    if evaluator["active"]:
        warnings.append("AI judge calls run separately from the target request budget and send cases and generated answers to each judge.")
        warnings.extend(evaluator["readiness_errors"])
    if config.request_source == "request_pack" and not pack_selected:
        warnings.append("Your dataset is inactive: select Answer correctness or Capacity under load to use it.")
    if config.overlap_models and target_count > 1:
        warnings.append("Models run at the same time and share Simultaneous limit. Their actual overlap depends on when request slots become free.")
    known = sum(row.get("known_scheduled_requests", row["requests_per_target"] or 0
                        if row["kind"] == "exact" else 0) for row in rows)
    unknown = any(row["requests_per_target"] is None for row in rows)
    estimated = any(row["kind"] == "estimate" for row in rows)
    kind = "bounded" if unknown or any(row["kind"] == "bounded" for row in rows) else "estimate" if estimated else "exact"
    planned = None if unknown else sum(row["requests_per_target"] for row in rows)
    counted = sum(row["requests_per_target"] if row["requests_per_target"] is not None
                  else row.get("expected_scheduled_requests", 0) for row in rows)
    adaptive_estimates = {}
    if config.max_requests is not None and "recovery" in selected and config.effective_check("recovery")["sustained_seconds"] > 0:
        adaptive_estimates["sustained"] = max(config.effective_check("recovery")["limits"]["min_samples"],
            config.effective_check("recovery")["concurrency"] * 2)
    estimated_total = counted + sum(adaptive_estimates.values())
    # A workload row may contain both unknown refills and a known arrival
    # schedule. Preserve the additive schedule in the run headline as well as
    # in the drawer; Poisson expectations remain explicitly approximate.
    scheduled_extra = sum(row.get("expected_scheduled_requests", 0)
                          - row.get("known_scheduled_requests", 0) for row in rows)
    headline_count = (known + scheduled_extra) * target_count
    request_count_label = ("Unknown + " if unknown and headline_count else "Unknown" if unknown else "")
    if headline_count or not unknown:
        request_count_label += f"{'≈ ' if scheduled_extra or estimated else ''}{headline_count:,.0f}"
    def edit_field(row):
        identifier = row["id"]
        if identifier.startswith("load_"):
            return "load_test.schedule.duration_seconds"
        if identifier.startswith("pack_"):
            return "request_pack.cases"
        if identifier in {"arrivals", "prefix", "sessions", "consistency"}:
            return identifier
        if identifier == "tools":
            return "tool_checks"
        if identifier == "capacity":
            return "traffic.rates"
        if identifier in {"initial", "warm"}:
            return "test_options.serving"
        if identifier in {"recovery_prime", "recovery_baseline", "recovery", "sustained"}:
            return "test_options.recovery"
        if identifier in {"limits.stop_control", "cancellation", "backend_cancellation"}:
            return "test_options.cancellation"
        return "test_options." + ("context" if identifier == "context_first_batch" else identifier)

    request_warnings = []
    for target, quota in quotas.items():
        if quota is None:
            continue
        if known > quota or planned is not None and planned > quota:
            elapsed, cut = 0, []
            for row in rows:
                row_count = row["requests_per_target"] or row.get("known_scheduled_requests", 0)
                elapsed += row_count
                if elapsed > quota and row_count:
                    cut.append(row)
            required = planned * target_count if kind == "exact" else known * target_count
            if load_plan:
                # Calibration has a finite attempt allowance. A suggested
                # budget must cover that allowance as well as scheduled work.
                required = math.ceil(planned if planned is not None else known + sum(
                    row["requests_per_target"] or 0 for row in rows
                    if row["id"] == "load_calibration")) * target_count
            fields = list(dict.fromkeys(["max_requests", *[edit_field(row) for row in cut]]))
            titles = list(dict.fromkeys(_warning_title(row) for row in cut))
            edits = list({(action["label"], action["field"]): action for action in [
                {"label": f"Edit {_warning_title(row)}", "field": edit_field(row)} for row in cut]}.values())
            message = (f"{target}: incomplete coverage for {', '.join(titles)}; "
                       f"the planned requests exceed this endpoint's share of the request limit (Maximum requests: {quota:,}).")
            request_warnings.append({"kind": "incomplete_coverage", "fields": fields,
                "values": {"max_requests": config.max_requests, "required_request_limit": required, "quota": quota},
                "cut_checks": [row["id"] for row in cut], "msg": message,
                "actions": [{"label": f"Use {required:,} total requests", "field": "max_requests", "value": required},
                            *edits]})
            warnings.append(message)
        if unknown or kind != "exact":
            message = f"{target}: duration-based or adaptive traffic can exhaust the request budget and leave checks unfinished."
            request_warnings.append({"kind": "adaptive_risk", "fields": ["max_requests"], "msg": message})
            warnings.append(message)
    # One correction for the shared run budget, with each endpoint's cut work
    # retained as explanation data for both the sidebar and Review.
    grouped = []
    for warning_kind in ("incomplete_coverage", "adaptive_risk"):
        notices = [item for item in request_warnings if item["kind"] == warning_kind]
        if not notices:
            continue
        if len(notices) == 1:
            grouped.append(notices[0])
            continue
        combined = {**notices[0], "msg": " ".join(item["msg"] for item in notices),
                    "fields": list(dict.fromkeys(field for item in notices for field in item["fields"]))}
        if warning_kind == "incomplete_coverage":
            combined["cut_checks"] = list(dict.fromkeys(check for item in notices for check in item["cut_checks"]))
            affected_targets = [(target, quota) for target, quota in quotas.items()
                                if quota is not None and (known > quota or planned is not None and planned > quota)]
            combined["targets"] = [{"target": target, "quota": quota, "cut_checks": item["cut_checks"]}
                                   for (target, quota), item in zip(affected_targets, notices)]
            combined["values"] = {**combined["values"], "quotas": quotas}
            combined["actions"] = list({(action.get("field"), action.get("value")): action
                                        for item in notices for action in item["actions"]}.values())
        grouped.append(combined)
        warnings = [item for item in warnings if item not in {notice["msg"] for notice in notices}]
        warnings.append(combined["msg"])
    request_warnings = grouped
    timing = _time_plan(config, rows, observations)
    warnings.extend(notice["msg"] for notice in timing["time_warnings"] if notice["msg"] not in warnings)
    for notice in [*concurrency_warnings, *load_warnings, *capacity_warnings,
                   *request_warnings, *timing["time_warnings"]]:
        fields = notice.get("fields", [])
        if fields:
            notice.setdefault("actions", [{"label": "Edit settings", "fields": fields}])
            if "values" not in notice:
                values = {}
                for field in fields:
                    setting = config
                    for part in field.split("."):
                        setting = getattr(setting, part, None)
                    if type(setting) in {int, bool} or type(setting) is float and math.isfinite(setting):
                        values[field] = setting
                    elif isinstance(setting, list) and all(type(item) in {int, float} and math.isfinite(item) for item in setting):
                        values[field] = setting
                notice["values"] = values
    return {**timing, "count_kind": kind, "planned_per_target": planned,
            **({"request_count_label": request_count_label} if load_plan else {}),
            "planned_total": planned * target_count if planned is not None else None,
            "conservative_requests_per_target": estimated_total, "adaptive_estimates": adaptive_estimates,
            "load_test": load_plan,
            "request_settings": {check: (config.effective_check(check) | {
                                         field: config.effective_response()[field]
                                         for field in CHECK_OPTION_FIELDS[check]}
                                         if check in {"serving", "first_output"}
                                         else pack_settings(config) if check == "correctness" and pack_selected
                                         else config.effective_check(check)) for check in config.checks},
            "initial_request_settings": config.effective_initial(),
            "baseline_comparison": {"regression_percent": config.limits.regression_percent,
                                    "field": "limits.regression_percent"},
            "configured_load_seconds": load_plan.get("configured_load_seconds", 0),
            "load_warnings": load_warnings, "concurrency_warnings": concurrency_warnings,
            "capacity_warnings": capacity_warnings,
            "required_concurrency": required_concurrency(config),
            "minimum_reserved_per_target": known + (minimum_reservation(config.load_test) if load_plan else 0),
            "known_per_target": known, "known_total": known * target_count,
            "request_warnings": request_warnings, "request_limit": config.max_requests, "memory_limit_bytes": config.max_memory_mb * 1024**2,
            "memory_limit_label": "Maximum memory (RAM growth during this run)",
            "required_request_limit": planned * target_count if kind == "exact" else None,
            "target_quotas": quotas, "rows": rows, "bucket_cases": bucket_cases,
            "bucket_levels": bucket_levels, "evaluator": evaluator, "warnings": warnings}
