"""Deterministic session plans, independent of worker order and response timing."""

import random

from .session_metrics import workload_plan


def session_plan(settings):
    sampling = settings.sampling
    plans = []
    mixed = settings.profile == "performance"
    conversations = ([("live", index, workload) for index, workload in
                      enumerate(workload_plan(settings.workloads, settings.sessions))] if mixed else
                     [(mode, index, None) for mode in settings.modes for index in range(settings.sessions)])
    for mode, index, workload in conversations:
        seed = sampling.seed if sampling else 42
        rng = random.Random(f"{seed}:{index}" if mixed else f"{seed}:{mode}:{index}")

        def sample(field, default):
            distribution = getattr(sampling, field) if sampling else None
            return (rng.choices(distribution.values, weights=distribution.weights, k=1)[0]
                    if distribution else default)

        count = int(sample("turn_count", len(workload.turns))) if mixed else len(settings.turns)
        turns = []
        for turn in range(count):
            turns.append({
                "think_time_seconds": sample("think_time_seconds", settings.delay_seconds) if turn else 0,
                "input_tokens": sample("input_tokens", None),
                "output_tokens": int(sample("output_tokens", (workload.max_tokens if workload else None) or settings.max_tokens)),
            })
        plans.append({"workload": workload, "turns": turns, **({"mode": mode, "index": index} if not mixed else {})})
    return plans


async def prepare_session_input(run, target, prompt, tokens):
    from .local_tokenizer import prepare_messages

    def build_messages(chars):
        filler = (" neutral" * (chars // 8 + 1))[:chars]
        content = f"Background context (synthetic):\n{filler}\n\n{prompt}" if chars else prompt
        return [{"role": "user", "content": content}]

    return await prepare_messages(run, target, "session_input", build_messages, int(tokens),
                                  tolerance=.05, content_only=True)
