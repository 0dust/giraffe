"""Opt-in workloads on the suite's shared budget. No tools or server commands execute."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter

from giraffe.models import CHECK_NAMES, CORE_CHECKS, CheckResult, RequestSpec


def spec(prompt, scenario, check, *, expected=None, max_tokens=24, options=None, **workload):
    return RequestSpec(
        fixture_id=f"{scenario}.v1",
        scenario=scenario,
        check_ids=[check],
        messages=[{"role": "user", "content": prompt}],
        scorer="exact" if expected is not None else "none",
        expected=expected,
        input_chars=len(prompt),
        max_tokens=max_tokens,
        options=options or {},
        workload=workload,
    )


def label_prompt(chars, prefix=""):
    final = "\nThe box label is K7P4. Reply with the box label only."
    filler = (" The box is gray." * (chars // 17 + 1))[: max(0, chars - len(prefix + final))]
    return prefix + filler + final


async def arrivals(run, target, client, fixtures):
    settings = run.config.arrivals
    base = fixtures["short"][0].model_copy(deep=True)
    base.scenario, base.check_ids = "arrival", ["arrivals"]
    observation = {
        "planned": settings.requests,
        "issued": 0,
        "unissued": [],
        "clock": "milliseconds since run start; schedule independent of concurrency",
        "mode": settings.mode,
        "seed": settings.seed,
        "max_pending": settings.max_pending,
    }
    run.observations[target.name]["arrivals"] = observation
    start = time.monotonic()
    pending = set()
    try:
        for index in range(settings.requests):
            offset = (
                index / settings.requests_per_second
                if settings.mode == "steady"
                else (index // settings.burst_size) * settings.interval_seconds
            )
            due = start + offset
            if not run.allowed(target.name):
                observation["unissued"].append(
                    {
                        "first_index": index,
                        "count": settings.requests - index,
                        "reason": run.reason or "target budget reached",
                    }
                )
                break
            await asyncio.sleep(
                max(0, min(due - time.monotonic(), run.deadline - time.monotonic()))
            )
            pending = {task for task in pending if not task.done()}
            if not run.allowed(target.name):
                observation["unissued"].append(
                    {
                        "first_index": index,
                        "count": settings.requests - index,
                        "reason": run.reason or "target budget reached",
                    }
                )
                break
            if len(pending) >= settings.max_pending:
                observation["unissued"].append(
                    {
                        "index": index,
                        "scheduled_ms": (due - run.start) * 1000,
                        "reason": "bounded pending queue full",
                    }
                )
                continue
            request = base.model_copy(
                update={
                    "workload": {
                        "arrival_index": index,
                        "scheduled_ms": (due - run.start) * 1000,
                        "seed": settings.seed,
                    }
                }
            )
            pending.add(asyncio.create_task(run.request(target, client, request)))
        if pending:
            await asyncio.gather(*pending)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        records = [r for r in run.records if r.target == target.name and r.scenario == "arrival"]
        observation["issued"] = len(records)
        missing = (
            settings.requests
            - len(records)
            - sum(row.get("count", 1) for row in observation["unissued"])
        )
        if missing > 0:
            observation["unissued"].append(
                {"count": missing, "reason": "queued request not dispatched before stop/budget"}
            )
        observation["delayed"] = sum((r.client_wait_ms or 0) > 10 for r in records)
        observation["max_client_wait_ms"] = max(
            (r.client_wait_ms or 0 for r in records), default=None
        )
        window = time.monotonic() - start
        observation["measurement_window_seconds"] = window
        observation["completed_requests_per_second"] = (
            sum(r.valid and r.status == "completed" for r in records) / window
            if window > 0
            else None
        )
        observation["configured_rate_achieved"] = (
            not observation["unissued"] and not observation["delayed"]
        )


async def prefixes(run, target, client):
    settings = run.config.prefix
    prefix = (" The box is gray." * (settings.prefix_chars // 17 + 1))[: settings.prefix_chars]
    for index in range(settings.repeats):
        for kind in ("shared", "independent"):
            # Deterministic controls of equal character length; observed token counts stay separate.
            control = hashlib.sha256(f"control-{index}".encode()).hexdigest()
            matched = (control * (len(prefix) // len(control) + 1))[: len(prefix)]
            prompt = label_prompt(len(prefix) + 80, prefix if kind == "shared" else matched)
            prompt += f"\nTrial {index:04d}."
            request = spec(
                prompt,
                f"prefix_{kind}",
                "prefix",
                expected="K7P4",
                prefix_group=kind,
                order=index,
                cache_state="first unique use is not proof of empty server cache",
            )
            if not await run.request(target, client, request):
                return
    history = []
    for turn in range(settings.history_turns):
        prompt = label_prompt(96)
        messages = [*history, {"role": "user", "content": prompt}]
        request = spec(prompt, "prefix_history", "prefix", expected="K7P4", turn=turn, mode="fixed")
        request.messages = messages
        request.input_chars = sum(len(m["content"]) for m in messages)
        if request.input_chars + request.max_tokens + 32 > run.config.context_limit:
            run.unfinished[target.name].add("prefix")
            run.observations[target.name]["prefix_history_stop"] = (
                "Conservative character admission ceiling reached; no history cropped."
            )
            break
        if not await run.request(target, client, request):
            break
        history = [*messages, {"role": "assistant", "content": "K7P4"}]


def bucket_spec(settings, input_shape, output_shape, scenario, check, level):
    chars = settings.input_chars[input_shape]
    cap = settings.output_tokens[output_shape]
    if output_shape == "short":
        prompt, expected = label_prompt(chars), "K7P4"
    else:
        # Realistic EOS allowed. A cap does not establish that this length was generated.
        prompt = label_prompt(chars).replace(
            "Reply with the box label only.",
            f"First write the box label, then list the numbers from 1 to {cap}.",
        )
        expected = None
    return spec(
        prompt,
        scenario,
        check,
        expected=expected,
        max_tokens=cap,
        input_bucket=input_shape,
        output_bucket=output_shape,
        level=level,
        requested_input_chars=chars,
        requested_output_tokens=cap,
    )


async def buckets(run, target, client):
    settings = run.config.buckets
    for level in settings.levels:
        for input_shape in ("short", "medium", "long"):
            for output_shape in ("short", "medium", "long"):
                scenario = f"bucket_c{level}_{input_shape}_{output_shape}"
                request = bucket_spec(
                    settings, input_shape, output_shape, scenario, "buckets", level
                )
                end = min(run.deadline, time.monotonic() + settings.window_seconds)
                count = 0
                while (
                    count < settings.samples_per_bucket
                    and time.monotonic() < end
                    and run.allowed(target.name)
                ):
                    batch = min(level, settings.samples_per_bucket - count)
                    results = await asyncio.gather(
                        *(
                            run.request(
                                target,
                                client,
                                request,
                                timeout_override=max(0.001, end - time.monotonic()),
                            )
                            for _ in range(batch)
                        )
                    )
                    count += sum(r is not None for r in results)
                if count < settings.samples_per_bucket:
                    run.unfinished[target.name].add("buckets")
    # Controls use precisely the same short request as every mixed pair.
    short = bucket_spec(settings, "short", "short", "mixed_control", "mixed", 1)
    await run.group(target, client, [short], "mixed_control", count=settings.mixed_pairs)
    if run.config.concurrency < 2:
        run.unfinished[target.name].add("mixed")
        return
    heavy_shapes = [kind for kind, weight in settings.heavy_weights.items() for _ in range(weight)]
    for kind in heavy_shapes:
        for phase in ("before_output", "active_stream"):
            for index in range(settings.mixed_pairs):
                if not run.allowed(target.name):
                    run.unfinished[target.name].add("mixed")
                    return
                heavy = bucket_spec(
                    settings,
                    "long" if kind == "long_input" else "short",
                    "short" if kind == "long_input" else "long",
                    f"mixed_heavy_{kind}_{phase}",
                    "mixed",
                    2,
                )
                light = short.model_copy(deep=True)
                light.scenario = f"mixed_short_{kind}_{phase}"
                light.workload.update(pair=index, heavy_type=kind, interference_phase=phase)
                event = asyncio.Event()
                if phase == "before_output":
                    started = asyncio.Event()
                    first = asyncio.create_task(
                        run.request(target, client, heavy, on_start=started.set)
                    )
                    waiter = asyncio.create_task(started.wait())
                    try:
                        await asyncio.wait([first, waiter], return_when=asyncio.FIRST_COMPLETED)
                        light.workload["heavy_active_at_dispatch"] = (
                            not first.done() and started.is_set()
                        )
                        await run.request(target, client, light)
                        await first
                    finally:
                        waiter.cancel()
                        if not first.done():
                            first.cancel()
                        await asyncio.gather(waiter, first, return_exceptions=True)
                else:
                    first = asyncio.create_task(run.request(target, client, light, event))
                    waiter = asyncio.create_task(event.wait())
                    try:
                        await asyncio.wait([first, waiter], return_when=asyncio.FIRST_COMPLETED)
                        if event.is_set() and not first.done():
                            heavy.workload["short_stream_active_at_dispatch"] = True
                            await run.request(target, client, heavy)
                        else:
                            run.unfinished[target.name].add("mixed")
                        await first
                    finally:
                        waiter.cancel()
                        if not first.done():
                            first.cancel()
                        await asyncio.gather(waiter, first, return_exceptions=True)


async def sessions(run, target, client):
    settings = run.config.sessions
    observation = {
        "failure_policy": settings.failure_policy,
        "sessions": [],
        "history_admission": "Conservative characters plus output/template reserve; actual tokens reported separately. Never crop required history.",
        "live_baseline": "Live generated inputs can differ; compare measured shape/hash evidence, not presumed identical histories.",
    }
    run.observations[target.name]["sessions"] = observation

    async def session(mode, index):
        history, previous = [], None
        session_id = f"{target.name}:{mode}:{index}"
        item = {"id": session_id, "mode": mode, "turns": [], "stop_reason": None}
        observation["sessions"].append(item)
        due = time.monotonic()
        for turn, prompt in enumerate(settings.turns):
            if not run.allowed(target.name):
                item["stop_reason"] = run.reason or "target request budget reached"
                run.unfinished[target.name].add("sessions")
                break
            await asyncio.sleep(
                max(0, min(due - time.monotonic(), run.deadline - time.monotonic()))
            )
            messages = [*history, {"role": "user", "content": prompt}]
            input_chars = sum(len(m["content"]) for m in messages)
            if input_chars + settings.max_tokens + 32 > run.config.context_limit:
                item["stop_reason"] = (
                    "history admission ceiling reached; required history not cropped"
                )
                run.unfinished[target.name].add("sessions")
                break
            request = spec(
                prompt,
                f"session_{mode}_turn{turn}",
                "sessions",
                max_tokens=settings.max_tokens,
                session_id=session_id,
                turn_id=turn,
                mode=mode,
                history_messages=len(history),
                configured_delay_seconds=settings.delay_seconds,
                scheduled_ms=(due - run.start) * 1000,
            )
            request.messages, request.input_chars = messages, input_chars
            # The built-in follow-up has a deterministic expectation even in live mode.
            if (
                turn
                and prompt
                == "Repeat exactly the bakery name you just suggested, with no extra text."
            ):
                request.scorer, request.expected = "exact", previous
            record = await run.request(target, client, request)
            if record is None:
                item["stop_reason"] = run.reason or "request budget reached"
                run.unfinished[target.name].add("sessions")
                break
            item["turns"].append(record.id)
            if (
                not record.valid
                or record.status != "completed"
                or record.finish_reason == "length"
                or record.score is False
            ):
                item["stop_reason"] = (
                    "failed task/transport, cancelled or truncated turn; stopped without substitute answer"
                )
                run.unfinished[target.name].add("sessions")
                break
            if (
                record.input_tokens is not None
                and record.input_tokens + settings.max_tokens > run.config.context_limit
            ):
                item["stop_reason"] = "observed context budget reached"
                run.unfinished[target.name].add("sessions")
                break
            previous = (
                record.output
                if mode == "live"
                else settings.saved_answers[turn]
                if turn < len(settings.saved_answers)
                else ""
            )
            history = [*messages, {"role": "assistant", "content": previous}]
            due = time.monotonic() + settings.delay_seconds
        item["completed_turns"] = len(item["turns"])
        item["planned_turns"] = len(settings.turns)

    for mode in settings.modes:
        for start in range(0, settings.sessions, settings.concurrency):
            await asyncio.gather(
                *(
                    session(mode, index)
                    for index in range(start, min(start + settings.concurrency, settings.sessions))
                )
            )


async def consistency(run, target, client, fixtures):
    settings = run.config.consistency
    # Always sequential first, then selected concurrent levels, using independent identical requests.
    for shape in settings.shapes:
        base = fixtures[shape][0].model_copy(deep=True)
        base.options.update(temperature=settings.temperature)
        if settings.seed is not None:
            base.options["seed"] = settings.seed
        for level in dict.fromkeys([1, *settings.levels]):
            base.scenario, base.check_ids = f"consistency_c{level}_{shape}", ["consistency"]
            base.workload = {
                "level": level,
                "input_bucket": shape,
                "equality": settings.equality,
                "temperature": settings.temperature,
                "seed": settings.seed,
                "option_support": "requested; runtime enforcement unverified",
            }
            await run.group(
                target, client, [base], base.scenario, count=settings.repetitions, concurrency=level
            )


TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Return the weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


async def tools(run, target, client):
    for forced in [False, True] if run.config.forced_tool_diagnostic else [False]:
        for stream in (False, True) if run.config.stream else (False,):
            options = {
                "tools": [TOOL],
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}}
                if forced
                else "auto",
            }
            request = spec(
                "Use get_weather to check the weather in Delhi.",
                f"tools_{'forced' if forced else 'auto'}_{'stream' if stream else 'complete'}",
                "tools",
                max_tokens=run.config.max_output_tokens,
                options=options,
                selection="forced diagnostic" if forced else "automatic",
                capability="required when selected",
            )
            request.scorer, request.stream = "tool", stream
            await run.request(target, client, request)


async def execute(run, target, client, fixtures):
    for field, callback in (
        ("arrivals", arrivals),
        ("prefix", prefixes),
        ("buckets", buckets),
        ("sessions", sessions),
        ("consistency", consistency),
        ("tool_calling", tools),
    ):
        if getattr(run.config, field):
            run.emit("scenario_started", target.name, field)
            if not run.allowed(target.name):
                run.unfinished[target.name].update({"tools" if field == "tool_calling" else field})
                continue
            if field in {"arrivals", "consistency"}:
                await callback(run, target, client, fixtures)
            else:
                await callback(run, target, client)
            run.emit("scenario_finished", target.name, field)


def results(run, target, stats, violations, missing_timing):
    result = []
    selected = set(run.config.checks) - set(CORE_CHECKS)
    for check in CHECK_NAMES:
        if check not in selected:
            continue
        records = [r for r in run.records if r.target == target.name and check in r.check_ids]
        metrics = stats(records)
        status = "pass"
        summary = "Bounded workload observed; untested combinations remain unverified."
        minimum = run.config.limits.min_samples
        groups = {}
        for scenario in dict.fromkeys(r.scenario for r in records):
            items = [r for r in records if r.scenario == scenario]
            entry = stats(items)
            entry["achieved_overlap"] = max((r.overlap for r in items), default=0)
            entry["workload"] = items[0].workload
            if check == "consistency":
                hashes = Counter(
                    r.answer_hash
                    for r in items
                    if r.valid and r.status == "completed" and r.answer_hash
                )
                entry.update(
                    distinct_answer_hashes=dict(hashes),
                    agreement_rate=max(hashes.values()) / sum(hashes.values()) if hashes else None,
                    equality=run.config.consistency.equality,
                    correctness=entry["correct"] / entry["scored"] if entry["scored"] else None,
                    expected_samples=run.config.consistency.repetitions,
                )
                if len(hashes) > 1 and run.config.consistency.strict:
                    status, summary = (
                        "fail",
                        "Configured strict consistency violated; correctness is reported independently.",
                    )
                if len(hashes) > 1 and not run.config.consistency.strict:
                    summary = "Answer variation observed; variation alone is diagnostic, not a correctness failure."
                if (
                    len(items) < run.config.consistency.repetitions
                    or entry["completed"] < minimum
                    or entry["achieved_overlap"] < items[0].workload["level"]
                ):
                    if status != "fail":
                        status = "inconclusive"
            if check == "buckets":
                level = items[0].workload["level"]
                expected = items[0].workload["requested_output_tokens"]
                entry["output_length_exercised"] = sum(
                    r.output_tokens is not None and r.output_tokens >= expected * 0.5 for r in items
                )
                entry["input_length_verified"] = sum(r.input_tokens is not None for r in items)
                if (
                    len(items) < run.config.buckets.samples_per_bucket
                    or entry["completed"] < minimum
                    or entry["achieved_overlap"] < level
                    or (
                        items[0].workload["output_bucket"] != "short"
                        and entry["output_length_exercised"] < minimum
                    )
                    or not entry["input_length_verified"]
                ):
                    if status != "fail":
                        status = "inconclusive"
            if check == "prefix":
                entry["cache_observations"] = [
                    {
                        "request_id": r.id,
                        "input_tokens": r.input_tokens,
                        "cached_tokens": r.cached_prompt_tokens,
                        "source": r.cache_source,
                        "reuse": "reported" if r.cached_prompt_tokens is not None else "unverified",
                    }
                    for r in items
                ]
            groups[scenario] = entry
        metrics["buckets"] = groups
        if check == "arrivals":
            metrics["schedule"] = run.observations[target.name].get("arrivals", {})
            if metrics["schedule"].get("unissued") or metrics["schedule"].get("delayed"):
                status, summary = (
                    "inconclusive",
                    "Scheduled traffic was delayed or unissued; configured arrival rate is not established.",
                )
        if check == "sessions":
            metrics["sessions"] = run.observations[target.name].get("sessions")
            if not metrics["sessions"] or any(
                s["stop_reason"] for s in metrics["sessions"]["sessions"]
            ):
                status, summary = (
                    "inconclusive",
                    "Sessions stopped at a failure or budget; complete conversation coverage not established.",
                )
        if check == "mixed":
            controls = [r for r in records if r.scenario == "mixed_control"]
            control = stats(controls)
            comparisons = []
            for name, row in groups.items():
                if not name.startswith("mixed_short"):
                    continue
                ratio = (
                    row["p50_ms"] / control["p50_ms"]
                    if row["p50_ms"] is not None and control["p50_ms"]
                    else None
                )
                observed = row["achieved_overlap"] >= 2
                comparisons.append(
                    {
                        "scenario": name,
                        "short_only": control,
                        "mixed_short": row,
                        "latency_ratio": ratio,
                        "overlap_observed": observed,
                    }
                )
                if (
                    not observed or row["completed"] < run.config.buckets.mixed_pairs
                ) and status != "fail":
                    status = "inconclusive"
                if (
                    ratio is not None
                    and run.config.limits.fairness_max_ratio
                    and ratio > run.config.limits.fairness_max_ratio
                    and run.max_lag_ms <= 100
                ):
                    status, summary = (
                        "fail",
                        "Short-request latency exceeded the configured mixed-load ratio.",
                    )
            metrics["comparisons"] = comparisons
            if not comparisons:
                status = "inconclusive"
        if check == "tools":
            auto = [r for r in records if r.workload.get("selection") == "automatic"]
            expected_count = 2 if run.config.stream else 1
            metrics["forced_diagnostic"] = [
                {
                    "request_id": r.id,
                    "status": r.status,
                    "score": r.score,
                    "reason": r.error or r.score_message,
                }
                for r in records
                if r.workload.get("selection") == "forced diagnostic"
            ]
            metrics["capability_support"] = (
                "observed for built-in fixture"
                if len(auto) == expected_count and all(r.score is True for r in auto)
                else "rejected or unverified; see request outcomes"
            )
            if any(r.score is not True for r in auto):
                status, summary = (
                    "fail",
                    "Required automatic tool capability failed; parsed calls required and no tools executed.",
                )
            elif len(auto) < expected_count:
                status, summary = (
                    "inconclusive",
                    "Automatic tool coverage unfinished. Forced selection is separate diagnostic evidence.",
                )
        assessed = (
            records
            if check != "tools"
            else [r for r in records if r.workload.get("selection") == "automatic"]
        )
        assessed_stats = stats(assessed)
        if check != "tools" and assessed_stats["completed"] < minimum and status != "fail":
            status, summary = "inconclusive", "Too few successful samples for workload acceptance."
        errors = assessed_stats["error_rate"]
        cap_violations = [
            r.id
            for r in assessed
            if r.completion_tokens is not None and r.completion_tokens > r.requested_max_tokens
        ]
        metrics["output_cap_violations"] = cap_violations
        if (
            cap_violations
            or any(r.score is False for r in assessed)
            or (errors is not None and errors > run.config.limits.max_error_rate)
        ):
            status, summary = (
                "fail",
                "Workload task checks or request errors violated configured limits; variation and telemetry are assessed separately.",
            )
        timing = violations(assessed, run.config.limits)
        missing = missing_timing(assessed, run.config.limits) if assessed else []
        if missing:
            metrics["missing_timing_metrics"] = missing
            if check != "tools" and status != "fail":
                status, summary = (
                    "inconclusive",
                    "Configured workload timing limits cannot be checked with available measurements.",
                )
        if timing:
            metrics["timing_violations"] = timing
            if run.max_lag_ms > 100 and status != "fail":
                status, summary = (
                    "inconclusive",
                    "Generator saturation prevents attributing timing violations to the endpoint.",
                )
            elif run.max_lag_ms <= 100:
                status, summary = "fail", "Workload exceeded configured timing limits."
        if not records or (check in run.unfinished[target.name] and status != "fail"):
            status, summary = (
                "inconclusive",
                "Requested workload coverage unfinished within shared budgets.",
            )
        result.append(
            CheckResult(
                id=check,
                target=target.name,
                title=CHECK_NAMES[check],
                status=status,
                summary=summary,
                metrics=metrics,
                evidence_ids=[r.id for r in records],
            )
        )
    return result
