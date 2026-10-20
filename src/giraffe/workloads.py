"""Opt-in workloads on the suite's shared budget. No tools or server commands execute."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import uuid
from collections import Counter
from pathlib import Path

from giraffe.fixtures import final_answer, reasoning_budget
from giraffe.logprob_workload import corpus_width, refill, sample_count
from giraffe.overlap import NEEDS_STREAM, answer_window, evidenced_overlap, scheduling_delay_windows
from giraffe.models import INTERNAL_CHECK_NAMES as CHECK_NAMES, CORE_CHECKS, CheckResult, RequestSpec, ToolExpectation
from giraffe.tool_checks import default_tool_fixture, is_discovery_rejection, resolved_tool_fixtures, resolved_tool_modes

# One settings block can drive multiple selectable workloads.
WORKLOAD_CHECKS = {
    "arrivals": {"arrivals"}, "prefix": {"prefix"},
    "sessions": {"sessions"},
    "consistency": {"consistency"}, "tool_calling": {"tools"},
    "forced_tool_diagnostic": {"tools"}, "tool_checks": {"tools"},
    "concurrent_load": {"concurrency"},
}


# Capture the loaded generator's identity, including deterministic fixture construction.
FIXTURE_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


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


def deterministic_options(config, options=None):
    options = dict(options or {})
    if "temperature" not in config.request_options:
        options.setdefault("temperature", 0)
    return options


def consistency_options(config, options=None):
    options = dict(options or {})
    settings = config.consistency
    if "temperature" in settings.model_fields_set:
        options["temperature"] = settings.temperature
    if settings.seed is not None:
        options["seed"] = settings.seed
    return deterministic_options(config, options)


COPY_PASSAGES = (
    "At sunrise, Mira opened the garden gate and placed a blue notebook beside the fountain. "
    "A small brown bird landed on the fence while her neighbor carried fresh bread across the square. "
    "By noon, the clouds had cleared, and three children were drawing a map beneath the old oak tree. "
    "Before leaving, she watered the roses, closed the wooden shutters, and left a note for the evening caretaker.",
    "The museum keeper found a brass key inside an empty ceramic bowl. "
    "Outside, a bicycle rested against the stone wall beside a basket of oranges. "
    "Later that afternoon, a visitor returned a borrowed book with a pressed fern between its pages.",
    "On Tuesday, the harbor bell rang just before the first ferry arrived. "
    "An engineer checked the red signal lamp while a baker delivered six warm rolls to the ticket office. "
    "As the rain stopped, a traveler unfolded a green umbrella and walked toward the eastern pier.",
)


def session_prompt(prompt, turn, value):
    return prompt + (f"\nUse this exact bakery name for this session: {value}. Reply with that name only."
                     if turn == 0 else "\nReply with the original session bakery name only.")


def fixed_session_turns(settings, index, context_limit):
    value = f"Session_{index:04d}_fixed"
    history_chars = 0
    for turn, prompt in enumerate(settings.turns):
        chars = len(session_prompt(prompt, turn, value))
        if history_chars + chars + settings.max_tokens + 32 > context_limit:
            return turn
        history_chars += chars + len(value)
    return len(settings.turns)


LABEL_INSTRUCTION = "\nThe box label is K7P4. Reply with the box label only."


def label_context(chars, prefix=""):
    return (" The box is gray." * (chars // 17 + 1))[: max(0, chars - len(prefix + LABEL_INSTRUCTION))]


def label_prompt(chars, prefix=""):
    return prefix + label_context(chars, prefix) + LABEL_INSTRUCTION


async def arrivals(run, target, client, fixtures):
    settings = run.config.arrivals
    base = fixtures["short"][0].model_copy(deep=True)
    base.scenario, base.check_ids = "arrival", ["arrivals"]
    base.workload = {"requests_per_second": settings.requests_per_second}
    run.start_condition(target.name, base.scenario, base.workload, settings.requests)
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
    queued = set()

    async def dispatch(index, request):
        try:
            return await run.request(target, client, request, on_start=lambda: queued.discard(index))
        finally:
            queued.discard(index)

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
            if len(queued) >= settings.max_pending:
                observation["unissued"].append(
                    {
                        "index": index,
                        "scheduled_ms": (due - run.start) * 1000,
                        "reason": "pending queue full",
                    }
                )
                continue
            request = base.model_copy(
                update={
                    "workload": {
                        **base.workload,
                        "arrival_index": index,
                        "scheduled_ms": (due - run.start) * 1000,
                        "seed": settings.seed,
                    }
                }
            )
            queued.add(index)
            pending.add(asyncio.create_task(dispatch(index, request)))
            await asyncio.sleep(0)
        if pending:
            await asyncio.gather(*pending)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        records = [r for r in run.records if r.target == target.name and r.scenario == "arrival"]
        observation["issued"] = len(records)
        # Arrivals have no intentional cancellation: a cancelled one was cut by
        # the run deadline or a stop and is not evidence that it was served.
        observation["incomplete"] = sum(r.status == "cancelled" for r in records)
        missing = (
            settings.requests
            - len(records)
            - sum(row.get("count", 1) for row in observation["unissued"])
        )
        if missing > 0:
            observation["unissued"].append(
                {"count": missing, "reason": "queued request not dispatched before stop/budget"}
            )
        observation["client_wait_threshold_ms"] = settings.max_client_wait_ms
        observation["delayed"] = sum((r.client_wait_ms or 0) > settings.max_client_wait_ms for r in records)
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
            not observation["unissued"] and not observation["delayed"] and not observation["incomplete"]
        )

        reasons = list(dict.fromkeys(item["reason"] for item in observation["unissued"]))
        run.finish_condition(target.name, base.scenario, base.workload,
            reason=run.condition_reason(target.name, "; ".join(reasons)) if reasons else None)


def prefix_admitted(input_chars, max_tokens, context_limit):
    # Filler can use ~3.6 chars/token; reserve extra for the hex nonce and template.
    return (input_chars + 2) // 3 + 20 + max_tokens + 32 <= context_limit


async def prefixes(run, target, client):
    from giraffe.runner import _context_rejection, _serving_failure

    async def send(request):
        record = await run.request(target, client, request)
        if record is not None and _context_rejection(record):
            # Undo the serving-health charge for our own prompt-sizing failure.
            if _serving_failure(record, request.scenario):
                run.errors -= 1
            record.check_ids = list(request.check_ids)
            run.unfinished[target.name].add("prefix")
            run.observations[target.name]["prefix_stop"] = "Generated prompt exceeded the server context length."
            return None
        return record

    settings = run.config.prefix
    plans = {"prefix_shared": settings.repeats, "prefix_independent": settings.repeats,
             "prefix_history": settings.history_turns}
    for scenario, count in plans.items():
        run.plan_condition(target.name, scenario, planned=count)
    try:
        nonce = uuid.uuid4().hex + "\n"
        prefix = nonce + (" The rock is gray." * (settings.prefix_chars // len(" The rock is gray.") + 1))[: settings.prefix_chars]
        for kind in ("shared", "independent"):
            for index in range(settings.repeats):
                # Unique initial nonces keep warm-cache controls independent across runs and targets.
                rng = random.Random(index)
                matched = f"Control {index:04d}: " + "".join(
                    f" The {rng.choice(('tree', 'bird', 'boat', 'rock'))} is "
                    f"{rng.choice(('blue', 'gray', 'pink', 'gold'))}."
                    for _ in range(len(prefix) // 18 + 1)
                )
                matched = (uuid.uuid4().hex + "\n" + matched)[:len(prefix)]
                prompt = label_prompt(len(prefix) + 80, prefix if kind == "shared" else matched)
                prompt += f"\nTrial {index:04d}."
                request = spec(
                    prompt,
                    f"prefix_{kind}",
                    "prefix",
                    expected="K7P4",
                    prefix_group=kind,
                    shared_prefix_chars=len(prefix),
                    order=index,
                    cache_state="first unique use is not proof of empty server cache",
                )
                if not prefix_admitted(request.input_chars, request.max_tokens, run.config.context_limit):
                    run.unfinished[target.name].add("prefix")
                    run.observations[target.name]["prefix_stop"] = "Estimated token admission ceiling reached."
                    return
                request.options = deterministic_options(run.config, request.options)
                if not await send(request):
                    return
        history = []
        for turn in range(settings.history_turns):
            prompt = label_prompt(96)
            messages = [*history, {"role": "user", "content": prompt}]
            request = spec(prompt, "prefix_history", "prefix", expected="K7P4", turn=turn, mode="fixed")
            request.messages = messages
            request.input_chars = sum(len(m["content"]) for m in messages)
            if not prefix_admitted(request.input_chars, request.max_tokens, run.config.context_limit):
                run.unfinished[target.name].add("prefix")
                run.observations[target.name]["prefix_history_stop"] = (
                    "Estimated token admission ceiling reached; no history cropped."
                )
                break
            request.options = deterministic_options(run.config, request.options)
            if not await send(request):
                break
            history = [*messages, {"role": "assistant", "content": "K7P4"}]
    finally:
        for scenario in plans:
            run.finish_condition(target.name, scenario)


async def sessions(run, target, client):
    settings = run.config.sessions
    mixed = settings.profile == "performance"
    observation = {
        "failure_policy": settings.failure_policy,
        "sessions": [],
        "history_admission": "Conservative characters plus output/template reserve; actual tokens reported separately. Never crop required history.",
        "live_baseline": "Live generated inputs can differ; compare measured shape/hash evidence, not presumed identical histories.",
        "kind": settings.profile,
        "planned_sessions": settings.sessions * len(settings.modes),
        "configured_concurrency": settings.concurrency,
    }
    run.observations[target.name]["sessions"] = observation

    async def session(mode, index, workload=None):
        history, previous = [], None
        session_id = f"{target.name}:{mode}:{index}"
        turns = workload.turns if workload else settings.turns
        budget = (workload.max_tokens if workload else None) or settings.max_tokens
        item = {"id": session_id, "mode": mode, "turns": [], "stop_reason": None,
                "task_type": workload.name if workload else "exact_repeat",
                "planned_turns": len(turns)}
        observation["sessions"].append(item)
        value = f"Session_{index:04d}_{mode}"
        item["seeded_value"] = value
        due = time.monotonic()
        for turn, prompt in enumerate(turns):
            if not run.allowed(target.name):
                item["stop_reason"] = run.reason or "target request budget reached"
                run.unfinished[target.name].add("sessions")
                break
            await asyncio.sleep(
                max(0, min(due - time.monotonic(), run.deadline - time.monotonic()))
            )
            if not workload:
                prompt = session_prompt(prompt, turn, value)
            messages = [*history, {"role": "user", "content": prompt}]
            input_chars = sum(len(m["content"]) for m in messages)
            if input_chars + budget + 32 > run.config.context_limit:
                item["stop_reason"] = (
                    "history admission ceiling reached; required history not cropped"
                )
                run.unfinished[target.name].add("sessions")
                break
            request = spec(
                prompt,
                f"session_{mode}_turn{turn}",
                "sessions",
                max_tokens=budget,
                session_id=session_id,
                task_type=item["task_type"],
                turn_id=turn,
                mode=mode,
                history_messages=len(history),
                configured_delay_seconds=settings.delay_seconds,
                scheduled_ms=(due - run.start) * 1000,
            )
            request.messages, request.input_chars = messages, input_chars
            if not workload:
                request.scorer, request.expected = "exact", value
                request.options = deterministic_options(run.config, request.options)
            record = await run.request(target, client, request)
            if record is None:
                item["stop_reason"] = run.reason or "request budget reached"
                run.unfinished[target.name].add("sessions")
                break
            item["turns"].append(record.id)
            if reasoning_budget(record):
                item["stop_reason"] = "reasoning exhausted the output budget before an answer; session recall unverified"
                run.unfinished[target.name].add("sessions")
                break
            if (
                not record.valid
                or record.status != "completed"
                or (not workload and (record.finish_reason == "length" or record.score is not True))
                or record.score is False
            ):
                item["stop_reason"] = (
                    "failed task/transport, cancelled or truncated turn; stopped without substitute answer"
                )
                run.unfinished[target.name].add("sessions")
                break
            if (
                record.input_tokens is not None
                and record.input_tokens + budget > run.config.context_limit
            ):
                item["stop_reason"] = "observed context budget reached"
                run.unfinished[target.name].add("sessions")
                break
            previous = final_answer(record.output)[0] if mode == "live" else value
            history = [*messages, {"role": "assistant", "content": previous}]
            due = time.monotonic() + settings.delay_seconds
        item["completed_turns"] = len(item["turns"])

    from .session_metrics import workload_plan

    session_workloads = workload_plan(settings.workloads, settings.sessions) if mixed else []
    for mode in settings.modes:
        planned = sum(len(workload.turns) for workload in session_workloads) if mixed else settings.sessions * len(settings.turns)
        run.plan_condition(target.name, f"session_{mode}_turn0", planned=planned)
    try:
        if mixed:
            pending = asyncio.Queue()
            for index, workload in enumerate(session_workloads):
                pending.put_nowait((index, workload))

            async def worker():
                while not pending.empty():
                    index, workload = pending.get_nowait()
                    await session("live", index, workload)

            await asyncio.gather(*(worker() for _ in range(min(settings.concurrency, settings.sessions))))
            return

        for mode in settings.modes:
            next_index = 0

            async def worker():
                nonlocal next_index
                while next_index < settings.sessions:
                    index = next_index
                    next_index += 1
                    await session(mode, index)

            await asyncio.gather(*(worker() for _ in range(min(settings.concurrency, settings.sessions))))
    finally:
        for mode in settings.modes:
            skipped = any(item["mode"] == mode and item["stop_reason"] and
                          len(item["turns"]) < item["planned_turns"] for item in observation["sessions"])
            run.finish_condition(target.name, f"session_{mode}_turn0",
                reason="dependent turns skipped" if skipped else None)


async def consistency(run, target, client, fixtures):
    settings = run.config.consistency
    if settings.logprobs is not None:
        from giraffe.logprob_workload import run_logprob_consistency
        await run_logprob_consistency(run, target, client, fixtures)
        return
    levels = list(dict.fromkeys([1, *settings.levels]))
    for shape in settings.shapes:
        for level in levels:
            run.plan_condition(target.name, f"consistency_c{level}_{shape}",
                planned=settings.prompts_per_shape * sample_count(settings.repetitions, level))
    try:
        for shape in settings.shapes:
            for prompt_index in range(settings.prompts_per_shape):
                answer = COPY_PASSAGES[prompt_index % len(COPY_PASSAGES)]
                if prompt_index >= len(COPY_PASSAGES):
                    answer += f" The parcel tag reads R{prompt_index:04d}."
                context = ""
                if shape == "long":
                    context = label_context(max(64, (run.config.context_limit - 256) * 2))
                    context += "\nThe background above is context only.\n"
                prompt = context + f"Copy the following text exactly, with no extra text:\n{answer}"
                base = spec(prompt, "consistency", "consistency", expected=answer,
                            max_tokens=min(192, run.config.max_output_tokens),
                            options=consistency_options(run.config))
                for level in dict.fromkeys([1, *settings.levels]):
                    base.scenario = f"consistency_c{level}_{shape}_p{prompt_index}"
                    base.check_ids = ["consistency"]
                    base.workload = {"level": level, "input_bucket": shape, "prompt_index": prompt_index,
                                     "equality": settings.equality, "temperature": (run.config.request_options | base.options).get("temperature"),
                                     "seed": settings.seed, "option_support": "requested; runtime enforcement unverified"}
                    await refill(run, target, client, [base], sample_count(settings.repetitions, level), level, condition_plan=False)
    finally:
        for shape in settings.shapes:
            for level in levels:
                run.finish_condition(target.name, f"consistency_c{level}_{shape}")


TOOL = default_tool_fixture().tools[0]


async def tools(run, target, client):
    fixtures = resolved_tool_fixtures(run.config)
    modes = resolved_tool_modes(run.config)
    custom = bool(run.config.tool_checks and run.config.tool_checks.fixtures)
    from giraffe.tool_conversations import run_conversation
    for fixture in run.config.tool_checks.conversations if run.config.tool_checks else []:
        for mode in modes:
            await run_conversation(run, target, client, fixture, mode)
    expectations = {
        fixture.fixture_id: ToolExpectation(
            tools=fixture.tools, expected_calls=fixture.expected_calls,
            expected_content=fixture.expected_content, choice=fixture.choice)
        for fixture in fixtures
    }
    for forced in [False, True] if run.config.forced_tool_diagnostic else [False]:
        for fixture in fixtures:
            if forced and fixture.choice:
                continue
            for mode in modes:
                if not run.allowed(target.name):
                    run.unfinished[target.name].add("tools")
                    return
                selection = "forced diagnostic" if forced else "contract" if fixture.choice else "automatic"
                prefix = f"tools_{fixture.fixture_id}" if custom else "tools"
                choice_mode = "forced" if forced else fixture.choice.mode if fixture.choice else "auto"
                scenario = f"{prefix}_{choice_mode}_{mode}"
                messages = [message.model_dump() for message in fixture.messages]
                request = RequestSpec(
                    fixture_id=f"{scenario}.v1", scenario=scenario, check_ids=["tools"],
                    messages=messages, scorer="tool", stream=mode == "stream",
                    input_chars=sum(len(message["content"]) for message in messages),
                    max_tokens=run.config.max_output_tokens,
                    options={**fixture.options, "tools": fixture.tools,
                             **({} if fixture.choice else {
                                 "tool_choice": {"type": "function", "function": {
                                     "name": fixture.expected_calls[0].name}} if forced else "auto"})},
                    tool_expectation=expectations[fixture.fixture_id],
                    workload={"selection": selection, "capability": "required when selected",
                              "tool_fixture_id": fixture.fixture_id, "mode": mode},
                )
                request.options = deterministic_options(run.config, request.options)
                run.start_condition(target.name, scenario, request.workload, planned=1)
                try:
                    await run.request(target, client, request)
                finally:
                    run.finish_condition(target.name, scenario, request.workload)


async def execute(run, target, client, fixtures):
    from giraffe.concurrent_load import execute as concurrent_load
    for field, callback in (
        ("arrivals", arrivals),
        ("prefix", prefixes),
        ("concurrent_load", concurrent_load),
        ("sessions", sessions),
        ("consistency", consistency),
        ("tool_calling", tools),
    ):
        selected = WORKLOAD_CHECKS[field].intersection(run.config.checks)
        if selected and getattr(run.config, field):
            run.emit("scenario_started", target.name, field)
            if field != "concurrent_load" and not run.allowed(target.name):
                run.unfinished[target.name].update(selected)
                continue
            if field in {"arrivals", "consistency"}:
                await callback(run, target, client, fixtures)
            else:
                await callback(run, target, client)
            run.emit("scenario_finished", target.name, field)


def results(run, target, stats, violations, missing_timing, *, saturated=False):
    from .timing import configured_metrics, qualified_violations, run_quality

    config = run.config
    result = []
    selected = set(config.checks) - set(CORE_CHECKS)
    for check in CHECK_NAMES:
        if check in {"concurrency", "token_stress"}:
            continue
        if check not in selected:
            continue
        from giraffe.runner import _check_config
        config = run.config
        records = [r for r in run.records if r.target == target.name and check in r.check_ids]
        metrics = stats(records)
        quality = run_quality(run, records, target.name, configured_metrics(config.limits, check))
        saturated = quality["status"] == "client_delayed"
        metrics["timing_quality"] = quality
        status = "pass"
        summary = "Bounded workload observed; untested combinations remain unverified."
        minimum = config.limits.min_samples
        groups = {}
        for scenario in dict.fromkeys(r.scenario for r in records):
            items = [r for r in records if r.scenario == scenario and r.workload.get("phase") != "priming"]
            if not items:
                continue
            entry = stats(items)
            entry["achieved_overlap"] = max((r.workload.get("observed_overlap", 0) for r in items), default=0)
            entry["workload"] = items[0].workload
            if check == "prefix":
                entry["cache_observations"] = [{
                    "request_id": r.id, "input_tokens": r.input_tokens,
                    "cached_tokens": r.cached_prompt_tokens, "source": r.cache_source,
                    "reuse": "reported" if r.cached_prompt_tokens is not None else "unverified",
                } for r in items]
            if check == "consistency":
                hashes = Counter(
                    r.answer_hash
                    for r in items
                    if r.valid and r.status == "completed" and r.finish_reason != "length"
                    and final_answer(r.output)[0].strip() and r.answer_hash
                )
                entry.update(
                    distinct_answer_hashes=dict(hashes),
                    agreement_rate=max(hashes.values()) / sum(hashes.values()) if hashes else None,
                    equality=config.consistency.equality,
                    correctness=entry["correct"] / entry["scored"] if entry["scored"] else None,
                    expected_samples=(config.consistency.repetitions * corpus_width(config.consistency)
                                      if config.consistency.logprobs else
                                      sample_count(config.consistency.repetitions, items[0].workload["level"])),
                    interpretation="Run-to-run repeatability within this level.",
                )
                if len(hashes) > 1 and config.consistency.strict:
                    status, summary = (
                        "fail",
                        "Configured strict consistency violated; correctness is reported independently.",
                    )
                if len(hashes) > 1 and not config.consistency.strict and status == "pass":
                    status, summary = "inconclusive", "Answer variation observed; enable strict to enforce."
                if (
                    len(items) < entry["expected_samples"]
                    or sum(hashes.values()) < minimum
                    or sum(r.valid and r.status == "completed" and r.finish_reason != "length"
                           and r.workload.get("load_qualified", False) for r in items) < minimum
                ):
                    if status != "fail":
                        status, summary = (
                            "inconclusive",
                            "Repetition/load coverage lacks sufficient samples or server-evidenced overlap (overlapping streamed output).",
                        )
            groups[scenario] = entry
        metrics["buckets"] = groups
        if check == "prefix":
            shared = [r for r in records if r.scenario == "prefix_shared"]
            independent = [r for r in records if r.scenario == "prefix_independent"]
            first, repeat = stats(shared[:1]), stats(shared[1:])
            observed = [r for r in shared[1:] if (r.valid and r.status == "completed" or reasoning_budget(r))
                        and r.cached_prompt_tokens is not None and r.input_tokens]
            def reused(r):
                prefix_fraction = r.workload.get("shared_prefix_chars", 0) / max(1, r.input_chars)
                threshold = max(1, int((r.input_tokens or 0) * prefix_fraction * .5) // 16 * 16)
                return bool(r.input_tokens and prefix_fraction and r.cached_prompt_tokens is not None
                            and r.cached_prompt_tokens >= threshold)

            verified = [r for r in observed if reused(r)]
            controls = [r for r in independent if (r.valid and r.status == "completed" or reasoning_budget(r))
                        and r.cached_prompt_tokens is not None and r.input_tokens]
            misses = len(observed) - len(verified)
            metrics["cache_reuse_samples"] = len(verified)
            metrics["cache_repeat_misses"] = {"misses": misses, "observed": len(observed),
                                              "planned": run.config.prefix.repeats - 1}
            metrics["cache_controls_discriminative"] = bool(controls) and not any(reused(r) for r in controls)
            counts = f"{misses} of {len(observed)} repeat misses."
            if any(reused(r) for r in controls):
                status, summary = "inconclusive", "cache reporting not discriminative: independent controls also report reuse; " + counts
            elif len(controls) < minimum or len(controls) != run.config.prefix.repeats:
                status, summary = "inconclusive", "cache reuse unverified: missing independent-control cache evidence; " + counts
            elif misses > (run.config.prefix.repeats - 1) / 2:
                prefix_tokens = [r.input_tokens * r.workload.get("shared_prefix_chars", 0) / max(1, r.input_chars)
                                 for r in observed]
                if prefix_tokens and max(prefix_tokens) < 512:
                    status, summary = "inconclusive", "Prefix is estimated below 512 tokens; cache misses do not establish failed reuse; " + counts
                else:
                    status, summary = "fail", "no prefix reuse observed on a majority of repeats; " + counts
            elif len(observed) < run.config.prefix.repeats - 1:
                status, summary = "inconclusive", "cache reuse unverified: missing repeated cache/token evidence; " + counts
            elif len(verified) < minimum:
                status, summary = "inconclusive", "cache reuse unverified: insufficient repeats with substantial reuse; " + counts
            else:
                summary = "Prefix reuse observed with discriminative independent controls; " + counts
            metrics["first_repeat"] = {
                "shared_first": first,
                "shared_repeats": repeat,
                "independent_controls": stats(independent),
                "repeat_to_first_first_output_ratio": (
                    repeat["first_output_p50_ms"] / first["first_output_p50_ms"]
                    if repeat["first_output_p50_ms"] is not None
                    and first["first_output_p50_ms"] else None
                ),
                "interpretation": "First observed use is not proof of an empty cache; timing alone does not establish reuse.",
            }
        if check == "arrivals":
            metrics["schedule"] = run.observations[target.name].get("arrivals", {})
            if (metrics["schedule"].get("unissued") or metrics["schedule"].get("delayed")
                    or metrics["schedule"].get("incomplete")):
                status, summary = (
                    "inconclusive",
                    "Scheduled traffic was delayed, unissued or cancelled before completion; "
                    "configured arrival rate is not established.",
                )
            elif not configured_metrics(config.limits, check):
                status, summary = "inconclusive", "Arrivals observed; no acceptance limit configured."
        if check == "sessions":
            metrics["sessions"] = run.observations[target.name].get("sessions")
            from .session_metrics import performance

            metrics["conversation_performance"] = performance(metrics["sessions"], records, stats)
            if not metrics["sessions"] or any(
                s["stop_reason"] for s in metrics["sessions"]["sessions"]
            ):
                status, summary = (
                    "inconclusive",
                    "Sessions stopped at a failure or budget; complete conversation coverage not established.",
                )
            completed_sessions = {mode: sum(s["mode"] == mode and not s["stop_reason"]
                and s["completed_turns"] == s["planned_turns"]
                for s in (metrics["sessions"] or {}).get("sessions", []))
                for mode in run.config.sessions.modes}
            metrics["completed_sessions_per_mode"] = completed_sessions
            if any(count < minimum for count in completed_sessions.values()) and status != "fail":
                status, summary = "inconclusive", "Insufficient completed independent sessions per mode."
            metrics["configured_session_concurrency"] = run.config.sessions.concurrency
            delays = scheduling_delay_windows(run)
            metrics["evidenced_session_overlap"] = max((evidenced_overlap(r, [other for other in records
                if other is r or other.workload.get("session_id") != r.workload.get("session_id")], delays)
                for r in records), default=0)
            metrics["conversation_performance"]["evidenced_overlap"] = metrics["evidenced_session_overlap"]
            if status != "fail" and (run.config.sessions.concurrency < 2 or metrics["evidenced_session_overlap"] < 2):
                status, summary = "inconclusive", (
                    "Conversations were not observed concurrently: "
                    + ("session concurrency is below two." if run.config.sessions.concurrency < 2 else
                       "no session turn's output overlapped another conversation's output; "
                       + (NEEDS_STREAM if not any(r.stream for r in records) else "client overlap alone does not show server concurrency") + "."))
        if check == "tools":
            auto = [r for r in records if r.workload.get("selection") in {"automatic", "contract"}]
            expected_cases = {(fixture.fixture_id, mode)
                              for fixture in resolved_tool_fixtures(config)
                              for mode in resolved_tool_modes(config)}
            conversations = run.observations[target.name].get("tool_conversations", [])
            expected_cases.update((fixture.fixture_id, mode)
                                  for fixture in (config.tool_checks.conversations if config.tool_checks else [])
                                  for mode in resolved_tool_modes(config))
            observed_cases = [(r.workload.get("tool_fixture_id"), r.workload.get("mode"))
                              for r in auto]
            observed_cases += [(session["fixture_id"], session["mode"]) for session in conversations
                               if session["status"] in {"pass", "fail"}]
            metrics["conversations"] = conversations
            complete_coverage = (len(observed_cases) == len(expected_cases)
                                 and set(observed_cases) == expected_cases)
            metrics["forced_diagnostic"] = [
                {"request_id": r.id, "status": r.status, "score": r.score,
                 "reason": r.error or r.score_message}
                for r in records if r.workload.get("selection") == "forced diagnostic"
            ]
            metrics["outcomes"] = [
                {"request_id": r.id, "fixture_id": r.workload.get("tool_fixture_id"),
                 "mode": r.workload.get("mode"), "selection": r.workload.get("selection"),
                 "score": r.score,
                 **(r.tool_evaluation.model_dump() if r.tool_evaluation else {})}
                for r in records
            ]
            metrics["parity"] = {
                "status": "not_established", "basis": "independent_generations",
                "reason": "Each mode is checked against its fixture. Independent generations "
                          "can differ; these outcomes do not establish parser loss or exact parity.",
            }
            metrics["missing_cases"] = [
                {"fixture_id": fixture_id, "mode": mode}
                for fixture_id, mode in sorted(expected_cases - set(observed_cases))
            ]
            metrics["capability_support"] = (
                "observed for configured fixtures"
                if complete_coverage and all(r.score is True for r in auto)
                and all(session["status"] == "pass" for session in conversations)
                else "rejected or unverified; see request outcomes"
            )
            if any(not is_discovery_rejection(r) and
                   (r.score is False or (not r.valid or r.status != "completed") and not reasoning_budget(r)) for r in auto) or any(
                       session["status"] == "fail" for session in conversations):
                status, summary = (
                    "fail",
                    "Required tool fixture failed; declared choice, text and calls must pass. "
                    "No tools were executed.",
                )
            elif any(r.score is None for r in auto) or any(session["status"] == "inconclusive" for session in conversations):
                status, summary = "inconclusive", "Tool fixture evaluation could not establish correctness. See component outcomes."
            elif not complete_coverage:
                status, summary = (
                    "inconclusive",
                    "Tool fixture coverage unfinished. Legacy forced selection is separate diagnostic evidence.",
                )
        assessed = (
            records
            if check != "tools"
            else [r for r in records if r.workload.get("selection") in {"automatic", "contract", "conversation"}
                  and not is_discovery_rejection(r)]
        )
        if check == "consistency" and not run.config.consistency.logprobs:
            comparisons = []
            for name, entry in groups.items():
                identity = entry["workload"]
                baseline = next((v for v in groups.values() if v["workload"].get("level") == 1
                    and v["workload"].get("input_bucket") == identity.get("input_bucket")
                    and v["workload"].get("prompt_index") == identity.get("prompt_index")), None)
                hashes = entry["distinct_answer_hashes"]
                control_hashes = baseline["distinct_answer_hashes"] if baseline else {}
                modal = max(control_hashes, key=control_hashes.get) if control_hashes else None
                agreement = hashes.get(modal, 0) / sum(hashes.values()) if hashes and modal else None
                entry["level1_agreement_rate"] = agreement
                comparisons.append({"scenario": name, "level": identity["level"],
                                    "level1_modal_hash": modal, "agreement_rate": agreement})
                if identity["level"] != 1 and agreement is not None and agreement < 1:
                    if run.config.consistency.strict:
                        status, summary = "fail", "Strict cross-level answer consistency violated against level-1 modal answer."
                    elif status == "pass":
                        status, summary = "inconclusive", "Cross-level answer variation observed; enable strict to enforce."
                if agreement is None and status != "fail":
                    status, summary = "inconclusive", "Missing comparable level-1 answer baseline."
            metrics["cross_level"] = comparisons
        if check == "consistency" and run.config.consistency.logprobs:
            from giraffe.logprob_workload import summarize_logprobs
            metrics["logprobs"] = summarize_logprobs(records, run.config.consistency, min_samples=minimum)
            probability_status = metrics["logprobs"]["status"]
            reasons = list(dict.fromkeys(reason for row in metrics["logprobs"]["conditions"] for reason in row["coverage_reasons"]))
            if probability_status == "fail" or (probability_status == "inconclusive" and status == "pass"):
                status, summary = probability_status, "Token probability consistency or capability expectation is not satisfied; see independent probability outcomes."
            elif status == "pass":
                summary = "Token probability comparison completed (" + probability_status + "); answer correctness is separate."
            if probability_status == "inconclusive" and status != "fail" and reasons:
                summary += " " + " ".join(reason[:1].upper() + reason[1:].rstrip(".") + "." for reason in reasons)
        assessed_stats = stats(assessed)
        from giraffe.runner import _context_rejection
        prefix_rejected = [r for r in assessed if check == "prefix" and _context_rejection(r)]
        prefix_rejected_ids = {r.id for r in prefix_rejected}
        if check == "prefix":
            metrics["context_rejections"] = len(prefix_rejected)
        budget_records = [r for r in assessed if check in {"prefix", "sessions", "tools"} and reasoning_budget(r)]
        if check in {"prefix", "sessions", "tools"}:
            metrics["reasoning_budget_unscored"] = len(budget_records)
        successful = assessed_stats["completed"] + (len(budget_records) if check == "prefix" else 0)
        if check != "tools" and successful < minimum and status != "fail":
            status, summary = "inconclusive", "Too few successful samples for workload acceptance."
        errors = stats([r for r in assessed if not (
            (check == "consistency" and r.logprob_evidence and
             r.logprob_evidence.status == "rejected" and run.config.consistency.logprobs and
             run.config.consistency.logprobs.support == "discover")
            or (r.tool_conversation and r.tool_conversation["status"] == "inconclusive")
            or (check in {"prefix", "sessions", "tools"} and reasoning_budget(r))
            or r.id in prefix_rejected_ids
        )])["error_rate"]
        if budget_records or prefix_rejected:
            metrics["failed"] -= stats(budget_records + prefix_rejected)["failed"]
            metrics["error_rate"] = errors
        cap_violations = [
            r.id
            for r in assessed
            if r.completion_tokens is not None and r.completion_tokens > r.requested_max_tokens
        ]
        metrics["output_cap_violations"] = cap_violations
        if check in {"consistency"}:
            metrics["correctness_observed"] = (assessed_stats["correct"] / assessed_stats["scored"]
                                                if assessed_stats["scored"] else None)
        if (
            cap_violations
            or (check not in {"consistency"} and any(r.score is False for r in assessed))
            or (errors is not None and errors > config.limits.max_error_rate)
        ):
            status, summary = (
                "fail",
                "Workload task checks or request errors violated configured limits; variation and telemetry are assessed separately.",
            )
        timing = violations(assessed, config.limits)
        missing = missing_timing(assessed, config.limits) if assessed else []
        if missing:
            metrics["missing_timing_metrics"] = missing
            if check != "tools" and status != "fail":
                status, summary = (
                    "inconclusive",
                    "Configured workload timing limits cannot be checked with available measurements.",
                )
        if timing:
            metrics["timing_violations"] = timing
            qualified = qualified_violations(assessed, config.limits, quality)
            if qualified:
                metrics["qualified_timing_violations"] = qualified
                status, summary = "fail", "Workload exceeded configured timing limits without overlapping client scheduling delay."
            elif saturated and status != "fail":
                status, summary = (
                    "inconclusive",
                    "Client scheduling delay prevents attributing timing violations to the endpoint.",
                )
            elif not saturated:
                status, summary = "fail", "Workload exceeded configured timing limits."
        if (
            saturated
            and check != "tools"
            and status != "fail"
            and any((config.limits.first_output_ms, config.limits.latency_ms,
                     config.limits.stream_gap_ms,
                     config.limits.min_output_tokens_per_second))
        ):
            status, summary = (
                "inconclusive",
                "Client scheduling delay prevents qualifying configured workload timing limits.",
            )
        if (not records or check in run.unfinished[target.name]) and status == "pass":
            status, summary = (
                "inconclusive",
                "Requested workload coverage unfinished within shared budgets.",
            )
        if prefix_rejected and status != "fail":
            status, summary = "inconclusive", "Server rejected a Giraffe-built prefix prompt for context length; token admission estimate did not fit."
        if check == "consistency" and run.config.consistency.logprobs:
            probability = metrics["logprobs"]
            if not probability["strict"] and any(
                row["token_divergences"] or row["top_k_membership_changes"] or
                (row["max_absolute_delta"] is not None and
                 row["max_absolute_delta"] > (probability["tolerance"] or 0))
                for row in probability["conditions"] + probability["cross_conditions"]
            ):
                summary += " Probability variation observed; enable strict to enforce."
            delta = probability["max_cross_level_delta"]
            summary += (f" Maximum cross-level delta: {delta if delta is not None else 'unavailable'}; "
                        f"token divergences: {probability['cross_level_token_divergences']}.")
        if check == "consistency" and (correctness := metrics.get("correctness_observed")) is not None and correctness < 1:
            summary += f" correctness_observed: {correctness}."
        if check == "sessions" and status == "pass" and config.sessions.profile == "performance":
            summary = ("Concurrent conversation performance met configured limits." if any(value is not None for value in (
                config.limits.first_output_ms, config.limits.latency_ms, config.limits.stream_gap_ms,
                config.limits.min_output_tokens_per_second)) else
                "Concurrent conversations completed; performance was measured but no timing limits are configured.") + (
                " Answer quality was not assessed; capped replies are reported separately.")
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
