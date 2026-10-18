"""Conversation workload allocation and client-observed performance summaries."""

from collections import Counter
from statistics import mean


def workload_plan(workloads, count):
    """Apportion a finite conversation budget, then interleave its task types."""
    scale = max(workload.weight for workload in workloads)
    weights = [workload.weight / scale for workload in workloads]
    total = sum(weights)
    quotas = [count * weight / total for weight in weights]
    counts = [int(quota) for quota in quotas]
    order = sorted(range(len(counts)), key=lambda i: (-(quotas[i] - counts[i]), i))
    for index in order[:count - sum(counts)]:
        counts[index] += 1
    issued = [0] * len(counts)
    plan = []
    for slot in range(count):
        index = max((i for i in range(len(counts)) if issued[i] < counts[i]),
                    key=lambda i: ((slot + 1) * counts[i] / count - issued[i], -i))
        issued[index] += 1
        plan.append(workloads[index])
    return plan


def _summary(records, stats):
    values = stats(records)
    rates = sorted(r.generation_tokens_per_second for r in records
                   if r.generation_tokens_per_second is not None)
    # Interpolated percentile, matching the runner's percentile convention.
    position = (len(rates) - 1) * .1
    lower = int(position) if rates else 0
    values.update(
        generation_tokens_per_second_mean=mean(rates) if rates else None,
        generation_tokens_per_second_p10=(rates[lower] + (rates[min(lower + 1, len(rates)-1)]
            - rates[lower]) * (position-lower)) if rates else None,
        unmeasured_generation_requests=values["completed"] - len(rates),
        capped_responses=sum(r.finish_reason == "length" for r in records),
    )
    return values


def performance(observation, records, stats):
    observation = observation or {}
    users = []
    sessions = observation.get("sessions", [])
    for session in sessions:
        selected = [r for r in records if r.workload.get("session_id") == session["id"]]
        row = _summary(selected, stats)
        complete = (not session["stop_reason"] and len(session["turns"]) == session["planned_turns"])
        row.update(session_id=session["id"], task_type=session.get("task_type", "exact_repeat"),
                   mode=session["mode"], planned_turns=session["planned_turns"],
                   conversation_completed=complete, stop_reason=session["stop_reason"])
        users.append(row)
    user_rates = [row["generation_tokens_per_second_mean"] for row in users
                  if row["generation_tokens_per_second_mean"] is not None]
    tasks = []
    turns = []
    for task_type, mode in dict.fromkeys((r.workload.get("task_type", "exact_repeat"),
                                        r.workload.get("mode")) for r in records):
        selected = [r for r in records if r.workload.get("task_type", "exact_repeat") == task_type
                    and r.workload.get("mode") == mode]
        tasks.append(dict(_summary(selected, stats), task_type=task_type, mode=mode))
        for turn in sorted({r.workload.get("turn_id", 0) for r in selected}):
            turns.append(dict(_summary([r for r in selected if r.workload.get("turn_id", 0) == turn], stats),
                              task_type=task_type, mode=mode, turn=turn + 1))
    result = _summary(records, stats)
    result.update(
        kind=observation.get("kind", "correctness"),
        configured_concurrency=observation.get("configured_concurrency"),
        achieved_request_overlap=max((r.overlap for r in records), default=0),
        planned_conversations=observation.get("planned_sessions", len(sessions)),
        started_conversations=len(sessions),
        completed_conversations=sum(row["conversation_completed"] for row in users),
        users_with_measured_generation=len(user_rates),
        mean_user_generation_tokens_per_second=mean(user_rates) if user_rates else None,
        actual_task_mix=dict(Counter(session.get("task_type", "exact_repeat") for session in sessions)),
        users=users, tasks=tasks, turns=turns,
        interpretation="Mean of each user's mean response generation rate; each measured user has equal weight. "
                       "Excludes first-output wait and configured inter-turn pauses. Raw rates are estimates; "
                       "see client timing quality and missing-measurement coverage. Aggregate throughput includes "
                       "the whole recorded request window, including pauses. Capped replies are counted, not quality-certified.",
    )
    return result
